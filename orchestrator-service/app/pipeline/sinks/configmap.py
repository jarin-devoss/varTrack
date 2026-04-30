"""
app/pipeline/sinks/configmap.py
─────────────────────────────────
Kubernetes ConfigMap sink.

Key format: FLAT (default) or DOT
──────────────────────────────────
ConfigMap data keys must satisfy: [-._a-zA-Z0-9]+

  "flat" (default, safest)   "db.host.port" → "db_host_port"
  "dot"                      "db.host.port" → "db.host.port"   (dots are valid)

SLASH is not supported — ConfigMap keys cannot start with "/".
rule.proto SinkKeyFormat.configmap enforces this at validation time.

Auto-provision
──────────────
When an env is new (e.g. "pr-42" from a first PR push), the sink creates
a brand-new ConfigMap instead of patching an existing one.

Two auto-provision modes:

  env_as_namespace: true
    Each env maps to its own K8s namespace.
    ConfigMap name stays constant, namespace = env.
    e.g.  namespace: pr-42 / name: payments-config

  env_as_name_suffix: true  (default when neither is set)
    ConfigMap name gets the env as a suffix.
    e.g.  name: payments-config-pr-42   namespace: (configured)

The env is also written as:
    metadata.annotations["vartrack.io/env"] = "pr-42"
so tooling (kubectl) can filter/identify per-env ConfigMaps.
"""
from __future__ import annotations

import logging
from typing import Any

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import (
    KeyFormat, format_keys, key_format_from_str, validate_sink_format,
    resolve_destination,
)
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_SINK_KIND      = "configmap"
_DEFAULT_FORMAT = KeyFormat.FLAT


