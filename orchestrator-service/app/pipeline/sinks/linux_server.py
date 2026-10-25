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
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import (
    KeyFormat, format_keys, key_format_from_str, validate_sink_format,
    resolve_destination,
)
from app.pipeline.transformer import unflatten
from app.utils.enums.sync_mode import SyncMode

logger = logging.getLogger(__name__)

_SINK_KIND      = "linux_server"
_DEFAULT_FORMAT = KeyFormat.DOT


class LinuxServerSink(BaseSink):
    object_name = "file"

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

        # Where the file goes: a server over SSH, or this machine's disk.
        ssh_host = cfg.get("ssh_host")
        self._ssh = _SshTransport(cfg) if ssh_host else None
        self._fs: _Fs = self._ssh if self._ssh else _LocalFs()
        self._write_opts = _WriteOptions.from_config(cfg)

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
        total_sources: int,
        data: Any = None,
    ) -> dict[str, Any]:
        target = self._resolve_path(env)
        formatted = format_keys(flat_data, self._key_format)
        content   = _render(formatted, self._file_format, labels, env)

        _write_file(self._fs, target.as_posix(), content, self._write_opts)
        logger.info("LinuxServerSink: wrote %s env=%s%s", target.as_posix(), env, " over SSH" if self._ssh else "")

        return {"written": len(flat_data), "path": str(target)}

    def close(self) -> None:
        if self._ssh:
            self._ssh.close()

    def read_values(
        self,
        keys: "Collection[str]",
        datasource: str,
        env: str,
    ) -> "dict[str, str]":
        """
        Read current values back from the file this sink writes for *env*.
        Never raises: stage_sync calls this before every write for the
        change-log, and a file the write is about to replace — even one that
        no longer parses — must not fail that write.
        """
        if not keys:
            return {}
        try:
            target = self._resolve_path(env)
            content = self._fs.read(target.as_posix())
            values = _parse(content, self._file_format)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            return self._read_failed(exc, "LinuxServerSink.read_values")
        out: dict[str, str] = {}
        for k in keys:
            # Keys are stored in the sink's key format (e.g. FLAT: "db_host").
            stored = next(iter(format_keys({k: ""}, self._key_format)))
            if stored in values:
                out[k] = values[stored]
        return out


# ── Renderers ─────────────────────────────────────────────────────────────────

def _render(data: dict[str, str], fmt: str, labels: VarTrackLabels, env: str) -> str:
    """Serialise flat_data to the target file format."""
    header = f"# vartrack managed — env={env} tenant={labels.tenant_id}\n"

    if fmt in ("json",):
        # Reconstruct nested dict from dot-separated keys for clean JSON output.
        nested = unflatten(data)
        return json.dumps(nested, indent=2, sort_keys=True) + "\n"

    if fmt in ("yaml", "yml"):
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "pyyaml is required for YAML file output: pip install pyyaml"
            ) from exc
        nested = unflatten(data)
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


def _parse(content: str, fmt: str) -> dict[str, str]:
    """Inverse of _render: file content → stored key → value."""
    if fmt in ("json",):
        return _flatten(json.loads(content or "{}"))
    if fmt in ("yaml", "yml"):
        import yaml
        return _flatten(yaml.safe_load(content) or {})
    sep = " = " if fmt in ("ini", "cfg", "conf", "config") else "="
    values: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or sep.strip() not in line:
            continue
        k, _, v = line.partition(sep) if sep in line else line.partition(sep.strip())
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        values[k.strip()] = v
    return values


def _flatten(node: Any, prefix: str = "") -> dict[str, str]:
    """{"a": {"b": "c"}} → {"a.b": "c"}"""
    if not isinstance(node, dict):
        return {prefix: str(node)} if prefix else {}
    out: dict[str, str] = {}
    for k, v in node.items():
        out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    return out


def _safe_filename(env: str) -> str:
    """Replace filesystem-unsafe characters in env strings."""
    import re
    return re.sub(r"[^\w.\-]", "_", env)


