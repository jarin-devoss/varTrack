"""
app/grpc_server/auth_interceptor.py
────────────────────────────────────
One check in front of every gRPC method.

While internal authentication is on (app/api/tokens.py), a call must carry
``authorization: Bearer <grant>`` metadata. The grant is verified here, once,
for the audience of the service being called:

    Orchestrator, CLIService   →  "sync"   (the gateway's grants)
    Watcher                    →  "watch"  (the watcher's own grants)

and its claims are made available to the servicer for the duration of the call
(``tokens.current_grant()``). A servicer therefore learns who is asking from the
verified grant, never from metadata the client can set freely.

A call without a valid grant is refused with UNAUTHENTICATED before the servicer
is reached. With internal authentication off (development) the interceptor does
nothing.
"""
from __future__ import annotations

import logging

import grpc

from app.api import tokens

logger = logging.getLogger(__name__)


def audience_for(method: str) -> str:
    """The grant audience a full method name ("/vartrack.v1.Watcher/Heal") needs."""
    service = method.split("/")[1] if method.count("/") >= 2 else ""
    return tokens.AUDIENCE_WATCH if service.rsplit(".", 1)[-1] == "Watcher" else tokens.AUDIENCE_SYNC


def _refuse(code: grpc.StatusCode, message: str) -> grpc.RpcMethodHandler:
    def _abort(request, context):
        context.abort(code, message)
    return grpc.unary_unary_rpc_method_handler(_abort)


class DependencyInterceptor(grpc.ServerInterceptor):
    """A call that fails because something varTrack depends on cannot be read answers UNAVAILABLE and
    names it, not UNKNOWN (an uncaught error) and not NOT_FOUND (an empty answer)."""

    def intercept_service(self, continuation, handler_call_details):
        from app.dependencies import DependencyError

        handler = continuation(handler_call_details)
        inner = getattr(handler, "unary_unary", None)
        if handler is None or inner is None:
            return handler

        def _guarded(request, context):
            try:
                return inner(request, context)
            except DependencyError as exc:
                logger.error("gRPC %s: %s unavailable: %s", handler_call_details.method, exc.dependency, exc.reason)
                context.abort(grpc.StatusCode.UNAVAILABLE, f"{exc.dependency} unavailable: {exc.reason}")

        return grpc.unary_unary_rpc_method_handler(
            _guarded,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


class GrantInterceptor(grpc.ServerInterceptor):
    """Verify the caller's grant before any servicer runs."""

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or not tokens.enforced():
            return handler

        metadata = {k.lower(): v for k, v in (handler_call_details.invocation_metadata or ())}
        token = tokens.bearer(metadata.get("authorization"))
        if not token:
            logger.warning("gRPC %s refused: no grant (is INTERNAL_AUTH_SECRET set on the caller?)",
                           handler_call_details.method)
            return _refuse(grpc.StatusCode.UNAUTHENTICATED, "a signed grant is required")
        try:
            grant = tokens.verify(token, audience_for(handler_call_details.method))
        except tokens.GrantError as exc:
            logger.warning("gRPC %s refused: %s", handler_call_details.method, exc)
            return _refuse(grpc.StatusCode.UNAUTHENTICATED, f"invalid grant: {exc}")

        inner = handler.unary_unary
        if inner is None:
            # Every method of the orchestrator is unary; anything else is not served.
            return _refuse(grpc.StatusCode.UNIMPLEMENTED, "unsupported call type")

        def _with_grant(request, context):
            marker = tokens.set_current_grant(grant)
            try:
                return inner(request, context)
            finally:
                tokens.reset_current_grant(marker)

        return grpc.unary_unary_rpc_method_handler(
            _with_grant,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