class ConfigmapSink(BaseSink):

    def __init__(
        self,
        *,
        name: str = "configmap",
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        try:
            from kubernetes import client as k8s_client, config as k8s_config
        except ImportError as exc:
            raise RuntimeError("kubernetes is required: pip install kubernetes") from exc

        cfg = rule_config or {}

        self._namespace          = cfg["k8s_namespace"]
        self._cm_name            = cfg["k8s_configmap_name"]
        self._tenant_id          = tenant_id
        self._env_as_namespace   = bool(cfg.get("env_as_namespace", False))
        self._env_as_name_suffix = bool(cfg.get("env_as_name_suffix", True))
        # destination_template: ConfigMap name template, e.g. "myapp-{env}".
        # Takes precedence over env_as_name_suffix logic when set.
        self._dest_tpl     = cfg.get("destination_template", "")

        # Key format: "flat" (default) or "dot". Validated against allowed list.
        fmt_name         = cfg.get("key_format", "flat")
        self._key_format = key_format_from_str(fmt_name)
        validate_sink_format(_SINK_KIND, self._key_format)

        self._k8s_client = k8s_client
        self._api        = self._build_api(cfg, k8s_client, k8s_config)

        logger.debug(
            "ConfigmapSink ready fmt=%s env_as_namespace=%s env_as_name_suffix=%s",
            self._key_format.value, self._env_as_namespace, self._env_as_name_suffix,
        )

    @staticmethod
    def _build_api(cfg, k8s_client, k8s_config):
        api_server = cfg.get("k8s_api_server")
        token      = cfg.get("k8s_token")
        kubeconfig = cfg.get("k8s_kubeconfig_path")
        if api_server and token:
            configuration = k8s_client.Configuration()
            configuration.host    = api_server
            configuration.api_key = {"authorization": f"Bearer {token}"}
            configuration.verify_ssl = bool(cfg.get("k8s_verify_ssl", True))
            k8s_client.Configuration.set_default(configuration)
        elif kubeconfig:
            k8s_config.load_kube_config(config_file=kubeconfig)
        else:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()
        return k8s_client.CoreV1Api()

    # ── Namespace / name resolution (auto-provision) ──────────────────────────

    def _resolve_cm_target(self, env: str) -> tuple[str, str]:
        """
        Return (namespace, cm_name) for the given env.

        For a new env the ConfigMap won't exist yet; _write() will create it
        automatically (create_namespaced_config_map on 404).
        """
        if self._dest_tpl:
            namespace = self._namespace
            cm_name   = _k8s_safe_name(
                resolve_destination(self._dest_tpl, tenant=self._tenant_id, env=env)
            )
        elif self._env_as_namespace:
            # Each env → its own namespace.  Namespace must be K8s-safe.
            namespace = _k8s_safe_name(env)
            cm_name   = self._cm_name
        elif self._env_as_name_suffix:
            namespace = self._namespace
            cm_name   = f"{self._cm_name}-{_k8s_safe_name(env)}"
        else:
            namespace = self._namespace
            cm_name   = self._cm_name

        return namespace, cm_name

    # ── Write ─────────────────────────────────────────────────────────────────

    def _write(
        self,
        *,
        flat_data: dict[str, str],
        sync_mode: SyncMode,
        datasource: str,
        env: str,
        repo: str,
        branch: str,
        commit_sha: str,
        file_path: str,
        labels: VarTrackLabels,
        prune: bool,
        prune_last: bool,
        prune_protection: list[str],
        dry_run_prune: bool,
        total_sources: int,
    ) -> dict[str, Any]:
        from kubernetes.client.exceptions import ApiException

        namespace, cm_name = self._resolve_cm_target(env)

        # Convert dot keys → configured format before building the CM.
        cm_data = format_keys(flat_data, self._key_format)

        body = self._k8s_client.V1ConfigMap(
            metadata=self._k8s_client.V1ObjectMeta(
                name=cm_name,
                namespace=namespace,
                labels=labels.as_k8s_labels(),
                annotations={
                    "vartrack.io/env":        env,
                    "vartrack.io/key-format": self._key_format.value,
                },
            ),
            data=cm_data,
        )

        try:
            existing = self._api.read_namespaced_config_map(cm_name, namespace)
            body.metadata.resource_version = existing.metadata.resource_version
            # ConfigMap exists — patch it.
            self._api.replace_namespaced_config_map(cm_name, namespace, body)
            logger.debug("ConfigmapSink: replaced %s/%s env=%s", namespace, cm_name, env)
        except ApiException as exc:
            if exc.status == 404:
                # ConfigMap (or namespace) doesn't exist yet — auto-provision.
                self._ensure_namespace(namespace)
                self._api.create_namespaced_config_map(namespace, body)
                logger.info(
                    "ConfigmapSink: created new ConfigMap %s/%s for env=%s",
                    namespace, cm_name, env,
                )
            elif exc.status == 403:
                raise RuntimeError(
                    f"ConfigmapSink: RBAC denied access to {namespace}/{cm_name} "
                    f"(403 Forbidden) — grant the orchestrator ServiceAccount "
                    f"'get,create,update' on ConfigMaps in namespace '{namespace}'"
                ) from None
            else:
                raise

        return {"written": len(flat_data), "pruned": 0}

    def _ensure_namespace(self, namespace: str) -> None:
        """Create the namespace if env_as_namespace is on and it doesn't exist."""
        if not self._env_as_namespace:
            return
        from kubernetes.client.exceptions import ApiException
        v1 = self._k8s_client.CoreV1Api()
        try:
            v1.read_namespace(namespace)
        except ApiException as exc:
            if exc.status == 404:
                v1.create_namespace(
                    self._k8s_client.V1Namespace(
                        metadata=self._k8s_client.V1ObjectMeta(name=namespace)
                    )
                )
                logger.info("ConfigmapSink: created namespace %s", namespace)
            else:
                raise

    def close(self) -> None:
        pass


def _k8s_safe_name(env: str) -> str:
    """
    Sanitise an env string for use as a K8s name/namespace.
    K8s names: lowercase alphanumeric and '-', max 63 chars.
    """
    import re
    safe = re.sub(r"[^a-z0-9\-]", "-", env.lower()).strip("-")
    return safe[:63] or "default"
