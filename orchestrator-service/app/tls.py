"""
app/tls.py
───────────
TLS / mTLS support for the orchestrator HTTP and gRPC servers.

Modes (controlled by env vars):
  MTLS_ENABLED=false (default) → plain HTTP/gRPC, no SSL.  Safe for local dev
                                  and unit tests without any cert setup.
  MTLS_ENABLED=true            → TLS on both uvicorn HTTP and gRPC listeners.
    TLS_CERT_FILE + TLS_KEY_FILE → load server cert from disk (production).
    TLS_SELF_SIGNED=true         → generate a self-signed cert (staging / CI).
    TLS_CA_FILE                  → also require and verify client certs (mTLS).

Self-signed cert:
  Generated via `openssl req -x509` subprocess (no `cryptography` dep needed).
  Valid for localhost, 127.0.0.1, and the host's LAN IPs + POD_IP (Kubernetes).
  Lifetime: 24 hours — must be regenerated on restart (dev only).

TLS minimum version: 1.2.  TLS 1.3 is preferred automatically by the ssl module.
"""
from __future__ import annotations

import logging
import os
import socket
import ssl
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)


# ── Public types ──────────────────────────────────────────────────────────────

class TLSBundle(NamedTuple):
    """Resolved paths to cert, key, and optionally CA for client verification."""
    cert_file: str
    key_file:  str
    ca_file:   str | None   # None = server-only TLS; set = full mTLS


# ── Entry points ──────────────────────────────────────────────────────────────

def build_ssl_context() -> ssl.SSLContext | None:
    """
    Return an ssl.SSLContext for uvicorn, or None for plain HTTP.

    None is returned when MTLS_ENABLED is false/unset — safe for local dev
    and test environments that do not have certificates available.
    """
    from app.config import settings

    if not settings.MTLS_ENABLED:
        logger.debug("mTLS disabled (MTLS_ENABLED not set) — plain HTTP")
        return None

    bundle = _resolve_bundle(settings)
    if bundle is None:
        msg = "MTLS_ENABLED=true but no cert available — set TLS_CERT_FILE/TLS_KEY_FILE or TLS_SELF_SIGNED=true"
        if getattr(settings, "APP_ENV", "") == "production":
            raise RuntimeError(msg)
        logger.warning("%s — falling back to plain HTTP", msg)
        return None

    ctx = ssl.SSLContext(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(bundle.cert_file, bundle.key_file)

    if bundle.ca_file:
        ctx.load_verify_locations(bundle.ca_file)
        ctx.verify_mode = ssl.CERT_REQUIRED
        logger.info("mTLS: client certificate required, ca=%s", bundle.ca_file)
    else:
        ctx.verify_mode = ssl.CERT_NONE
        logger.info("TLS: server-side only (no client cert required), cert=%s", bundle.cert_file)

    return ctx


def build_grpc_server_credentials():
    """
    Return grpc.ServerCredentials for the gRPC listener, or None for insecure.

    None → `server.add_insecure_port()`  (local dev / test)
    creds → `server.add_secure_port()`   (production / staging)
    """
    try:
        import grpc
    except ImportError:
        return None

    from app.config import settings

    if not settings.MTLS_ENABLED:
        logger.debug("gRPC: insecure (MTLS_ENABLED not set)")
        return None

    bundle = _resolve_bundle(settings)
    if bundle is None:
        logger.warning("MTLS_ENABLED=true but no cert available — gRPC falling back to insecure")
        return None

    cert_pem = Path(bundle.cert_file).read_bytes()
    key_pem  = Path(bundle.key_file).read_bytes()

    if bundle.ca_file:
        ca_pem = Path(bundle.ca_file).read_bytes()
        creds = grpc.ssl_server_credentials(
            [(key_pem, cert_pem)],
            root_certificates=ca_pem,
            require_client_auth=True,
        )
        logger.info("gRPC mTLS: client cert required, ca=%s", bundle.ca_file)
    else:
        creds = grpc.ssl_server_credentials([(key_pem, cert_pem)])
        logger.info("gRPC TLS: server-only, cert=%s", bundle.cert_file)

    return creds


# ── Internal helpers ──────────────────────────────────────────────────────────

def _resolve_bundle(settings) -> TLSBundle | None:
    """
    Resolve cert/key paths from settings, generating self-signed if requested.
    Returns None when neither files nor self-signed are configured.
    """
    cert = getattr(settings, "TLS_CERT_FILE", "")
    key  = getattr(settings, "TLS_KEY_FILE",  "")
    ca   = getattr(settings, "TLS_CA_FILE",   "") or None
    self_signed = getattr(settings, "TLS_SELF_SIGNED", False)

    if cert and key:
        if not Path(cert).exists():
            logger.error("TLS_CERT_FILE=%r not found", cert)
            return None
        if not Path(key).exists():
            logger.error("TLS_KEY_FILE=%r not found", key)
            return None
        return TLSBundle(cert_file=cert, key_file=key, ca_file=ca or None)

    if self_signed:
        return _generate_self_signed(ca)

    return None


def _generate_self_signed(ca_file: str | None = None) -> TLSBundle | None:
    """
    Generate a self-signed ECDSA P-256 certificate via `openssl` subprocess.

    Valid for localhost, 127.0.0.1, all LAN IPs, and POD_IP (Kubernetes).
    Lifetime: 24 hours.  Written to a temp directory that lives for the
    process lifetime (Python cleans it up on interpreter exit).

    Returns None if openssl is not available.
    """
    tmpdir = tempfile.mkdtemp(prefix="vt_tls_")
    cert_file = os.path.join(tmpdir, "server.crt")
    key_file  = os.path.join(tmpdir, "server.key")

    # Build SAN list: localhost + all non-loopback IPs + POD_IP
    san_ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("::"):
                san_ips.add(ip)
    except Exception:
        pass
    if pod_ip := os.getenv("POD_IP"):
        san_ips.add(pod_ip)

    san = "DNS:localhost," + ",".join(f"IP:{ip}" for ip in sorted(san_ips))
    subject = "/O=vartrack-orchestrator/CN=localhost"

    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-nodes",
                "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                "-keyout", key_file,
                "-out",    cert_file,
                "-days",   "1",
                "-subj",   subject,
                "-addext", f"subjectAltName={san}",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except FileNotFoundError:
        logger.warning("openssl not found — cannot generate self-signed cert; falling back to plain HTTP")
        return None
    except subprocess.CalledProcessError as exc:
        logger.error("openssl self-signed cert generation failed: %s", exc.stderr.decode())
        return None

    logger.warning(
        "using auto-generated self-signed TLS certificate (not for production), san=%s", san
    )
    return TLSBundle(cert_file=cert_file, key_file=key_file, ca_file=ca_file or None)
