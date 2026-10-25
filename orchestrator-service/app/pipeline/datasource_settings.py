"""
app/pipeline/datasource_settings.py
───────────────────────────────────
A sink's connection settings from config.cue.

config.cue is where datasources are declared (bundle.datasources, in the
proto's field names); the sinks read flat, prefixed keys (mongo_uri,
s3_region, ssh_host, …) from their rule_config. Here the datasource's
config.cue block is translated to the sink's keys and laid over the rule.

Only config.cue sets these keys: rule_layers.py drops them from bundle.json
rules. A bundle with no rules of its own
uses bundle.json's rules as they are; then what config.cue says still wins,
and bundle.json supplies anything config.cue does not set.

Credential fields are SecretRefs: a plain string, {value}, {file} or {ref}.
They are resolved here, before the sink sees them — {ref} from the bundle's
secret manager — so sinks only ever get plain values.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ── config.cue, cached by modification time ─────────────────────────────────

_bundle_lock = threading.Lock()
_bundle_cache: tuple[str, float, dict] | None = None


def _bundle() -> dict:
    """The bundle in CONFIG_PATH, re-read when the file changes."""
    global _bundle_cache
    from app.config import _load_cue_bundle, settings

    path = settings.CONFIG_PATH
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _bundle_lock:
        if _bundle_cache and _bundle_cache[0] == path and _bundle_cache[1] == mtime:
            return _bundle_cache[2]
    bundle = _load_cue_bundle(path)
    with _bundle_lock:
        _bundle_cache = (path, mtime, bundle)
    return bundle


def find_datasource(bundle: dict, name: str) -> tuple[Optional[str], Optional[dict]]:
    """The (kind, config) of the datasource named *name*: "{kind}" or "{kind}-{tag}"."""
    for wrapper in bundle.get("datasources") or []:
        if not isinstance(wrapper, dict):
            continue
        for kind, cfg in wrapper.items():
            if not isinstance(cfg, dict):
                continue
            full = f"{kind}-{cfg['tag']}" if cfg.get("tag") else kind
            if full == name:
                return kind, cfg
    return None, None


# ── value conversions ────────────────────────────────────────────────────────

class _Secrets:
    """Resolves SecretRef values. A {ref} is read from the rule's
    secret_manager ("vault-prod"); a rule without one uses the entry tagged
    "default", else the bundle's only one."""

    def __init__(self, bundle: dict, datasource: str, manager: str = "") -> None:
        self._bundle = bundle
        self._datasource = datasource
        self._manager = manager
        self._refs: Any = None

    def __call__(self, value: Any, field: str) -> Optional[str]:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return None
        if "value" in value:
            return str(value["value"])
        if "file" in value:
            from app.utils.secret_file import read_secret_file
            try:
                return read_secret_file(value["file"], value.get("key"))
            except (OSError, ValueError) as exc:
                logger.warning("datasource %s: %s: cannot read %s: %s", self._datasource, field, value["file"], exc)
                return None
        if "ref" in value:
            managers = sorted(
                f"{kind}-{cfg['tag']}" if isinstance(cfg, dict) and cfg.get("tag") else kind
                for wrapper in self._bundle.get("secret_managers") or [] for kind, cfg in wrapper.items()
            )
            name = self._manager
            if not name:
                name = "vault-default" if "vault-default" in managers else (managers[0] if len(managers) == 1 else "")
            problem = ""
            if self._manager and name not in managers:
                problem = f"the rule's secret_manager {name!r} is not in the bundle's secret_managers ({', '.join(managers) or 'none'})"
            elif not name:
                problem = (f"the bundle has {len(managers)} secret managers ({', '.join(managers) or 'none'}); "
                           "name one with secret_manager on the rule, or tag one \"default\"")
            if problem:
                logger.warning("datasource %s: %s is a {ref}, but %s — the rule's value is used instead",
                               self._datasource, field, problem)
                return None
            try:
                if self._refs is None:
                    from app.ui.sessions import _SecretManagerRefs
                    self._refs = _SecretManagerRefs(self._bundle, name)
                return self._refs.get(value, f"datasource {self._datasource}: {field}")
            except Exception as exc:
                logger.warning("datasource %s: %s: %s", self._datasource, field, exc)
                return None
        return None


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}


