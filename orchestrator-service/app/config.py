"""
app/config.py
──────────────
Single source of truth for every environment-variable setting.

Resolution order (last wins):
  1. Env vars — always read as the baseline / fallback
  2. CUE bundle at CONFIG_PATH — overrides supported fields

The CUE bundle is parsed with:
  vartrack-bundle export <CONFIG_PATH>  →  the bundle as JSON (see _load_cue_bundle)

Celery broker / result-backend
───────────────────────────────
The operator declares which datasource acts as the Celery broker and
which acts as the result backend via global_tags in the CUE bundle:

  bundle: {
    global_tags: {
      celery_broker:  "redis"          // tag of the datasource to use as broker
      celery_backend: "mongo-results"  // tag of the datasource to use as backend
    }
    datasources: [
      { redis: { tag: "redis", hosts: ["localhost:6379"], ... } }
      { mongo: { tag: "mongo-results", host: "localhost", port: 27017, ... } }
    ]
  }

The loader resolves the tagged datasource and builds the correct URI
(redis://, mongodb://) automatically.  If global_tags are absent,
CELERY_BROKER_URL and CELERY_RESULT_BACKEND fall back to env vars.

CUE → settings mappings
────────────────────────
  global_tags["celery_broker"]        → CELERY_BROKER_URL   (via datasource tag)
  global_tags["celery_backend"]       → CELERY_RESULT_BACKEND (via datasource tag)
  global_tags["git_cache_dir"]        → GIT_CACHE_DIR
  global_tags["git_cache_max_entries"]→ GIT_CACHE_MAX_ENTRIES
  global_tags["schema_cache_dir"]     → SCHEMA_CACHE_DIR  (Path)
  global_tags["schema_ttl"]           → SCHEMA_TTL_SECONDS (int)

HOST and PORT are derived from ORCHESTRATOR_HTTP_ADDR.
DEBUG and CORS_ORIGINS are env-var-only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── .env loader ───────────────────────────────────────────────────────────────
# Must run before _Settings class body, which calls os.getenv() at definition
# time.  Mirrors the Go services' loadDotEnv() / EnvOr() pattern:
#   - never overrides a real environment variable (os.environ.setdefault)
#   - skips blank lines and # comments
#   - strips inline comments and surrounding quotes

def _load_dotenv() -> None:
    """Load a .env file without overriding existing environment variables."""
    candidates: list[str] = []

    # 1. Explicit override via ENV_FILE
    env_file = os.environ.get("ENV_FILE", "")
    if env_file:
        candidates.append(env_file)

    # 2. .env in current working directory
    candidates.append(str(Path.cwd() / ".env"))

    # 3. Walk up from this file's directory to find the repo root .env
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        candidates.append(str(parent / ".env"))

    for path in candidates:
        if not path or not Path(path).is_file():
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    # Strip inline comment
                    if " #" in value:
                        value = value[: value.index(" #")].rstrip()
                    # Strip surrounding quotes
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                        value = value[1:-1]
                    if key:
                        os.environ.setdefault(key, value)
        except OSError:
            pass
        else:
            logger.debug("config: loaded env from %s", path)
            break  # stop at first successfully loaded file


_load_dotenv()


# ── CUE bundle loader ─────────────────────────────────────────────────────────

class BundleError(RuntimeError):
    """config.cue cannot be loaded: it is not valid, a service would refuse it, or the
    program that reads it is missing. The message says why."""


def _bundle_binary() -> str:
    return os.getenv("VARTRACK_BUNDLE_BINARY", "") or shutil.which("vartrack-bundle") or ""


def _load_cue_bundle(config_path: str) -> dict[str, Any]:
    """
    The bundle in the CUE file at *config_path*, as the gateway and the watcher read it.

    config.cue is read once, by one program (``vartrack-bundle``, built from libs/go/bundle):
    the same CUE evaluation, shorthands, renamed options and strict parse against the protos
    those services use. This is the bundle object it prints, with its fields under the proto's
    names. A file it refuses is refused by every service, so this process never works from a
    reading the gateway would not accept. It raises BundleError when the file cannot be loaded;
    it never answers with an empty bundle.
    """
    binary = _bundle_binary()
    if not binary:
        raise BundleError(
            "vartrack-bundle is not installed (or set VARTRACK_BUNDLE_BINARY): it reads config.cue for "
            "the orchestrator exactly as the gateway does. Build it with `go build ./cmd/vartrack-bundle` "
            "in libs/go; the orchestrator image ships it")
    try:
        result = subprocess.run([binary, "export", config_path], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise BundleError(f"vartrack-bundle timed out reading {config_path}") from exc
    except OSError as exc:
        raise BundleError(f"vartrack-bundle cannot run: {exc}") from exc
    if result.returncode != 0:
        raise BundleError(f"{config_path}: {result.stderr.strip() or 'vartrack-bundle failed'}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BundleError(f"vartrack-bundle printed invalid JSON for {config_path}: {exc}") from exc
    # The protos this process carries must accept it too: a vartrack-bundle built from other
    # protos than these stubs is a mismatched install, and it is better said here than found later.
    try:
        from google.protobuf import json_format
        from vartrack.v1.models import bundle_pb2

        json_format.Parse(result.stdout, bundle_pb2.Bundle(), ignore_unknown_fields=False)
    except ImportError:
        logger.warning("the protobuf stubs are not generated: %s was not checked against them", config_path)
    except json_format.ParseError as exc:
        raise BundleError(f"{config_path} does not match this orchestrator's protos "
                          f"(is vartrack-bundle from the same release?): {exc}") from exc
    return data


def load_bundle(config_path: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """The bundle at *config_path*. For a bundle that is re-read while the service runs: when it
    cannot be loaded now, *previous* (the last good one) keeps serving, said at ERROR and shown by
    readiness as stale, so a bad edit does not stop syncs. With no earlier reading there is nothing
    to keep, and it raises BundleUnavailable: an empty bundle would read as "no rules, no
    datasources, no secret managers", which is a different thing from "cannot be read"."""
    from app import dependencies

    try:
        bundle = _load_cue_bundle(config_path)
    except BundleError as exc:
        if previous is not None:
            logger.error("config.cue cannot be loaded, keeping what was loaded before: %s", exc)
            dependencies.stale("config.cue", str(exc))
            return previous
        dependencies.down("config.cue", str(exc))
        raise dependencies.BundleUnavailable(str(exc)) from exc
    dependencies.up("config.cue")
    return bundle


