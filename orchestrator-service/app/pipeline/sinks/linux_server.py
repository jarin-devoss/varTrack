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

  env_as_filename: true  (default; the rule's)
    The env is embedded in the filename.
    template: "{base}_{env}.{ext}"
    e.g.   /etc/myapp/config_pr-42.env
           /etc/myapp/config_v1.2.3.json

  env_as_path: true  (the datasource's)
    The env becomes a sub-directory.
    e.g.   /etc/myapp/pr-42/config.env

The parent directory is created automatically (os.makedirs).
If the file already exists it is overwritten (upsert semantics).

SSH support
───────────
When the datasource's block in config.cue sets host, the file is written to that
server over SSH (Paramiko); without one the sink writes to the local filesystem.
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

from app.pipeline.sinks.base import BaseSink
from app.pipeline.sinks.labels import VarTrackLabels
from app.pipeline.sinks.key_formatter import (
    KeyFormat, format_keys, key_format_from_str, validate_sink_format,
    resolve_destination,
)
from app.pipeline.transformer import unflatten
from app.utils import atomic_file
from app.utils.enums.sync_mode import SyncMode

if TYPE_CHECKING:
    from app.pipeline.sinks.settings import Settings

logger = logging.getLogger(__name__)

_SINK_KIND      = "linux_server"
_DEFAULT_FORMAT = KeyFormat.DOT


class LinuxServerSink(BaseSink):
    object_name = "file"

    @staticmethod
    def file_path_of(settings: Any) -> str | None:
        """The file the datasource writes: its first file_paths entry, under base_directory when relative."""
        paths = settings.get("file_paths") or []
        if not paths:
            return None
        path, base = str(paths[0]), settings.get("base_directory")
        if base and not path.startswith("/"):
            return base.rstrip("/") + "/" + path
        return path

    def __init__(
        self,
        *,
        name: str = "linux_server",
        settings: "Settings | None" = None,
        rule_config: dict | None = None,
        tenant_id: str = "default",
        **_kwargs: Any,
    ) -> None:
        from app.pipeline.sinks.settings import Settings

        s    = settings or Settings.of("linux_server")
        rule = rule_config or {}

        self._base_path       = Path(self.file_path_of(s) or "/etc/vartrack/config.env")
        self._tenant_id       = tenant_id
        self._env_as_filename = bool(rule.get("env_as_filename", True))
        self._env_as_dir      = bool(s.get("env_as_path", False))
        # destination_template: full file path template, e.g. "/etc/myapp/{env}/config.env".
        # Takes precedence over the datasource's file path + env_as_filename / env_as_path when set.
        self._dest_tpl  = rule.get("destination_template", "")
        self._file_format     = rule.get("file_format", self._base_path.suffix.lstrip(".") or "env")

        fmt_name         = rule.get("key_format", "dot")
        self._key_format = key_format_from_str(fmt_name)
        validate_sink_format(_SINK_KIND, self._key_format)

        # Where the file goes: a server over SSH, or this machine's disk.
        self._ssh = _SshTransport(s) if s.get("host") else None
        self._fs: _Fs = self._ssh if self._ssh else _LocalFs()
        self._write_opts = _WriteOptions.from_settings(s)

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


def _key_file(settings: Any) -> str | None:
    """The private key's file: ssh_key.private_key must be {file: "/path"}; the sink reads the key from
    a file, so any other form is refused rather than ignored."""
    key = (settings.raw("ssh_key") or {}).get("private_key")
    if key is None:
        return None
    if isinstance(key, dict) and "file" in key and not key.get("key"):
        return str(key["file"])
    raise ValueError(f"linux_server datasource {settings.name}: ssh_key.private_key must be "
                     "{file: \"/path/to/key\"}: the sink reads the key from a file")


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
    validation_command: str = ""              # "{file}" is the new file, "{target}" the path it is going to
    rollback_on_validation_fail: bool = False
    validate_after_rename: bool = False       # validate the file in place instead (see the proto)
    post_write_commands: list[str] = field(default_factory=list)
    continue_on_command_error: bool = False

    @classmethod
    def from_settings(cls, settings: Any) -> "_WriteOptions":
        mode = str(settings.get("file_mode") or "")
        if mode and not re.fullmatch(r"0?[0-7]{3}", mode):
            raise ValueError(f"file_mode {mode!r} is not an octal mode like 0640")
        return cls(
            create_directories=settings.get("create_directories", True) is not False,
            file_mode=mode,
            file_owner=str(settings.get("file_owner") or ""),
            backup=bool(settings.get("backup_before_write", False)),
            backup_suffix=str(settings.get("backup_suffix") or ".bak"),
            validation_command=str(settings.get("validation_command") or ""),
            rollback_on_validation_fail=bool(settings.get("rollback_on_validation_fail", False)),
            validate_after_rename=bool(settings.get("validate_after_rename", False)),
            post_write_commands=[str(c) for c in settings.get("post_write_commands") or []],
            continue_on_command_error=bool(settings.get("continue_on_command_error", False)),
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
    # What an atomic replacement needs. replace: *dst* becomes *src* in one step, so it is the old file or
    # the new one to any reader, never part of either.
    def replace(self, src: str, dst: str) -> None: ...
    def copy_attrs(self, src: str, dst: str) -> None: ...
    def remove_stale_temps(self, directory: str, name: str) -> None: ...


def _with_file(command: str, path: str, target: str | None = None) -> str:
    return command.replace("{file}", shlex.quote(path)).replace("{target}", shlex.quote(target or path))


def _write_file(fs: _Fs, path: str, content: str, opts: _WriteOptions) -> None:
    """Write *content* to *path* with the datasource's options, in order:
    directories, backup, the new file next to the old one, mode and owner, validation, the replacement,
    post-write commands.

    The target is never written in place: a service that reads it (or reloads when it changes) would get a
    truncated or half-written config, and so would a crash or a dropped connection leave one. The new content
    is written to a temp file in the same directory, given the mode and owner of the file it replaces,
    validated, and only then renamed over the target in one step. A failed validation leaves the old file
    untouched. With ``validate_after_rename`` the order is the older one: replace, then validate the file in
    place, and (with ``rollback_on_validation_fail``) put the previous file back when it fails."""
    directory = posixpath.dirname(path) or "/"
    name = posixpath.basename(path)
    if not fs.exists(directory):
        if not opts.create_directories:
            raise RuntimeError(f"{directory} does not exist and create_directories is off")
        fs.mkdirs(directory)
    fs.remove_stale_temps(directory, name)    # what a write that died between its steps left

    existed = fs.exists(path)
    in_place = bool(opts.validation_command and opts.validate_after_rename)
    # A backup is kept when asked for, and taken anyway when a failed in-place
    # validation must be able to restore the previous file.
    rollback = bool(in_place and opts.rollback_on_validation_fail)
    backup = path + opts.backup_suffix if (opts.backup or rollback) and existed else ""
    if backup:
        fs.copy(path, backup)

    temp: str | None = posixpath.join(directory, atomic_file.temp_name(name))
    try:
        fs.put(temp, content)
        if existed:
            fs.copy_attrs(path, temp)         # a replaced file keeps its mode and owner
        if opts.file_mode:
            fs.chmod(temp, opts.file_mode)
        if opts.file_owner:
            fs.chown(temp, opts.file_owner)

        if opts.validation_command and not in_place:
            rc, out = fs.run(_with_file(opts.validation_command, temp, target=path))
            if rc != 0:
                raise RuntimeError(f"validation_command failed (exit {rc}): {out.strip()[:500]}; "
                                   f"{path} was not replaced")

        fs.replace(temp, path)
        temp = None

        if in_place:
            rc, out = fs.run(_with_file(opts.validation_command, path))
            if rc != 0:
                restored = ""
                if rollback and backup:
                    fs.copy(backup, path)
                    restored = "; the previous file was restored"
                raise RuntimeError(f"validation_command failed (exit {rc}): {out.strip()[:500]}{restored}")
    finally:
        if temp is not None:                  # the replacement did not happen: the temp file goes
            try:
                fs.remove(temp)
            except Exception:
                logger.warning("LinuxServerSink: could not remove %s", temp, exc_info=True)
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
        atomic_file.write_durably(path, content)

    def replace(self, src: str, dst: str) -> None:
        atomic_file.replace(src, dst)

    def copy_attrs(self, src: str, dst: str) -> None:
        atomic_file.copy_attrs(src, dst)

    def remove_stale_temps(self, directory: str, name: str) -> None:
        atomic_file.remove_stale_temps(directory, name)

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

def _host_key_policy(paramiko: Any, verification: str, known_hosts: str | None) -> Any:
    """What paramiko does with a host whose key is not known yet.

    strict      refuse it
    accept-new  trust it and write its key to *known_hosts* at once, before the
                session starts, so the next connection checks it; a known host
                with another key never reaches the policy (paramiko raises
                BadHostKeyException first)
    off         trust it and write nothing down: nothing protects the connection
                from a man in the middle, and every connection says so in the log
    """
    if verification == "strict":
        return paramiko.RejectPolicy()

    class _TrustNewHostKey(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
            fingerprint = key.get_fingerprint().hex()
            if verification == "off":
                logger.warning("LinuxServerSink: host key of %s (%s %s) is not verified: "
                               "a man in the middle would not be detected",
                               hostname, key.get_name(), fingerprint)
                return
            client.get_host_keys().add(hostname, key.get_name(), key)
            if known_hosts:
                os.makedirs(os.path.dirname(os.path.abspath(known_hosts)), exist_ok=True)
                client.save_host_keys(known_hosts)
            logger.info("LinuxServerSink: new host %s, %s key %s recorded",
                        hostname, key.get_name(), fingerprint)

    return _TrustNewHostKey()


class _SshTransport:
    """A server reached over SSH.

    Files move over SFTP; with use_sftp off, or with use_sudo, they move
    through shell commands instead (cat, mkdir, cp, install) — with sudo the
    file is uploaded to a temporary path first and installed from there, so
    sudo's password and the file's content never share stdin.

    host_key_verification: "strict" (default) refuses a host whose key is not
    known; "accept-new" accepts a host seen for the first time and records its
    key in known_hosts_file (required), but refuses a changed key; "off" accepts
    any key and logs a warning on every connection.
    """

    def __init__(self, settings: Any) -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("paramiko is required for SSH sink: pip install paramiko") from exc
        self._paramiko = paramiko
        self._host = settings.get("host")
        self._command_timeout = float(settings.seconds("command_timeout") or 60)
        self._sudo = bool(settings.get("use_sudo", False))
        self._sudo_password = str(settings.secret("sudo_password") or "")
        self._client = self._connect(settings)
        use_sftp = settings.get("use_sftp", True) is not False
        self._sftp = self._client.open_sftp() if use_sftp and not self._sudo else None

    def _connect(self, settings: Any) -> Any:
        paramiko = self._paramiko
        verification = str(settings.get("host_key_verification") or "strict").lower()
        if verification not in ("strict", "accept-new", "off"):
            raise ValueError(f"host_key_verification {verification!r}: use strict, accept-new or off")
        known_hosts = settings.get("known_hosts_file")
        if verification == "accept-new" and not known_hosts:
            raise ValueError("host_key_verification accept-new needs known_hosts_file: "
                             "with nowhere to record a new key it would verify nothing")
        timeout = float(settings.seconds("connection_timeout") or 30)
        attempts = 1 + max(0, int(settings.get("max_retries") or 0))
        backoff = float(settings.seconds("retry_backoff") or 2)

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
            client.set_missing_host_key_policy(_host_key_policy(paramiko, verification, known_hosts))
            try:
                client.connect(
                    hostname=self._host,
                    port=int(settings.get("port") or 22),
                    username=settings.get("username") or "root",
                    password=settings.secret("password"),
                    key_filename=_key_file(settings),
                    timeout=timeout,
                    banner_timeout=timeout,
                    auth_timeout=timeout,
                    compress=bool(settings.get("enable_compression", False)),
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

    def replace(self, src: str, dst: str) -> None:
        """*dst* becomes *src* in one step. Over SFTP that is OpenSSH's posix-rename, which (unlike the
        protocol's own rename) replaces an existing file atomically; a server without it is refused rather than
        falling back to remove-then-rename, which has a moment with no file."""
        if self._sftp:
            try:
                self._sftp.posix_rename(src, dst)
            except (OSError, AttributeError) as exc:
                raise RuntimeError(
                    f"cannot replace {dst} atomically over SFTP: the server needs the posix-rename@openssh.com "
                    f"extension ({exc}); set use_sftp: false to replace it with `mv` over a shell instead"
                ) from exc
            return
        self._check(f"mv -f {shlex.quote(src)} {shlex.quote(dst)}")

    def copy_attrs(self, src: str, dst: str) -> None:
        """Give *dst* the mode and owner of *src*."""
        if self._sftp:
            st = self._sftp.stat(src)
            self._sftp.chmod(dst, stat.S_IMODE(st.st_mode))
            try:
                self._sftp.chown(dst, st.st_uid, st.st_gid)
            except OSError as exc:
                logger.warning("LinuxServerSink: kept the mode of %s but not its owner (%s); "
                               "set file_owner, or run as that user", src, exc)
            return
        out = self._check(f"stat -c '%a %u:%g' {shlex.quote(src)}").split()
        self._check(f"chmod {out[0]} {shlex.quote(dst)}")
        rc, err = self.run(f"chown {out[1]} {shlex.quote(dst)}")
        if rc != 0:
            logger.warning("LinuxServerSink: kept the mode of %s but not its owner (%s)", src, err.strip()[:200])

    def remove_stale_temps(self, directory: str, name: str) -> None:
        if self._sftp:
            for entry in self._sftp.listdir(directory):
                if atomic_file.is_temp_of(entry, name):
                    self._sftp.remove(posixpath.join(directory, entry))
            return
        pattern = f".{name}{atomic_file._MARK}*{atomic_file._SUFFIX}"
        self._check(f"find {shlex.quote(directory)} -maxdepth 1 -name {shlex.quote(pattern)} -delete")

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
