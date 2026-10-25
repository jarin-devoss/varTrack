"""
The git checkout cache keeps a shallow clone per (repository, ref) and moves it
to the remote's commit when the ref has changed.

A depth-1 clone has no history to merge a new commit into, so it is updated by
fetching and resetting; `git pull --depth=1` ends in "refusing to merge
unrelated histories" and the cache then had to clone again. And what it
evicts has to go for good: git writes its objects read-only, which a plain
`rmtree` cannot delete on Windows, leaving a directory the next clone refuses.
"""
from __future__ import annotations

import importlib
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from app.utils.git import repo_cache
from app.utils.git.repo_cache import BranchRepoCache

REF = "refs/heads/main"


def _load_real_gitpython():
    """tests/conftest.py stands in a mock for GitPython in every test; these
    tests need the real one, to run the real git. Returns it with its
    submodules, which GitPython imports again by name while it runs."""
    is_git = lambda k: k == "git" or k.startswith("git.")  # noqa: E731
    stubbed = {k: v for k, v in sys.modules.items() if is_git(k)}
    for k in stubbed:
        del sys.modules[k]
    try:
        module = importlib.import_module("git")
        return module, {k: v for k, v in sys.modules.items() if is_git(k)}
    except ImportError:
        return None, {}
    finally:
        for k in [k for k in sys.modules if is_git(k)]:
            del sys.modules[k]
        sys.modules.update(stubbed)


_REAL_GIT, _REAL_GIT_MODULES = _load_real_gitpython()
pytestmark = pytest.mark.skipif(_REAL_GIT is None or shutil.which("git") is None,
                                reason="needs GitPython and the git executable")


@pytest.fixture(autouse=True)
def real_git(monkeypatch):
    for name, module in _REAL_GIT_MODULES.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(repo_cache, "git", _REAL_GIT)


def _commit(repo, name: str, text: str) -> str:
    path = Path(repo.working_tree_dir) / name
    path.write_text(text, encoding="utf-8")
    repo.index.add([name])
    return repo.index.commit(f"write {name}", author=_AUTHOR, committer=_AUTHOR).hexsha


_AUTHOR = _REAL_GIT.Actor("vartrack-test", "test@example.com") if _REAL_GIT else None


@pytest.fixture
def origin(tmp_path):
    repo = _REAL_GIT.Repo.init(tmp_path / "origin", initial_branch="main")
    _commit(repo, "app.yaml", "version: 1\n")
    return repo


def _url(repo) -> str:
    return Path(repo.working_tree_dir).as_uri()  # file://, so that --depth applies


def test_a_new_commit_updates_the_cached_clone_in_place(origin, tmp_path):
    cache = BranchRepoCache(base_dir=str(tmp_path / "cache"))
    path, first = cache.get_or_clone(_url(origin), REF)
    assert (path / "app.yaml").read_text(encoding="utf-8") == "version: 1\n"

    second_sha = _commit(origin, "app.yaml", "version: 2\n")
    again, second = cache.get_or_clone(_url(origin), REF)

    assert second == second_sha != first
    assert again == path and len(cache) == 1  # updated, not evicted and cloned again
    assert (path / "app.yaml").read_text(encoding="utf-8") == "version: 2\n"


def test_several_new_commits_in_a_row_keep_updating_it(origin, tmp_path):
    cache = BranchRepoCache(base_dir=str(tmp_path / "cache"))
    cache.get_or_clone(_url(origin), REF)
    for n in range(2, 5):
        sha = _commit(origin, "app.yaml", f"version: {n}\n")
        path, got = cache.get_or_clone(_url(origin), REF)
        assert got == sha
        assert (path / "app.yaml").read_text(encoding="utf-8") == f"version: {n}\n"


def test_an_unchanged_ref_is_served_from_the_cache(origin, tmp_path):
    cache = BranchRepoCache(base_dir=str(tmp_path / "cache"))
    path, sha = cache.get_or_clone(_url(origin), REF)
    assert cache.get_or_clone(_url(origin), REF) == (path, sha)


def test_evicting_a_checkout_removes_git_s_read_only_files(origin, tmp_path):
    cache = BranchRepoCache(base_dir=str(tmp_path / "cache"))
    path, _ = cache.get_or_clone(_url(origin), REF)
    # What git leaves behind on Windows: objects without the write bit.
    for obj in (path / ".git" / "objects").rglob("*"):
        if obj.is_file():
            os.chmod(obj, stat.S_IREAD)

    cache.invalidate(_url(origin), REF)

    assert not path.exists()


def test_a_checkout_can_be_cloned_again_after_it_was_evicted(origin, tmp_path):
    cache = BranchRepoCache(base_dir=str(tmp_path / "cache"))
    path, _ = cache.get_or_clone(_url(origin), REF)
    cache.invalidate(_url(origin), REF)
    sha = _commit(origin, "app.yaml", "version: 2\n")

    again, got = cache.get_or_clone(_url(origin), REF)

    assert got == sha and (again / "app.yaml").read_text(encoding="utf-8") == "version: 2\n"
