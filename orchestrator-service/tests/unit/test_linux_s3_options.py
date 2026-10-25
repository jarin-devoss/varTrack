"""
The linux_server and S3 datasource options the sinks act on: the write
procedure (_write_file) on a real directory, the SSH host-key modes and
retries, and the S3 client and upload arguments.
"""
from __future__ import annotations

import os
import shlex
import stat
import sys
from unittest.mock import MagicMock

import pytest

from app.pipeline.datasource_settings import sink_settings
from app.pipeline.sinks.linux_server import _LocalFs, _WriteOptions, _write_file


@pytest.fixture
def target(tmp_path):
    return (tmp_path / "etc" / "app" / "app.env").as_posix()


def test_directories_are_created_unless_turned_off(target):
    _write_file(_LocalFs(), target, "a=1\n", _WriteOptions())
    assert open(target).read() == "a=1\n"
    other = target.replace("/app/app.env", "/missing/app.env")
    with pytest.raises(RuntimeError, match="create_directories is off"):
        _write_file(_LocalFs(), other, "a=1\n", _WriteOptions(create_directories=False))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_file_mode(target):
    _write_file(_LocalFs(), target, "a=1\n", _WriteOptions(file_mode="0640"))
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


def test_backup_keeps_the_previous_file(target):
    _write_file(_LocalFs(), target, "a=1\n", _WriteOptions())
    _write_file(_LocalFs(), target, "a=2\n", _WriteOptions(backup=True, backup_suffix=".prev"))
    assert open(target).read() == "a=2\n"
    assert open(target + ".prev").read() == "a=1\n"


class FakeFs(_LocalFs):
    """Local files; commands are answered from a table instead of a shell."""

    def __init__(self, results):
        self.results, self.ran = results, []

    def run(self, command):
        self.ran.append(command)
        return self.results.get(command.split()[0], (0, ""))


def test_a_failed_validation_rolls_back(target):
    _write_file(_LocalFs(), target, "good=1\n", _WriteOptions())
    fs = FakeFs({"validate": (1, "bad key")})
    opts = _WriteOptions(validation_command="validate {file}", rollback_on_validation_fail=True)
    with pytest.raises(RuntimeError, match="validation_command failed .*bad key.*previous file was restored"):
        _write_file(fs, target, "broken\n", opts)
    assert open(target).read() == "good=1\n"
    assert fs.ran == [f"validate {shlex.quote(target)}"]
    assert not os.path.exists(target + ".bak")          # the rollback copy is not kept


def test_post_write_commands_stop_or_continue(target):
    fs = FakeFs({"reload": (1, "no such unit")})
    with pytest.raises(RuntimeError, match="post_write_command 'reload app' failed"):
        _write_file(fs, target, "a=1\n", _WriteOptions(post_write_commands=["reload app", "echo done"]))
    assert fs.ran == ["reload app"]

    fs = FakeFs({"reload": (1, "no such unit")})
    _write_file(fs, target, "a=1\n", _WriteOptions(post_write_commands=["reload app", "echo {file}"],
                                                   continue_on_command_error=True))
    assert fs.ran == ["reload app", f"echo {shlex.quote(target)}"]


def test_file_mode_must_be_octal():
    with pytest.raises(ValueError, match="octal"):
        _WriteOptions.from_config({"file_mode": "rw-r--r--"})


def test_config_cue_linux_options():
    s = sink_settings("linux_server", {
        "host": "h", "username": "u", "host_key_verification": "accept-new", "connection_timeout": "10s",
        "use_sudo": True, "sudo_password": {"value": "pw"}, "use_sftp": False, "file_mode": "0640",
        "file_owner": "app:app", "backup_before_write": True, "validation_command": "test -s {file}",
        "post_write_commands": ["systemctl reload app"], "continue_on_command_error": True,
    }, "linux_server", {})
    assert s["ssh_host_key_verification"] == "accept-new"
    assert s["ssh_connect_timeout_secs"] == 10
    assert (s["ssh_use_sudo"], s["ssh_sudo_password"], s["ssh_use_sftp"]) == (True, "pw", False)
    assert (s["file_mode"], s["file_owner"], s["backup_before_write"]) == ("0640", "app:app", True)
    assert s["post_write_commands"] == ["systemctl reload app"]


# ── SSH host keys and retries ────────────────────────────────────────────────

class _FakeHostKeys:
    def __init__(self):
        self.added = []

    def add(self, hostname, keytype, key):
        self.added.append((hostname, keytype))


class _FakeClient:
    def __init__(self):
        self.policy, self.loaded, self.saved, self.system, self.kw = None, None, None, False, {}
        self.host_keys = _FakeHostKeys()

    def get_host_keys(self):
        return self.host_keys

    def load_host_keys(self, path):
        self.loaded = path

    def load_system_host_keys(self):
        self.system = True

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kw):
        self.kw = kw

    def save_host_keys(self, path):
        self.saved = path

    def open_sftp(self):
        return MagicMock()

    def close(self):
        pass


