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

from app.pipeline.sinks.settings import Settings
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


def _siblings(target):
    return sorted(os.listdir(os.path.dirname(target)))


def test_a_failed_validation_rolls_back_when_validating_in_place(target):
    _write_file(_LocalFs(), target, "good=1\n", _WriteOptions())
    fs = FakeFs({"validate": (1, "bad key")})
    opts = _WriteOptions(validation_command="validate {file}", rollback_on_validation_fail=True,
                         validate_after_rename=True)
    with pytest.raises(RuntimeError, match="validation_command failed .*bad key.*previous file was restored"):
        _write_file(fs, target, "broken\n", opts)
    assert open(target).read() == "good=1\n"
    assert fs.ran == [f"validate {shlex.quote(target)}"]
    assert not os.path.exists(target + ".bak")          # the rollback copy is not kept


def test_a_failed_validation_never_touches_the_target(target):
    _write_file(_LocalFs(), target, "good=1\n", _WriteOptions())
    fs = FakeFs({"validate": (1, "bad key")})
    opts = _WriteOptions(validation_command="validate {file} {target}")
    with pytest.raises(RuntimeError, match=r"validation_command failed .*bad key.* was not replaced"):
        _write_file(fs, target, "broken\n", opts)
    assert open(target).read() == "good=1\n"
    assert _siblings(target) == ["app.env"]             # the temp file is gone
    [ran] = fs.ran
    checked, final = shlex.split(ran)[1:]
    assert checked != target and checked.startswith(os.path.dirname(target) + "/.app.env.vt-")
    assert final == target                              # {target} is where it is going, {file} what is checked


def test_the_new_content_is_what_validation_sees(target):
    _write_file(_LocalFs(), target, "good=1\n", _WriteOptions())
    seen = []

    class Reading(FakeFs):
        def run(self, command):
            path = shlex.split(command)[1]
            seen.append((open(path).read(), open(target).read()))
            return 0, ""

    _write_file(Reading({}), target, "new=2\n", _WriteOptions(validation_command="check {file}"))
    assert seen == [("new=2\n", "good=1\n")]            # the file is checked while the old one is still in place
    assert open(target).read() == "new=2\n"


def test_a_reader_never_sees_a_partial_file(target):
    _write_file(_LocalFs(), target, "old\n", _WriteOptions())
    seen = []

    class Watching(_LocalFs):
        def put(self, path, content):
            super().put(path, content)
            seen.append(open(target).read())            # the new file is written; the target is still the old one

    _write_file(Watching(), target, "new\n", _WriteOptions())
    assert seen == ["old\n"] and open(target).read() == "new\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_replaced_file_keeps_its_mode(target):
    _write_file(_LocalFs(), target, "a=1\n", _WriteOptions())
    os.chmod(target, 0o640)
    _write_file(_LocalFs(), target, "a=2\n", _WriteOptions())
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640
    _write_file(_LocalFs(), target, "a=3\n", _WriteOptions(file_mode="0600"))   # an explicit mode wins
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_a_crash_between_the_steps_leaves_the_old_file_and_the_next_write_cleans_up(target):
    _write_file(_LocalFs(), target, "old\n", _WriteOptions())

    class Dying(_LocalFs):
        def replace(self, src, dst):
            raise OSError("disk went away")

        def remove(self, path):                         # the process is gone: nothing is cleaned up
            pass

    with pytest.raises(OSError, match="disk went away"):
        _write_file(Dying(), target, "new\n", _WriteOptions())
    assert open(target).read() == "old\n"
    assert len(_siblings(target)) == 2                  # the old file and the temp file the crash left

    _write_file(_LocalFs(), target, "newer\n", _WriteOptions())
    assert _siblings(target) == ["app.env"] and open(target).read() == "newer\n"


def test_a_failed_replace_leaves_no_temp_file(target):
    _write_file(_LocalFs(), target, "old\n", _WriteOptions())

    class Refusing(_LocalFs):
        def replace(self, src, dst):
            raise OSError("read-only file system")

    with pytest.raises(OSError, match="read-only"):
        _write_file(Refusing(), target, "new\n", _WriteOptions())
    assert _siblings(target) == ["app.env"] and open(target).read() == "old\n"


def test_stale_temp_files_of_other_names_are_left_alone(target):
    _write_file(_LocalFs(), target, "a=1\n", _WriteOptions())
    other = os.path.join(os.path.dirname(target), ".other.env.vt-abc.tmp")
    open(other, "w").close()
    _write_file(_LocalFs(), target, "a=2\n", _WriteOptions())
    assert ".other.env.vt-abc.tmp" in _siblings(target)


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
        _WriteOptions.from_settings(Settings.of("linux_server", file_mode="rw-r--r--"))