def _seconds(value: Any) -> Optional[int]:
    """A proto Duration as CUE writes it ("30s", "1m30s") or a number, in whole seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        parts = _DURATION.findall(value)
        if parts and "".join(n + u for n, u in parts) == value.strip():
            return int(sum(float(n) * _UNIT_SECONDS[u] for n, u in parts))
    return None


def _mongo_uri(cfg: dict, secret: _Secrets) -> Optional[str]:
    if cfg.get("endpoint"):
        return cfg["endpoint"]
    host = cfg.get("host")
    if not host:
        return None
    user, password = cfg.get("username"), secret(cfg.get("password"), "password")
    creds = f"{user}:{password}@" if user and password else ""
    return f"mongodb://{creds}{host}:{cfg.get('port') or 27017}"


def _linux_file_path(cfg: dict) -> Optional[str]:
    paths = cfg.get("file_paths") or []
    if not paths:
        return None
    path, base = str(paths[0]), cfg.get("base_directory")
    if base and not path.startswith("/"):
        return base.rstrip("/") + "/" + path
    return path


def _ssh_key_path(cfg: dict, datasource: str) -> Optional[str]:
    key = (cfg.get("ssh_key") or {}).get("private_key")
    if isinstance(key, dict) and "file" in key and not key.get("key"):
        return key["file"]
    if key is not None:
        logger.warning("datasource %s: ssh_key.private_key must be {file: \"/path\"} — the sink reads the key "
                       "from a file; set ssh_key_path in the rule instead", datasource)
    return None


# ── config.cue field → sink setting, per datasource kind ─────────────────────
#
# Each entry is (sink key, how to get it): a config.cue field name, or a
# function of (config, secret resolver, datasource name). A value that comes
# out as None is not set, so the sink's default applies. These sink keys are
# the ones only config.cue sets (rule_layers.admin_only_keys).

Getter = Callable[[dict, _Secrets, str], Any]


def _field(name: str, convert: Callable[[Any], Any] = lambda v: v) -> Getter:
    return lambda cfg, secret, ds: convert(cfg[name]) if name in cfg else None


def _secret_field(name: str) -> Getter:
    return lambda cfg, secret, ds: secret(cfg[name], name) if name in cfg else None


_MAPPINGS: dict[str, list[tuple[str, Getter]]] = {
    "mongo": [
        ("mongo_uri",           lambda cfg, secret, ds: _mongo_uri(cfg, secret)),
        ("database",            _field("database")),
        ("collection",          _field("collection")),
        ("env_as_collection",   _field("env_as_collection")),
        ("update_strategy",     _field("update_strategy")),
        ("mongo_tls",           _field("ssl")),
        ("mongo_tls_ca_file",   _field("ssl_ca_path")),
        ("mongo_tls_insecure",  _field("ssl_allow_invalid_certificates")),
    ],
    "redis": [
        ("redis_hosts",          _field("hosts")),
        ("redis_db",             _field("database")),
        ("redis_password",       _secret_field("password")),
        ("redis_data_structure", _field("data_structure", lambda v: None if v == "DATA_STRUCTURE_UNSPECIFIED" else v)),
        ("redis_hash_key",       _field("hash_key")),
        ("redis_key_prefix",     _field("key_prefix")),
        ("redis_ttl_seconds",    _field("ttl", _seconds)),
        ("update_strategy",      _field("update_strategy")),
    ],
    "zookeeper": [
        ("zk_hosts",        _field("hosts")),
        ("zk_base_path",    _field("base_path")),
        ("zk_username",     _field("username")),
        ("zk_password",     _secret_field("password")),
        ("zk_timeout_secs", _field("session_timeout", _seconds)),
        ("update_strategy", _field("update_strategy")),
    ],
    "s3": [
        ("s3_bucket",            _field("bucket")),
        ("s3_region",            _field("region")),
        ("s3_endpoint_url",      _field("endpoint", lambda v: v or None)),
        ("s3_access_key_id",     _secret_field("access_key_id")),
        ("s3_secret_access_key", _secret_field("secret_access_key")),
        ("s3_key_prefix",        _field("prefix")),
        ("s3_one_file_per_key",  _field("one_file_per_key")),
        ("update_strategy",      _field("update_strategy")),
        ("s3_session_token",     _secret_field("session_token")),
        ("s3_sse",               _field("server_side_encryption")),
        ("s3_kms_key_id",        _field("kms_key_id")),
        ("s3_enable_versioning", _field("enable_versioning")),
        ("s3_force_path_style",  _field("force_path_style")),
        ("s3_content_type",      _field("content_type")),
        ("s3_file_pattern",      _field("file_pattern")),
        ("s3_timeout_secs",      _field("timeout", _seconds)),
        ("s3_verify_ssl",        _field("verify_ssl")),
        ("s3_ssl_ca_cert",       _field("ssl_ca_cert")),
    ],
    "configmap": [
        ("k8s_namespace",        _field("namespace")),
        ("k8s_configmap_name",   _field("configmap_name")),
        ("k8s_kubeconfig_path",  _field("kubeconfig_path")),
        ("k8s_api_server",       _field("api_server")),
        ("k8s_token",            _secret_field("token")),
        ("k8s_verify_ssl",       _field("insecure_skip_verify", lambda v: not v)),
        ("env_as_namespace",     _field("env_as_namespace")),
        ("env_as_name_suffix",   _field("env_as_name_suffix")),
    ],
    "helm": [
        ("helm_namespace",        _field("namespace")),
        ("helm_release_name",     _field("release_name")),
        ("helm_chart",            _field("chart_name")),
        ("helm_chart_version",    _field("chart_version")),
        ("helm_repo",             _field("chart_repo")),
        ("helm_upgrade_strategy", _field("upgrade_strategy",
                                         lambda v: None if v == "UPGRADE_STRATEGY_UNSPECIFIED" else str(v).lower())),
        ("helm_kubeconfig_path",  _field("kubeconfig_path")),
        ("helm_context",          _field("context")),
        ("helm_values_prefix",    _field("values_prefix")),
        ("helm_wait",             _field("wait")),
        ("helm_wait_timeout",     _field("wait_timeout")),
        ("helm_force",            _field("force")),
        ("helm_cleanup_on_fail",  _field("cleanup_on_fail")),
        ("helm_atomic",           _field("atomic")),
        ("helm_no_hooks",         _field("no_hooks")),
        ("helm_max_history",      _field("max_history")),
    ],
    "linux_server": [
        ("ssh_host",             _field("host")),
        ("ssh_port",             _field("port")),
        ("ssh_user",             _field("username")),
        ("ssh_password",         _secret_field("password")),
        ("ssh_key_path",         lambda cfg, secret, ds: _ssh_key_path(cfg, ds)),
        ("ssh_known_hosts_file", _field("known_hosts_file")),
        ("file_path",            lambda cfg, secret, ds: _linux_file_path(cfg)),
        ("env_as_dir",           _field("env_as_path")),
        ("ssh_host_key_verification",   _field("host_key_verification", lambda v: v or None)),
        ("ssh_connect_timeout_secs",    _field("connection_timeout", _seconds)),
        ("ssh_command_timeout_secs",    _field("command_timeout", _seconds)),
        ("ssh_max_retries",             _field("max_retries")),
        ("ssh_retry_backoff_secs",      _field("retry_backoff", _seconds)),
        ("ssh_compression",             _field("enable_compression")),
        ("ssh_use_sftp",                _field("use_sftp")),
        ("ssh_use_sudo",                _field("use_sudo")),
        ("ssh_sudo_password",           _secret_field("sudo_password")),
        ("file_mode",                   _field("file_mode", lambda v: v or None)),
        ("file_owner",                  _field("file_owner")),
        ("create_directories",          _field("create_directories")),
        ("backup_before_write",         _field("backup_before_write")),
        ("backup_suffix",               _field("backup_suffix", lambda v: v or None)),
        ("validation_command",          _field("validation_command")),
        ("rollback_on_validation_fail", _field("rollback_on_validation_fail")),
        ("post_write_commands",         _field("post_write_commands")),
        ("continue_on_command_error",   _field("continue_on_command_error")),
    ],
    "vercel": [
        ("vercel_token",      _secret_field("token")),
        ("vercel_project_id", _field("project_id")),
        ("vercel_team_id",    _field("team_id")),
        ("vercel_targets",    _field("targets")),
        ("vercel_api_url",    _field("api_url")),
    ],
}


def sink_settings(kind: str, cfg: dict, datasource: str, bundle: dict, manager: str = "") -> dict[str, Any]:
    """The sink keys a config.cue datasource block sets. Unset fields are left
    out. *manager* is the rule's secret_manager, for {ref} credentials."""
    secret = _Secrets(bundle, datasource, manager)
    out: dict[str, Any] = {}
    for key, get in _MAPPINGS.get(kind, []):
        value = get(cfg, secret, datasource)
        if value is not None:
            out[key] = value
    return out


def with_datasource_settings(datasource: str, rule_config: dict) -> dict:
    """*rule_config* with the datasource's config.cue settings laid over it."""
    bundle = _bundle()
    if not bundle:
        return rule_config
    kind, cfg = find_datasource(bundle, datasource)
    if cfg is None:
        return rule_config
    settings = sink_settings(kind, cfg, datasource, bundle, str(rule_config.get("secret_manager") or ""))
    overridden = sorted(k for k, v in settings.items() if k in rule_config and rule_config[k] != v)
    if overridden:
        logger.info("datasource %s: config.cue overrides the rule's %s", datasource, ", ".join(overridden))
    return {**rule_config, **settings}
