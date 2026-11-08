"""A file on this machine is replaced, never rewritten in place: a reader sees the old file or the new one."""
from __future__ import annotations

import os
import stat
import sys

import pytest

from app.utils import atomic_file


def test_temp_names_sit_next_to_the_file_and_hide_from_globs():
    name = atomic_file.temp_name("app.env")
    assert name.startswith(".app.env.vt-") and name.endswith(".tmp")
    assert atomic_file.is_temp_of(name, "app.env")
    assert not atomic_file.is_temp_of(name, "other.env")
    assert not atomic_file.is_temp_of("app.env.bak", "app.env")
    assert atomic_file.temp_name("app.env") != name     # two writes never share one


def test_write_atomic_creates_directories_and_replaces(tmp_path):
    path = tmp_path / "a" / "b" / "values.yaml"
    atomic_file.write_atomic(path, "one: 1\n")
    atomic_file.write_atomic(path, "two: 2\n")
    assert path.read_text() == "two: 2\n"
    assert os.listdir(path.parent) == ["values.yaml"]


def test_the_old_file_survives_a_failed_replace(tmp_path, monkeypatch):
    path = tmp_path / "values.yaml"
    path.write_text("old\n")

    def refuse(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(atomic_file.os, "replace", refuse)
    with pytest.raises(OSError, match="disk went away"):
        atomic_file.write_atomic(path, "new\n")
    assert path.read_text() == "old\n"
    assert os.listdir(tmp_path) == ["values.yaml"]      # the temp file is not left behind


def test_the_old_file_is_visible_until_the_replace(tmp_path, monkeypatch):
    path = tmp_path / "values.yaml"
    path.write_text("old\n")
    seen = []
    real = atomic_file.os.replace

    def watch(src, dst):
        seen.append(path.read_text())                   # the new content is on disk already; readers get the old
        real(src, dst)

    monkeypatch.setattr(atomic_file.os, "replace", watch)
    atomic_file.write_atomic(path, "new\n")
    assert seen == ["old\n"] and path.read_text() == "new\n"


def test_only_the_temp_files_of_that_name_are_removed(tmp_path):
    stale = atomic_file.temp_name("app.env")
    others = [atomic_file.temp_name("other.env"), "app.env", "app.env.bak"]
    for n in [stale, *others]:
        (tmp_path / n).write_text("x")
    assert atomic_file.remove_stale_temps(tmp_path, "app.env") == [stale]
    assert sorted(os.listdir(tmp_path)) == sorted(others)
    assert atomic_file.remove_stale_temps(tmp_path / "missing", "app.env") == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_replaced_file_keeps_its_mode(tmp_path):
    path = tmp_path / "secret.env"
    path.write_text("a=1\n")
    path.chmod(0o600)
    atomic_file.write_atomic(path, "a=2\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_helm_values_file_is_replaced_atomically(tmp_path, monkeypatch):
    from app.pipeline.sinks.strategies.helm.values_file import ValuesFileStrategy

    path = tmp_path / "out" / "values.yaml"
    path.parent.mkdir()
    path.write_text("old: 1\n")
    seen = []
    real = atomic_file.os.replace

    def watch(src, dst):
        seen.append(path.read_text())
        real(src, dst)

    monkeypatch.setattr(atomic_file.os, "replace", watch)
    result = ValuesFileStrategy(str(path)).apply({"replicaCount": "3"}, "app", "shop", None, None, None, [], False)
    assert result == {"written": 1}
    assert seen == ["old: 1\n"]                         # the file was not touched until the one rename
    assert path.read_text().startswith("replicaCount")
    assert os.listdir(path.parent) == ["values.yaml"]