# ── Datasource URI builders ───────────────────────────────────────────────────

def _find_datasource_by_name(bundle: dict[str, Any], name: str) -> tuple[str | None, dict[str, Any] | None]:
    """
    Return (kind, cfg) for a datasource identified by its full name ({type}-{tag}).

    e.g. "redis-broker" → kind="redis", cfg={"tag": "broker", "hosts": [...], ...}
         "redis"        → kind="redis", cfg={"tag": "", ...}
    """
    if "-" in name:
        kind, tag = name.split("-", 1)
    else:
        kind, tag = name, ""
    for ds in bundle.get("datasources", []):
        if not isinstance(ds, dict):
            continue
        cfg = ds.get(kind)
        if isinstance(cfg, dict) and cfg.get("tag", "") == tag:
            return kind, cfg
    return None, None


def _find_datasource_by_tag(bundle: dict[str, Any], tag: str) -> tuple[str | None, dict[str, Any] | None]:
    """
    Return the inner config dict of the datasource whose `tag` field matches.

    bundle.datasources is a list of oneof wrappers, e.g.:
      [{"redis": {"tag": "redis", ...}}, {"mongo": {"tag": "mongo-results", ...}}]

    Returns the inner dict (e.g. the RedisConfig or MongoConfig) and its kind,
    or None if not found.
    """
    for ds in bundle.get("datasources", []):
        if not isinstance(ds, dict):
            continue
        for kind, cfg in ds.items():
            if isinstance(cfg, dict) and cfg.get("tag") == tag:
                return kind, cfg
    return None, None