def test_config_cue_linux_options(fake_paramiko, tmp_path):
    from app.pipeline.sinks.linux_server import _SshTransport

    s = Settings.parse("linux_server", "linux_server", {
        "host": "h", "username": "u", "host_key_verification": "accept-new", "connection_timeout": "10s",
        "known_hosts_file": str(tmp_path / "known_hosts"),
        "use_sudo": True, "sudo_password": {"value": "pw"}, "use_sftp": False, "file_mode": "0640",
        "file_owner": "app:app", "backup_before_write": True, "validation_command": "test -s {file}",
        "post_write_commands": ["systemctl reload app"], "continue_on_command_error": True,
    })
    ssh = _SshTransport(s)
    assert (ssh._sudo, ssh._sudo_password, ssh._sftp) == (True, "pw", None)
    client = fake_paramiko[-1]
    assert client.kw["timeout"] == 10.0 and client.kw["username"] == "u"
    opts = _WriteOptions.from_settings(s)
    assert (opts.file_mode, opts.file_owner, opts.backup) == ("0640", "app:app", True)
    assert opts.post_write_commands == ["systemctl reload app"]


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
    _SshTransport(Settings.of("linux_server", host="h", known_hosts_file=str(known), host_key_verification=mode,
                              connection_timeout=7, enable_compression=True))
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
    fields = {"known_hosts_file": known_hosts} if known_hosts else {}
    _SshTransport(Settings.of("linux_server", host="h", host_key_verification=mode, **fields))
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
        _SshTransport(Settings.of("linux_server", host="h", host_key_verification="accept-new"))
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
    linux_server._SshTransport(Settings.of("linux_server", host="h", max_retries=2, host_key_verification="off"))
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
    sink = s3_module.S3Sink(settings=Settings.of(
        "s3", bucket="b", server_side_encryption="aws:kms", kms_key_id="key-1", session_token="tok",
        insecure_skip_verify=True, enable_versioning=True, content_type="text/plain",
        file_pattern="settings-{env}.json",
    ))
    assert made["aws_session_token"] == "tok" and made["verify"] is False
    client.put_bucket_versioning.assert_called_once_with(Bucket="b", VersioningConfiguration={"Status": "Enabled"})

    sink._put_single({"a": "1"}, {"a": "1"}, "prod", "")
    put = client.put_object.call_args.kwargs
    assert put["Key"].endswith("/settings-prod.json")
    assert (put["ServerSideEncryption"], put["SSEKMSKeyId"], put["ContentType"]) == ("aws:kms", "key-1", "text/plain")


def test_config_cue_s3_options(monkeypatch):
    import types

    import boto3
    from app.pipeline.sinks import s3 as s3_module

    made = {}
    monkeypatch.setattr(boto3, "client", lambda service, **kw: made.update(kw) or MagicMock(), raising=False)
    monkeypatch.setitem(sys.modules, "botocore.config", types.SimpleNamespace(Config=lambda **kw: kw))
    sink = s3_module.S3Sink(settings=Settings.parse("s3", "s3", {
        "bucket": "b", "server_side_encryption": "AES256", "timeout": "20s", "force_path_style": True,
        "session_token": {"value": "t"}}))
    assert sink._put_extra == {"ServerSideEncryption": "AES256"} and made["aws_session_token"] == "t"
    assert made["config"] == {"s3": {"addressing_style": "path"}, "connect_timeout": 20, "read_timeout": 20}


# ── replacing a file over SSH ────────────────────────────────────────────────

def _ssh(tmp_path, **extra):
    from app.pipeline.sinks.linux_server import _SshTransport

    s = Settings.parse("linux_server", "linux_server", {
        "host": "h", "username": "u", "host_key_verification": "off", **extra,
    })
    return _SshTransport(s)


def test_sftp_replaces_with_posix_rename(fake_paramiko, tmp_path):
    ssh = _ssh(tmp_path)
    ssh.replace("/etc/app/.app.env.vt-1.tmp", "/etc/app/app.env")
    ssh._sftp.posix_rename.assert_called_once_with("/etc/app/.app.env.vt-1.tmp", "/etc/app/app.env")
    ssh._sftp.rename.assert_not_called()                # the protocol's rename refuses an existing file


def test_a_server_without_posix_rename_is_refused_not_worked_around(fake_paramiko, tmp_path):
    ssh = _ssh(tmp_path)
    ssh._sftp.posix_rename.side_effect = OSError("Operation unsupported")
    with pytest.raises(RuntimeError, match="posix-rename.*use_sftp: false"):
        ssh.replace("/etc/app/.t.tmp", "/etc/app/app.env")
    ssh._sftp.remove.assert_not_called()                # no remove-then-rename gap


def test_the_shell_replaces_with_mv(fake_paramiko, tmp_path):
    ssh = _ssh(tmp_path, use_sftp=False)
    ssh._check = MagicMock(return_value="")
    ssh.replace("/etc/app/.t.tmp", "/etc/app/my app.env")
    ssh._check.assert_called_once_with("mv -f /etc/app/.t.tmp '/etc/app/my app.env'")


def test_sftp_copies_mode_and_owner_to_the_new_file(fake_paramiko, tmp_path):
    ssh = _ssh(tmp_path)
    ssh._sftp.stat.return_value = MagicMock(st_mode=0o100640, st_uid=33, st_gid=44)
    ssh.copy_attrs("/etc/app/app.env", "/etc/app/.t.tmp")
    ssh._sftp.chmod.assert_called_once_with("/etc/app/.t.tmp", 0o640)
    ssh._sftp.chown.assert_called_once_with("/etc/app/.t.tmp", 33, 44)


def test_stale_temp_files_are_removed_over_sftp(fake_paramiko, tmp_path):
    ssh = _ssh(tmp_path)
    ssh._sftp.listdir.return_value = ["app.env", ".app.env.vt-abc.tmp", ".other.env.vt-abc.tmp", "app.env.bak"]
    ssh.remove_stale_temps("/etc/app", "app.env")
    ssh._sftp.remove.assert_called_once_with("/etc/app/.app.env.vt-abc.tmp")