@pytest.fixture
def fake_paramiko(monkeypatch):
    fake = MagicMock()
    fake.RejectPolicy = lambda: "reject"
    fake.MissingHostKeyPolicy = type("MissingHostKeyPolicy", (), {})
    fake.BadHostKeyException = type("BadHostKeyException", (Exception,), {})
    fake.SSHException = type("SSHException", (Exception,), {})
    clients = []

    def new_client():
        clients.append(_FakeClient())
        return clients[-1]

    fake.SSHClient = new_client
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    return clients


@pytest.mark.parametrize("mode", ["strict", "accept-new", "off"])
def test_host_key_verification(fake_paramiko, tmp_path, mode):
    from app.pipeline.sinks.linux_server import _SshTransport
    known = tmp_path / "known_hosts"
    known.write_text("")
    _SshTransport({"ssh_host": "h", "ssh_known_hosts_file": str(known), "ssh_host_key_verification": mode,
                   "ssh_connect_timeout_secs": 7, "ssh_compression": True})
    client = fake_paramiko[-1]
    assert (client.policy == "reject") is (mode == "strict")
    assert client.saved is None                      # nothing is written by connecting to a known host
    assert client.loaded == (None if mode == "off" else str(known))
    assert (client.kw["timeout"], client.kw["compress"]) == (7.0, True)


class _Key:
    def get_name(self):
        return "ssh-ed25519"

    def get_fingerprint(self):
        return b"\x01\x02"


def _unknown_host(fake_paramiko, tmp_path, mode, known_hosts):
    from app.pipeline.sinks.linux_server import _SshTransport
    _SshTransport({"ssh_host": "h", "ssh_known_hosts_file": known_hosts, "ssh_host_key_verification": mode})
    client = fake_paramiko[-1]
    client.policy.missing_host_key(client, "h", _Key())
    return client


def test_accept_new_records_a_new_host_key_in_known_hosts(fake_paramiko, tmp_path):
    known = str(tmp_path / "keys" / "known_hosts")
    client = _unknown_host(fake_paramiko, tmp_path, "accept-new", known)
    assert client.host_keys.added == [("h", "ssh-ed25519")]
    assert client.saved == known                    # written when the key is seen, not after the session


def test_accept_new_without_a_known_hosts_file_is_refused(fake_paramiko):
    from app.pipeline.sinks.linux_server import _SshTransport
    with pytest.raises(ValueError, match="needs known_hosts_file"):
        _SshTransport({"ssh_host": "h", "ssh_host_key_verification": "accept-new"})
    assert not fake_paramiko


def test_off_trusts_a_new_host_key_and_warns_every_time(fake_paramiko, tmp_path, caplog):
    with caplog.at_level("WARNING"):
        client = _unknown_host(fake_paramiko, tmp_path, "off", None)
    assert client.saved is None and client.host_keys.added == []
    assert "is not verified" in caplog.text


def test_connection_is_retried(fake_paramiko, monkeypatch):
    from app.pipeline.sinks import linux_server
    monkeypatch.setattr(linux_server.time, "sleep", lambda s: None)
    attempts = []

    def flaky_connect(self, **kw):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("connection refused")

    monkeypatch.setattr(_FakeClient, "connect", flaky_connect)
    linux_server._SshTransport({"ssh_host": "h", "ssh_max_retries": 2, "ssh_host_key_verification": "off"})
    assert len(attempts) == 3


# ── S3 ───────────────────────────────────────────────────────────────────────

def test_s3_client_and_upload_options(monkeypatch):
    import boto3
    from app.pipeline.sinks import s3 as s3_module

    client = MagicMock()
    made = {}

    def fake_client(service, **kw):
        made.update(kw)
        return client

    monkeypatch.setattr(boto3, "client", fake_client, raising=False)
    monkeypatch.setattr(s3_module, "_versioned", set())
    sink = s3_module.S3Sink(rule_config={
        "s3_bucket": "b", "s3_sse": "aws:kms", "s3_kms_key_id": "key-1", "s3_session_token": "tok",
        "s3_verify_ssl": False, "s3_enable_versioning": True, "s3_content_type": "text/plain",
        "s3_file_pattern": "settings-{env}.json",
    })
    assert made["aws_session_token"] == "tok" and made["verify"] is False
    client.put_bucket_versioning.assert_called_once_with(Bucket="b", VersioningConfiguration={"Status": "Enabled"})

    sink._put_single({"a": "1"}, {"a": "1"}, "prod", "")
    put = client.put_object.call_args.kwargs
    assert put["Key"].endswith("/settings-prod.json")
    assert (put["ServerSideEncryption"], put["SSEKMSKeyId"], put["ContentType"]) == ("aws:kms", "key-1", "text/plain")


def test_config_cue_s3_options():
    s = sink_settings("s3", {"bucket": "b", "server_side_encryption": "AES256", "timeout": "20s",
                             "force_path_style": True, "session_token": {"value": "t"}}, "s3", {})
    assert (s["s3_sse"], s["s3_timeout_secs"], s["s3_force_path_style"], s["s3_session_token"]) == ("AES256", 20, True, "t")