def _uri_from_datasource(kind: str, cfg: dict[str, Any]) -> str | None:
    """Build a broker/backend URI from a datasource config dict."""
    if kind == "redis":
        # RedisConfig: hosts = ["host:port", ...], database int, password SecretRef
        hosts = cfg.get("hosts", [])
        host_str = hosts[0] if hosts else "localhost:6379"
        db = cfg.get("database", 0)
        password = _resolve_secret_ref(cfg.get("password"))
        auth = f":{password}@" if password else ""
        return f"redis://{auth}{host_str}/{db}"

    if kind == "mongo":
        # MongoConfig: endpoint (full URI) or host + port + optional creds
        endpoint = cfg.get("endpoint")
        if endpoint:
            return endpoint
        host = cfg.get("host", "localhost")
        port = cfg.get("port", 27017)
        user = cfg.get("username", "")
        pwd  = cfg.get("password", "")
        creds = f"{user}:{pwd}@" if user and pwd else ""
        db    = cfg.get("database", "celery")
        return f"mongodb://{creds}{host}:{port}/{db}"

    logger.warning("No URI builder for datasource kind %r", kind)
    return None


def _resolve_secret_ref(ref: Any) -> str | None:
    """
    Resolve a SecretRef at config-load time.

    Supported forms::

        token: "ghp_xxx"                     # plain string
        token: {file: "/run/secrets/token"}  # read from file (whitespace stripped)
        token: {file: "/run/secrets/app.env", key: "TOKEN"}  # one key of that file
        token: {ref: "path#key"}             # Vault ref — resolved at task runtime

    Returns None for external refs (resolved later in the ETL pipeline).
    """
    if ref is None:
        return None
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict):
        if "file" in ref:
            path = ref["file"]
            from app.utils.secret_file import read_secret_file
            try:
                return read_secret_file(path, ref.get("key"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"SecretRef: cannot read file {path!r}: {exc}") from exc
        # Legacy {value: "..."} form — still accepted
        if "value" in ref:
            return ref["value"]
        # External ref {ref: "path#key"} — resolved at task runtime
        return None
    return None


# ── Bundle overrides extractor ────────────────────────────────────────────────

def _bundle_overrides(bundle: dict[str, Any]) -> dict[str, Any]:
    """
    Extract all CUE-sourced overrides.
    Returns a dict whose keys match _Settings attribute names.
    """
    overrides: dict[str, Any] = {}

    # protojson serialises map<string,string> as camelCase "globalTags".
    tags: dict[str, str] = bundle.get("globalTags", bundle.get("global_tags", {})) or {}

    # ── Celery broker ─────────────────────────────────────────────────────────
    # protojson serialises celery_broker_datasource as celeryBrokerDatasource.
    broker_tag = bundle.get("celeryBrokerDatasource") or bundle.get("celery_broker_datasource")
    if broker_tag:
        kind, cfg = _find_datasource_by_name(bundle, broker_tag)
        if kind is not None and cfg is not None:
            uri = _uri_from_datasource(kind, cfg)
            if uri:
                overrides["CELERY_BROKER_URL"] = uri
        else:
            logger.warning(
                "celery_broker_datasource=%r — no datasource with that tag found",
                broker_tag,
            )

    # ── Celery result backend ─────────────────────────────────────────────────
    backend_tag = bundle.get("celeryBackendDatasource") or bundle.get("celery_backend_datasource")
    if backend_tag:
        kind, cfg = _find_datasource_by_name(bundle, backend_tag)
        if kind is not None and cfg is not None:
            uri = _uri_from_datasource(kind, cfg)
            if uri:
                overrides["CELERY_RESULT_BACKEND"] = uri
        else:
            logger.warning(
                "celery_backend_datasource=%r — no datasource with that tag found",
                backend_tag,
            )

    # ── Git clone cache ───────────────────────────────────────────────────────
    git_dir = tags.get("git_cache_dir")
    if git_dir:
        overrides["GIT_CACHE_DIR"] = git_dir

    git_max = tags.get("git_cache_max_entries")
    if git_max is not None:
        try:
            overrides["GIT_CACHE_MAX_ENTRIES"] = int(git_max)
        except (ValueError, TypeError):
            logger.warning("global_tags.git_cache_max_entries is not an integer: %r", git_max)

    # ── Schema registry ───────────────────────────────────────────────────────
    schema_dir = tags.get("schema_cache_dir")
    if schema_dir:
        overrides["SCHEMA_CACHE_DIR"] = Path(schema_dir)

    schema_ttl = tags.get("schema_ttl")
    if schema_ttl is not None:
        try:
            overrides["SCHEMA_TTL_SECONDS"] = int(schema_ttl)
        except (ValueError, TypeError):
            logger.warning("global_tags.schema_ttl is not an integer: %r", schema_ttl)

    return overrides


def _secret_env(name: str) -> str:
    """A secret from the environment: the variable itself, else the contents of
    the file named by <name>_FILE (trailing whitespace removed). "" when neither
    is set. A file that is named but cannot be read is an error, never an empty
    secret: failing open would silently turn authentication off."""
    value = os.getenv(name, "").strip()
    if value:
        return value
    path = os.getenv(f"{name}_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"{name}_FILE={path!r} cannot be read: {exc}") from exc


# ── Settings ──────────────────────────────────────────────────────────────────

class _Settings:
    # ── HTTP server ───────────────────────────────────────────────────────────
    # HOST and PORT are split from ORCHESTRATOR_HTTP_ADDR. (ORCHESTRATOR_ADDR was this listener's
    # address too, while for the gateway and the watcher it is the orchestrator's gRPC address to
    # dial: a shared environment file made one value mean two things. It is still read here for one
    # release, and named when it is.)
    _addr: str              = os.getenv("ORCHESTRATOR_HTTP_ADDR") or os.getenv("ORCHESTRATOR_ADDR") or "0.0.0.0:8000"
    if not os.getenv("ORCHESTRATOR_HTTP_ADDR") and os.getenv("ORCHESTRATOR_ADDR"):
        logger.warning("ORCHESTRATOR_ADDR is read as the HTTP listen address here, but it is the orchestrator's "
                       "gRPC address for the gateway and the watcher: set ORCHESTRATOR_HTTP_ADDR instead "
                       "(ORCHESTRATOR_ADDR will stop being read here in the next release)")
    _host, _, _port         = _addr.rpartition(":")
    HOST: str               = _host or "0.0.0.0"
    PORT: int               = int(_port) if _port else 8000
    DEBUG: bool             = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
    # Default to empty allowlist — no origins permitted unless explicitly set.
    # Operators must set CORS_ORIGINS="https://app.example.com" (or "*" for open access).
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]

    # ── Celery (CUE bundle resolves these from tagged datasources) ────────────
    CELERY_BROKER_URL: str      = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND: str  = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

    # ── Schema registry ───────────────────────────────────────────────────────
    SCHEMA_CACHE_DIR: Path    = Path(os.getenv("SCHEMA_CACHE_DIR", "/tmp/schema_registry"))
    SCHEMA_TTL_SECONDS: int   = int(os.getenv("SCHEMA_TTL_SECONDS", "300"))

    # ── Git clone cache (CUE bundle overrides via global_tags) ────────────────
    GIT_CACHE_DIR: str         = os.getenv("GIT_CACHE_DIR", "/tmp/vt_gitcache")
    GIT_CACHE_MAX_ENTRIES: int = int(os.getenv("GIT_CACHE_MAX_ENTRIES", "20"))

    # ── CUE bundle path (shared with the Go gateway) ──────────────────────────
    CONFIG_PATH: str = os.getenv("CONFIG_PATH", "")

    # ── Admin server (separate port for health / metrics / pprof) ────────────
    # ADMIN_PORT is intentionally a different port from PORT (8000) so
    # Prometheus scrape and health probes never share bandwidth with webhook traffic.
    # Loopback by default: the admin listener is for the host's own probes. Publish
    # it only deliberately (ADMIN_HOST) and then guard it with INTERNAL_AUTH_SECRET.
    ADMIN_HOST: str = os.getenv("ADMIN_HOST", "127.0.0.1")
    ADMIN_PORT: int = int(os.getenv("ADMIN_PORT", "9090"))

    # ── TLS / mTLS ────────────────────────────────────────────────────────────
    # MTLS_ENABLED=false (default) → plain HTTP/gRPC; no certs needed.
    #   Safe for local development and unit tests.
    # MTLS_ENABLED=true + TLS_CERT_FILE/TLS_KEY_FILE → TLS on HTTP and gRPC.
    # TLS_CA_FILE set → also require and verify client certificates (full mTLS).
    # TLS_SELF_SIGNED=true → generate a self-signed cert on startup (dev/CI).
    #
    # Mirrors gateway env vars: GRPC_TLS_CA, GRPC_TLS_CERT, GRPC_TLS_KEY,
    # GATEWAY_TLS_CERT, GATEWAY_TLS_KEY; plus IsProduction() → MTLS_ENABLED.
    MTLS_ENABLED:   bool = os.getenv("MTLS_ENABLED",  "").lower() in ("1", "true", "yes")
    TLS_CERT_FILE:  str  = os.getenv("TLS_CERT_FILE",  "")
    TLS_KEY_FILE:   str  = os.getenv("TLS_KEY_FILE",   "")
    TLS_CA_FILE:    str  = os.getenv("TLS_CA_FILE",    "")
    TLS_SELF_SIGNED: bool = os.getenv("TLS_SELF_SIGNED", "").lower() in ("1", "true", "yes")

    # ── Environment ───────────────────────────────────────────────────────────
    APP_ENV: str = os.getenv("APP_ENV", "development")

    # ── Internal authentication ───────────────────────────────────────────────
    # Every call to the orchestrator's HTTP API, gRPC port and admin listener must
    # carry a signed grant (app/api/tokens.py). The gateway mints the grants for
    # webhooks and CLI calls, the watcher mints its own, and an operator mints
    # one for the admin scrape.
    #
    #   INTERNAL_AUTH_SECRET          signs/verifies sync grants (min 32 chars);
    #                                 also the fallback for the two below
    #   INTERNAL_AUTH_WATCH_SECRET    optional separate key for the watcher
    #   INTERNAL_AUTH_ADMIN_SECRET    optional separate key for the admin listener
    #   INTERNAL_AUTH_AUTHZ_SECRET    optional separate key for the questions this
    #                                 service asks the gateway's permission engine
    #   <each>_FILE                   read the secret from a file instead
    #   INTERNAL_AUTH_INSECURE=true   the explicit development switch: run with
    #                                 no verification. Production refuses to start
    #                                 without a secret unless this is set.
    INTERNAL_AUTH_SECRET:       str  = _secret_env("INTERNAL_AUTH_SECRET")
    INTERNAL_AUTH_WATCH_SECRET: str  = _secret_env("INTERNAL_AUTH_WATCH_SECRET")
    INTERNAL_AUTH_ADMIN_SECRET: str  = _secret_env("INTERNAL_AUTH_ADMIN_SECRET")
    INTERNAL_AUTH_AUTHZ_SECRET: str  = _secret_env("INTERNAL_AUTH_AUTHZ_SECRET")
    INTERNAL_AUTH_INSECURE:     bool = os.getenv("INTERNAL_AUTH_INSECURE", "").lower() in ("1", "true", "yes")

    # ── The permission engine ─────────────────────────────────────────────────
    # What the signed-in web UI user may see and do is decided by the gateway's
    # permission engine, the one the CLI API asks too; this service only asks and
    # obeys (app/authz_client.py). With sign-in on and no engine address, the UI
    # answers 503 rather than deciding for itself.
    #
    #   GATEWAY_AUTHZ_ADDR     host:port of the gateway's AUTHZ_ADDR listener
    #   GATEWAY_AUTHZ_CA_FILE  CA that signed the gateway's certificate (TLS_CA_FILE
    #                          when unset); client certificate: TLS_CERT_FILE/TLS_KEY_FILE
    #   GATEWAY_AUTHZ_TIMEOUT  seconds to wait for an answer
    GATEWAY_AUTHZ_ADDR:    str   = os.getenv("GATEWAY_AUTHZ_ADDR", "")
    GATEWAY_AUTHZ_CA_FILE: str   = os.getenv("GATEWAY_AUTHZ_CA_FILE", "")
    GATEWAY_AUTHZ_TIMEOUT: float = float(os.getenv("GATEWAY_AUTHZ_TIMEOUT", "10"))

    # ── Build / monitoring metadata ───────────────────────────────────────────
    APP_VERSION: str = os.getenv("APP_VERSION", "dev")
    GIT_COMMIT:  str = os.getenv("GIT_COMMIT",  "unknown")

    def __init__(self) -> None:
        self._apply_cue_overrides()
        # In production, require mTLS unless MTLS_ENABLED is explicitly set
        # to "false" (escape hatch).
        if self.APP_ENV == "production" and not os.getenv("MTLS_ENABLED"):
            self.MTLS_ENABLED = True
            logger.info("production mode: MTLS_ENABLED forced to True")

    def _apply_cue_overrides(self) -> None:
        """Parse the CUE bundle (if CONFIG_PATH is set) and apply overrides."""
        config_path = self.CONFIG_PATH
        if not config_path:
            return

        logger.info("Applying CUE bundle overrides from: %s", config_path)
        try:
            if not Path(config_path).exists():
                raise BundleError(f"CONFIG_PATH={config_path!r} does not exist")
            bundle = _load_cue_bundle(config_path)
        except BundleError as exc:
            from app import dependencies

            # A CONFIG_PATH that is set but cannot be read is not "no config": the service does
            # not start on environment variables alone unless the operator said it may.
            if self.APP_ENV == "production" or not dependencies.allowed("config"):
                raise
            logger.error("config.cue cannot be loaded, using environment variables only "
                         "(VARTRACK_ALLOW_DEGRADED=config): %s", exc)
            dependencies.down("config.cue", str(exc))
            dependencies.mode_on("config", str(exc))
            return
        if not bundle:
            return

        _SENSITIVE = frozenset({"password", "secret", "token", "key", "passwd", "credential"})
        _URL_CREDS = re.compile(r'(://[^:@/\s]+:)[^@/\s]+(@)', re.IGNORECASE)

        def _scrub(v: object) -> str:
            """Return a sanitized string — always a fresh str so taint is broken."""
            raw = str(v)
            return str(_URL_CREDS.sub(r'\1***\2', raw))

        for attr, value in _bundle_overrides(bundle).items():
            old = getattr(self, attr, None)
            setattr(self, attr, value)
            is_sensitive = any(s in attr.lower() for s in _SENSITIVE)
            safe_val: str = "***" if is_sensitive else _scrub(value)
            safe_old: str = "***" if is_sensitive else _scrub(old)
            logger.info("CUE override  %s = %r  (was %r)", attr, safe_val, safe_old)


settings = _Settings()