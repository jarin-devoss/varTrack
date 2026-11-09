"""
app/utils/atomic_file.py
────────────────────────
Replace a file on this machine so that a reader sees the old file or the new one, never part of one.

A config file is read by services while varTrack writes it (some reload it the moment it changes), and
``open(path, "w")`` truncates the file first: a reader in that moment gets an empty or half-written config, and a
crash or a full disk leaves it so. Here the new content goes to a file of its own in the *same directory* (a rename
across filesystems is not atomic), is flushed to disk, and only then takes the old file's place with one
``os.replace``. A crash leaves the old file and, at worst, a temp file, which the next write to the same path removes.

The temp file is dot-prefixed (``.app.env.vt-<id>.tmp``) so a glob such as ``*.env`` does not pick it up.
"""
from __future__ import annotations

import logging
import os
import stat
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_MARK = ".vt-"
_SUFFIX = ".tmp"


def temp_name(name: str) -> str:
    """The name of the temp file that stands in for *name* while it is written."""
    return f".{name}{_MARK}{uuid.uuid4().hex[:12]}{_SUFFIX}"


def is_temp_of(candidate: str, name: str) -> bool:
    return candidate.startswith(f".{name}{_MARK}") and candidate.endswith(_SUFFIX)


def remove_stale_temps(directory: str | os.PathLike[str], name: str) -> list[str]:
    """Remove the temp files an earlier write of *name* left behind (a crash between its two steps).

    Syncs of one place are serialised by the target lease, so a temp file found here is nobody's current write."""
    removed: list[str] = []
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return removed
    for entry in entries:
        if is_temp_of(entry, name):
            try:
                os.remove(os.path.join(directory, entry))
                removed.append(entry)
            except OSError as exc:
                logger.warning("atomic_file: could not remove the stale %s: %s", entry, exc)
    return removed


def fsync_directory(directory: str | os.PathLike[str]) -> None:
    """Make a rename in *directory* durable. Best effort: Windows cannot open a directory."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_durably(path: str | os.PathLike[str], content: str, *, encoding: str = "utf-8") -> None:
    """Write *content* to *path* (a file of its own, not the one being replaced) and flush it to disk."""
    with open(path, "w", encoding=encoding) as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())


def copy_attrs(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Give *dst* the mode and owner of *src*: the file that replaces another is not a new file to its readers."""
    st = os.stat(src)
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    if hasattr(os, "chown"):
        try:
            os.chown(dst, st.st_uid, st.st_gid)
        except PermissionError:
            # not root: the file is ours already, which is the owner it had unless varTrack runs as another user
            logger.debug("atomic_file: kept mode of %s, not its owner (no permission to chown)", src)


def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Atomically make *dst* the file *src* is, and make that durable."""
    os.replace(src, dst)
    fsync_directory(os.path.dirname(os.path.abspath(dst)))


def write_atomic(path: str | os.PathLike[str], content: str, *, encoding: str = "utf-8") -> None:
    """Replace the file at *path* with *content*, atomically, keeping its mode and owner when it exists."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    remove_stale_temps(target.parent, target.name)
    temp = target.parent / temp_name(target.name)
    try:
        write_durably(temp, content, encoding=encoding)
        if target.exists():
            copy_attrs(target, temp)
        replace(temp, target)
    finally:
        try:
            os.remove(temp)           # only still there when something failed before the replace
        except FileNotFoundError:
            pass