# ── Writing the file ─────────────────────────────────────────────────────────

@dataclass
class _WriteOptions:
    """What happens around the write — the datasource's linux_server settings."""

    create_directories: bool = True
    file_mode: str = ""                       # "0640"
    file_owner: str = ""                      # "app" or "app:app"
    backup: bool = False
    backup_suffix: str = ".bak"
    validation_command: str = ""              # "{file}" is the written file
    rollback_on_validation_fail: bool = False
    post_write_commands: list[str] = field(default_factory=list)
    continue_on_command_error: bool = False

    @classmethod
    def from_config(cls, cfg: dict) -> "_WriteOptions":
        mode = str(cfg.get("file_mode") or "")
        if mode and not re.fullmatch(r"0?[0-7]{3}", mode):
            raise ValueError(f"file_mode {mode!r} is not an octal mode like 0640")
        return cls(
            create_directories=cfg.get("create_directories", True) is not False,
            file_mode=mode,
            file_owner=str(cfg.get("file_owner") or ""),
            backup=bool(cfg.get("backup_before_write", False)),
            backup_suffix=str(cfg.get("backup_suffix") or ".bak"),
            validation_command=str(cfg.get("validation_command") or ""),
            rollback_on_validation_fail=bool(cfg.get("rollback_on_validation_fail", False)),
            post_write_commands=[str(c) for c in cfg.get("post_write_commands") or []],
            continue_on_command_error=bool(cfg.get("continue_on_command_error", False)),
        )


class _Fs(Protocol):
    def exists(self, path: str) -> bool: ...
    def mkdirs(self, path: str) -> None: ...
    def read(self, path: str) -> str: ...
    def put(self, path: str, content: str) -> None: ...
    def copy(self, src: str, dst: str) -> None: ...
    def remove(self, path: str) -> None: ...
    def chmod(self, path: str, mode: str) -> None: ...
    def chown(self, path: str, owner: str) -> None: ...
    def run(self, command: str) -> tuple[int, str]: ...


def _with_file(command: str, path: str) -> str:
    return command.replace("{file}", shlex.quote(path))


def _write_file(fs: _Fs, path: str, content: str, opts: _WriteOptions) -> None:
    """Write *content* to *path* with the datasource's options, in order:
    directories, backup, write, mode and owner, validation (rolling back to
    the previous file when it fails and rollback is on), post-write commands.
    """
    directory = posixpath.dirname(path) or "/"
    if not fs.exists(directory):
        if not opts.create_directories:
            raise RuntimeError(f"{directory} does not exist and create_directories is off")
        fs.mkdirs(directory)

    # A backup is kept when asked for, and taken anyway when a failed
    # validation must be able to restore the previous file.
    rollback = bool(opts.validation_command and opts.rollback_on_validation_fail)
    backup = path + opts.backup_suffix if (opts.backup or rollback) and fs.exists(path) else ""
    if backup:
        fs.copy(path, backup)

    try:
        fs.put(path, content)
        if opts.file_mode:
            fs.chmod(path, opts.file_mode)
        if opts.file_owner:
            fs.chown(path, opts.file_owner)

        if opts.validation_command:
            rc, out = fs.run(_with_file(opts.validation_command, path))
            if rc != 0:
                restored = ""
                if rollback and backup:
                    fs.copy(backup, path)
                    restored = "; the previous file was restored"
                raise RuntimeError(f"validation_command failed (exit {rc}): {out.strip()[:500]}{restored}")
    finally:
        # A copy taken only to roll back with is not left behind.
        if backup and not opts.backup:
            fs.remove(backup)

    for command in opts.post_write_commands:
        rc, out = fs.run(_with_file(command, path))
        if rc != 0:
            message = f"post_write_command {command!r} failed (exit {rc}): {out.strip()[:500]}"
            if not opts.continue_on_command_error:
                raise RuntimeError(message)
            logger.warning("LinuxServerSink: %s — continuing", message)


