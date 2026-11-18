"""
app/pipeline/sinks/file_output.py
──────────────────────────────────
What a sink that stores a FILE (update_strategy STRATEGY_FILE: S3, a Linux server) writes, and under what name.

The file is written in its own format, or in the one the rule names:

  1. the rule's ``file_format``, when it sets one;
  2. else the format the sink's own file name says (a file_pattern or destination path that ends in .json is JSON);
  3. else the format the file has in Git.

When the format is the file's own and nothing changed the file (no environment slice, root_key, secret or rename), the bytes
written are THE FILE AS IT IS IN GIT: comments, order, quoting and whitespace are kept. Otherwise the tree is rendered by the
format's writer (app/parsers/formats.py), which reads it back and raises Unrenderable, naming the key, if it would not read as
what was meant.

The name is the file's own (``app-settings.yaml``) unless the sink was given one; a converted file gets the new format's suffix.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.parsers import formats, loader


@dataclass(frozen=True)
class FileOutput:
    content: bytes
    name: str
    content_type: str
    fmt: formats.Format
    copied: bool          # the file as it is in Git, byte for byte


def target_format(*, file_path: str, source_text: str, file_format: str | None = None,
                  given_name: str | None = None) -> tuple[formats.Format, formats.Format, str]:
    """(the format to write, the format that keeps the file as it is, the file's parser family)."""
    family = formats.source_format(source_text, file_path) if source_text else _family_of_name(file_path)
    keep = formats.default_target(family, file_path)
    chosen = formats.target(file_format)
    named = formats.format_named_by(given_name) if given_name else None
    if chosen is not None and named is not None and named.name != chosen.name:
        raise formats.Unrenderable(
            f"file_format is {chosen.name}, but the file name {given_name} says {named.name}: name one or the other")
    if chosen is None:
        chosen = named
    return (chosen or keep), keep, family


def _family_of_name(file_path: str) -> str:
    from app.parsers import detector
    family = detector.detect(file_path)
    return "yaml" if family == "sniff" else family


def file_output(*, source_text: str, file_path: str, data: Any, flat_data: dict[str, str],
                file_format: str | None = None, given_name: str | None = None, xml_root: str = "config") -> FileOutput:
    """The bytes, name and content type to write for one file. Raises formats.Unrenderable."""
    fmt, keep, family = target_format(file_path=file_path, source_text=source_text,
                                      file_format=file_format, given_name=given_name)
    tree = data if data is not None else flat_data
    if data is None:
        family = "kv"     # only the flat keys are known: they are nested by their dots for a format that holds a tree

    copied = False
    if fmt == keep and source_text and data is not None:
        try:
            copied = loader.load_as(source_text, file_path) == data
        except Exception:
            copied = False
    if copied:
        content = source_text.encode("utf-8")
    else:
        content = formats.render(fmt, tree, source_family=family, source_text=source_text, xml_root=xml_root).encode("utf-8")

    stem = Path(file_path).stem
    name = given_name or (Path(file_path).name if fmt == keep else stem + fmt.suffix)
    return FileOutput(content=content, name=name, content_type=fmt.content_type, fmt=fmt, copied=copied)
