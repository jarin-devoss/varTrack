"""
app/pipeline/sinks/linux_server.py
────────────────────────────────────
Linux server / plain-file sink.

Writes config data to files on a local (or SSH-mounted) filesystem.
Supported file formats: .env  .json  .properties  .ini  .yaml

Key format: DOT (default) or FLAT
──────────────────────────────────
  "dot"  (default)  "db.host.port"  →  key as-is  (JSON / YAML fields, .env key)
  "flat"            "db.host.port"  →  "db_host_port"  (.properties / .ini key)

SLASH and SLASH_DOT are not supported for file sinks.
rule.proto SinkKeyFormat.linux_server enforces this at validation time.

Auto-provision
──────────────
When the env is new (e.g. "pr-42" the first time), the sink creates a new
file automatically.  Two naming strategies:

  env_as_filename: true  (default)
    The env is embedded in the filename.
    template: "{base}_{env}.{ext}"
    e.g.   /etc/myapp/config_pr-42.env
           /etc/myapp/config_v1.2.3.json

  env_as_dir: true
    The env becomes a sub-directory.
    e.g.   /etc/myapp/pr-42/config.env

The parent directory is created automatically (os.makedirs).
If the file already exists it is overwritten (upsert semantics).

SSH support
───────────
Set rule_config["ssh_host"] to write to a remote server via Paramiko.
When ssh_host is absent the sink writes to the local filesystem.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import (
    KeyFormat, format_keys, key_format_from_str, validate_sink_format,
    resolve_destination,
)
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_SINK_KIND      = "linux_server"
_DEFAULT_FORMAT = KeyFormat.DOT


class LinuxServerSink(BaseSink):

    def __init__(
        self,
        *,
        name: str = "linux_server",
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        cfg = rule_config or {}

        self._base_path       = Path(cfg.get("file_path", "/etc/vartrack/config.env"))
        self._tenant_id       = tenant_id
        self._env_as_filename = bool(cfg.get("env_as_filename", True))
        self._env_as_dir      = bool(cfg.get("env_as_dir", False))
        # destination_template: full file path template, e.g. "/etc/myapp/{env}/config.env".
        # Takes precedence over file_path + env_as_filename/env_as_dir when set.
        self._dest_tpl  = cfg.get("destination_template", "")
        self._file_format     = cfg.get("file_format", self._base_path.suffix.lstrip(".") or "env")

        fmt_name         = cfg.get("key_format", "dot")
        self._key_format = key_format_from_str(fmt_name)
        validate_sink_format(_SINK_KIND, self._key_format)

        # Optional SSH transport
        ssh_host = cfg.get("ssh_host")
        self._ssh = _SshTransport(cfg) if ssh_host else None

        logger.debug(
            "LinuxServerSink ready path=%s fmt=%s ssh=%s",
            self._base_path, self._key_format.value, bool(self._ssh),
        )

    # ── Path resolution (auto-provision) ─────────────────────────────────────

    def _resolve_path(self, env: str) -> Path:
        """
        Resolve the target file path for this env.

        For a new env this path won't exist yet; _write() will create it
        (os.makedirs + open for write).
        """
        base_dir  = self._base_path.parent
        base_stem = self._base_path.stem
        base_ext  = self._base_path.suffix  # ".env", ".json" …

        safe_env = _safe_filename(env)

        if self._dest_tpl:
            return Path(resolve_destination(self._dest_tpl, tenant=self._tenant_id, env=safe_env))

        if self._env_as_dir:
            # /etc/myapp/pr-42/config.env
            return base_dir / safe_env / f"{base_stem}{base_ext}"
        elif self._env_as_filename:
            # /etc/myapp/config_pr-42.env
            return base_dir / f"{base_stem}_{safe_env}{base_ext}"
        else:
            # Single shared file — env only in content labels
            return self._base_path

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
        target = self._resolve_path(env)
        formatted = format_keys(flat_data, self._key_format)
        content   = _render(formatted, self._file_format, labels, env)

        if self._ssh:
            self._ssh.write(str(target), content)
            logger.info("LinuxServerSink: SSH wrote %s env=%s", target, env)
        else:
            # Auto-provision: create parent directories if they don't exist.
            target.parent.mkdir(parents=True, exist_ok=True)
            is_new = not target.exists()
            target.write_text(content, encoding="utf-8")
            if is_new:
                logger.info("LinuxServerSink: created new file %s for env=%s", target, env)
            else:
                logger.debug("LinuxServerSink: updated %s env=%s", target, env)

        return {"written": len(flat_data), "path": str(target)}

    def close(self) -> None:
        if self._ssh:
            self._ssh.close()


# ── Renderers ─────────────────────────────────────────────────────────────────

def _render(data: dict[str, str], fmt: str, labels: VarTrackLabels, env: str) -> str:
    """Serialise flat_data to the target file format."""
    header = f"# vartrack managed — env={env} tenant={labels.tenant_id}\n"

    if fmt in ("json",):
        # Reconstruct nested dict from dot-separated keys for clean JSON output.
        nested = _unflatten(data)
        return json.dumps(nested, indent=2, sort_keys=True) + "\n"

    if fmt in ("yaml", "yml"):
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "pyyaml is required for YAML file output: pip install pyyaml"
            ) from exc
        nested = _unflatten(data)
        return f"# vartrack managed — env={env}\n" + yaml.dump(nested, default_flow_style=False)

    if fmt in ("ini", "cfg", "conf", "config"):
        lines = [header]
        for k, v in sorted(data.items()):
            lines.append(f"{k} = {v}")
        return "\n".join(lines) + "\n"

    if fmt in ("properties",):
        lines = [header]
        for k, v in sorted(data.items()):
            lines.append(f"{k}={v}")
        return "\n".join(lines) + "\n"

    # Default: .env / KEY=VALUE format
    lines = [header]
    for k, v in sorted(data.items()):
        # Quote values that contain spaces or special chars
        if any(c in v for c in (" ", "#", "'", '"', "\n")):
            v = f'"{v}"'
        lines.append(f"{k}={v}")
    return "\n".join(lines) + "\n"


def _unflatten(flat: dict[str, str]) -> dict:
    """"a.b.c" → {"a": {"b": {"c": value}}}"""
    result: dict = {}
    for key, value in flat.items():
        parts = key.split(".")
        node  = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return result


def _safe_filename(env: str) -> str:
    """Replace filesystem-unsafe characters in env strings."""
    import re
    return re.sub(r"[^\w.\-]", "_", env)


def _sftp_makedirs(sftp: Any, remote_dir: str) -> None:
    """
    Recursively create remote directories via SFTP (no shell, no injection risk).

    Equivalent to `mkdir -p` but uses the SFTP protocol directly.
    Works by attempting to stat each path component and creating missing ones.
    """
    parts = remote_dir.split("/")
    current = ""
    for part in parts:
        if not part:
            current = "/"
            continue
        current = f"{current}/{part}" if current and current != "/" else f"/{part}"
        try:
            sftp.stat(current)
        except FileNotFoundError:
            try:
                sftp.mkdir(current)
            except OSError:
                pass  # may have been created by a concurrent process


# ── SSH transport (optional) ──────────────────────────────────────────────────

class _SshTransport:
    def __init__(self, cfg: dict) -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("paramiko is required for SSH sink: pip install paramiko") from exc
        self._client = paramiko.SSHClient()
        known_hosts = cfg.get("ssh_known_hosts_file")
        if known_hosts:
            self._client.load_host_keys(known_hosts)
        else:
            self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self._client.connect(
            hostname=cfg["ssh_host"],
            port=int(cfg.get("ssh_port", 22)),
            username=cfg.get("ssh_user", "root"),
            password=cfg.get("ssh_password"),
            key_filename=cfg.get("ssh_key_path"),
        )
        self._sftp = self._client.open_sftp()

    def write(self, remote_path: str, content: str) -> None:
        # Auto-provision: ensure remote directory exists via SFTP mkdir
        remote_dir = str(Path(remote_path).parent)
        try:
            self._sftp.stat(remote_dir)
        except FileNotFoundError:
            _sftp_makedirs(self._sftp, remote_dir)

        with self._sftp.file(remote_path, "wb") as fh:
            fh.write(content.encode("utf-8"))

    def close(self) -> None:
        try:
            self._sftp.close()
            self._client.close()
        except Exception:
            pass