class _LocalFs:
    """This machine's filesystem; commands run through the shell."""

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def mkdirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def put(self, path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def copy(self, src: str, dst: str) -> None:
        shutil.copy2(src, dst)

    def remove(self, path: str) -> None:
        os.remove(path)

    def chmod(self, path: str, mode: str) -> None:
        os.chmod(path, int(mode, 8))

    def chown(self, path: str, owner: str) -> None:
        user, _, group = owner.partition(":")
        shutil.chown(path, user=user or None, group=group or None)

    def run(self, command: str) -> tuple[int, str]:
        # The bundle's own command, as on a remote server.
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=300)  # noqa: S602
        return result.returncode, result.stdout + result.stderr


# ── SSH transport ────────────────────────────────────────────────────────────

class _SshTransport:
    """A server reached over SSH.

    Files move over SFTP; with use_sftp off, or with use_sudo, they move
    through shell commands instead (cat, mkdir, cp, install) — with sudo the
    file is uploaded to a temporary path first and installed from there, so
    sudo's password and the file's content never share stdin.

    host_key_verification: "strict" (default) refuses a host whose key is not
    known; "accept-new" accepts and records a host seen for the first time but
    refuses a changed key; "off" accepts any key.
    """

    def __init__(self, cfg: dict) -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("paramiko is required for SSH sink: pip install paramiko") from exc
        self._paramiko = paramiko
        self._host = cfg["ssh_host"]
        self._command_timeout = float(cfg.get("ssh_command_timeout_secs") or 60)
        self._sudo = bool(cfg.get("ssh_use_sudo", False))
        self._sudo_password = str(cfg.get("ssh_sudo_password") or "")
        self._client = self._connect(cfg)
        use_sftp = cfg.get("ssh_use_sftp", True) is not False
        self._sftp = self._client.open_sftp() if use_sftp and not self._sudo else None

    def _connect(self, cfg: dict) -> Any:
        paramiko = self._paramiko
        verification = str(cfg.get("ssh_host_key_verification") or "strict").lower()
        if verification not in ("strict", "accept-new", "off"):
            raise ValueError(f"host_key_verification {verification!r}: use strict, accept-new or off")
        known_hosts = cfg.get("ssh_known_hosts_file")
        timeout = float(cfg.get("ssh_connect_timeout_secs") or 30)
        attempts = 1 + max(0, int(cfg.get("ssh_max_retries") or 0))
        backoff = float(cfg.get("ssh_retry_backoff_secs") or 2)

        for attempt in range(1, attempts + 1):
            client = paramiko.SSHClient()
            if verification != "off":
                if known_hosts:
                    if os.path.exists(known_hosts):
                        client.load_host_keys(known_hosts)
                    elif verification == "strict":
                        raise RuntimeError(f"known_hosts_file {known_hosts} does not exist")
                else:
                    client.load_system_host_keys()
            client.set_missing_host_key_policy(
                paramiko.RejectPolicy() if verification == "strict" else paramiko.AutoAddPolicy()
            )
            try:
                client.connect(
                    hostname=self._host,
                    port=int(cfg.get("ssh_port", 22)),
                    username=cfg.get("ssh_user", "root"),
                    password=cfg.get("ssh_password"),
                    key_filename=cfg.get("ssh_key_path"),
                    timeout=timeout,
                    banner_timeout=timeout,
                    auth_timeout=timeout,
                    compress=bool(cfg.get("ssh_compression", False)),
                )
            except paramiko.BadHostKeyException:
                raise  # a changed host key is never retried
            except (paramiko.SSHException, OSError) as exc:
                client.close()
                if attempt == attempts:
                    raise
                logger.warning("LinuxServerSink: SSH to %s failed (%s); retry %d of %d in %.0fs",
                               self._host, exc, attempt, attempts - 1, backoff)
                time.sleep(backoff)
                continue
            if verification == "accept-new" and known_hosts:
                client.save_host_keys(known_hosts)
            return client
        raise RuntimeError(f"cannot connect to {self._host}")

    # ── commands ──

    def run(self, command: str, stdin: bytes = b"", sudo: bool | None = None) -> tuple[int, str]:
        """Run *command*; with sudo it runs as root, the password read from stdin."""
        if self._sudo if sudo is None else sudo:
            command = f"sudo -S -p '' sh -c {shlex.quote(command)}"
            if self._sudo_password:
                stdin = (self._sudo_password + "\n").encode() + stdin
        chan_in, chan_out, chan_err = self._client.exec_command(command, timeout=self._command_timeout)
        if stdin:
            chan_in.write(stdin)
        chan_in.channel.shutdown_write()
        out = chan_out.read().decode("utf-8", "replace") + chan_err.read().decode("utf-8", "replace")
        return chan_out.channel.recv_exit_status(), out

    def _check(self, command: str, stdin: bytes = b"", sudo: bool | None = None) -> str:
        rc, out = self.run(command, stdin, sudo)
        if rc != 0:
            raise RuntimeError(f"{command.split()[0]} on {self._host} failed (exit {rc}): {out.strip()[:300]}")
        return out

    # ── files ──

    def exists(self, path: str) -> bool:
        if self._sftp:
            try:
                self._sftp.stat(path)
                return True
            except FileNotFoundError:
                return False
        return self.run(f"test -e {shlex.quote(path)}")[0] == 0

    def mkdirs(self, path: str) -> None:
        if self._sftp:
            current = ""
            for part in path.split("/"):
                if not part:
                    current = "/"
                    continue
                current = f"{current.rstrip('/')}/{part}"
                try:
                    self._sftp.stat(current)
                except FileNotFoundError:
                    try:
                        self._sftp.mkdir(current)
                    except OSError:
                        pass  # created meanwhile
            return
        self._check(f"mkdir -p {shlex.quote(path)}")

    def read(self, path: str) -> str:
        if self._sftp:
            with self._sftp.file(path, "rb") as fh:
                return fh.read().decode("utf-8")
        rc, out = self.run(f"cat {shlex.quote(path)}")
        if rc != 0:
            raise FileNotFoundError(path)
        return out

    def put(self, path: str, content: str) -> None:
        data = content.encode("utf-8")
        if self._sftp:
            with self._sftp.file(path, "wb") as fh:
                fh.write(data)
            return
        if not self._sudo:
            self._check(f"cat > {shlex.quote(path)}", stdin=data)
            return
        tmp = f"/tmp/.vartrack-{uuid.uuid4().hex}"
        self._check(f"cat > {shlex.quote(tmp)}", stdin=data, sudo=False)
        try:
            self._check(f"install -m 0644 {shlex.quote(tmp)} {shlex.quote(path)}")
        finally:
            self.run(f"rm -f {shlex.quote(tmp)}", sudo=False)

    def copy(self, src: str, dst: str) -> None:
        if self._sftp:
            with self._sftp.file(src, "rb") as fin, self._sftp.file(dst, "wb") as fout:
                fout.write(fin.read())
            return
        self._check(f"cp -p {shlex.quote(src)} {shlex.quote(dst)}")

    def remove(self, path: str) -> None:
        if self._sftp:
            self._sftp.remove(path)
            return
        self._check(f"rm -f {shlex.quote(path)}")

    def chmod(self, path: str, mode: str) -> None:
        if self._sftp:
            self._sftp.chmod(path, int(mode, 8))
            return
        self._check(f"chmod {mode} {shlex.quote(path)}")

    def chown(self, path: str, owner: str) -> None:
        self._check(f"chown {shlex.quote(owner)} {shlex.quote(path)}")

    def close(self) -> None:
        try:
            if self._sftp:
                self._sftp.close()
            self._client.close()
        except Exception:
            pass
