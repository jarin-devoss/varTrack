"""
app/parsers/formats.py
───────────────────────
The formats varTrack reads, each with the writer that puts a tree back into it.

The parsers (loader.py) turn a file into a tree. A sink that stores a FILE (update_strategy STRATEGY_FILE on S3 and on a
Linux server) writes the tree back out, in the file's own format or in another one the rule names. Each format has a name, a
suffix, a content type and a writer; ``render`` is the one entry point.

Every render is READ BACK: the text is parsed again with the target format's own parser, and what it reads as must be what
was meant. A conversion that would lose something (a null in TOML, a key that is not an XML name, a value that cannot sit on
one line of a .env file) raises Unrenderable and names the key, instead of writing a file that reads differently from Git.

Formats and what a tree needs for them
──────────────────────────────────────
  yaml, json     any tree
  toml           a table at the top; no null
  hcl            attributes and objects; strings are quoted (python-hcl2 writes them bare otherwise)
  xml            elements; "@attrs" are attributes, "#text" the text of an element with children or attributes, a list
                 repeated elements; one root element (another is added when the tree has several: ``xml_root``)
  ini            sections from the first dotted segment of a key; a key with no dot is in [DEFAULT]
  env, properties  one key=value per dotted key
"""
from __future__ import annotations

import configparser
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any


class Unrenderable(ValueError):
    """A tree that cannot be written in the format asked for without losing something. The message names the key."""


@dataclass(frozen=True)
class Format:
    name: str           # what a rule calls it: yaml, json, toml, hcl, xml, ini, env, properties
    family: str         # the parser that reads it: yaml, json, toml, hcl, xml, ini, kv
    suffix: str
    content_type: str
    text_only: bool     # every value comes back as text


TARGETS: dict[str, Format] = {f.name: f for f in (
    Format("yaml",       "yaml", ".yaml",       "application/yaml", False),
    Format("json",       "json", ".json",       "application/json", False),
    Format("toml",       "toml", ".toml",       "application/toml", False),
    Format("hcl",        "hcl",  ".hcl",        "text/plain",       False),
    Format("xml",        "xml",  ".xml",        "application/xml",  True),
    Format("ini",        "ini",  ".ini",        "text/plain",       True),
    Format("env",        "kv",   ".env",        "text/plain",       True),
    Format("properties", "kv",   ".properties", "text/plain",       True),
)}

_ALIASES = {"yml": "yaml", "dotenv": "env", "kv": "env", "cfg": "ini", "conf": "ini", "config": "ini", "tf": "hcl"}

# What a file name's extension says its format is, for a name the rule gives (a file_pattern, a destination path).
_BY_SUFFIX = {
    ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml", ".hcl": "hcl", ".tf": "hcl", ".xml": "xml",
    ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".env": "env", ".properties": "properties",
}


def target(name: str | None) -> Format | None:
    """The format a rule's ``file_format`` names; None when it names none. ValueError for a name that is not a format."""
    if not name:
        return None
    key = _ALIASES.get(name.strip().lower().lstrip("."), name.strip().lower().lstrip("."))
    if key not in TARGETS:
        raise ValueError(f"file_format {name!r} is not a format varTrack writes: {', '.join(sorted(TARGETS))}")
    return TARGETS[key]


def format_named_by(file_name: str) -> Format | None:
    """The format a file name's extension says (``app.yaml`` is yaml); None when it says none."""
    suffix = Path(file_name).suffix.lower()
    name = _BY_SUFFIX.get(suffix)
    return TARGETS[name] if name else None


def source_format(content: str, file_path: str) -> str:
    """The parser family that reads *file_path* (yaml, json, toml, hcl, xml, ini or kv)."""
    from app.parsers import loader
    return loader.format_of(content, file_path)


def default_target(family: str, file_path: str) -> Format:
    """The format that keeps a source of this family as it is. A kv file is a .properties file by its name, else a .env."""
    if family == "kv":
        return TARGETS["properties" if Path(file_path).suffix.lower() == ".properties" else "env"]
    return TARGETS[family]


# ── Trees ─────────────────────────────────────────────────────────────────────

def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flat(tree: Any) -> dict[str, str]:
    """The tree as the flat keys a sink that stores text gets (the pipeline's own rule), in the tree's order."""
    out: dict[str, str] = {}
    stack: list[tuple[Any, str]] = [(tree, "")]
    while stack:
        node, pfx = stack.pop()
        if isinstance(node, dict):
            for key, value in reversed(list(node.items())):
                stack.append((value, f"{pfx}.{key}" if pfx else str(key)))
        elif isinstance(node, (list, tuple)):
            for i, item in reversed(list(enumerate(node))):
                stack.append((item, f"{pfx}.{i}" if pfx else str(i)))
        else:
            out[pfx] = _text(node)
    return out


def clean_hcl(tree: Any) -> Any:
    """python-hcl2's tree without its quirks: strings without their quotes, no ``__is_block__`` markers, no ``__comments__``."""
    if isinstance(tree, dict):
        return {_unquote(k): clean_hcl(v) for k, v in tree.items() if k not in ("__is_block__", "__comments__")}
    if isinstance(tree, list):
        return [clean_hcl(v) for v in tree]
    return _unquote(tree) if isinstance(tree, str) else tree


def _unquote(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def nest(keys: dict[str, str]) -> Any:
    """Flat dotted keys as a tree: ``a.b`` is ``{"a": {"b": ...}}`` and keys 0, 1, 2 … are a list. A key whose path is
    already a value stays as it is, dotted."""
    root: dict[str, Any] = {}
    for key, value in keys.items():
        parts = key.split(".")
        node = root
        placed = True
        for part in parts[:-1]:
            nxt = node.setdefault(part, {})
            if not isinstance(nxt, dict):
                placed = False
                break
            node = nxt
        if placed and not isinstance(node.get(parts[-1]), dict):
            node[parts[-1]] = value
        else:
            root[key] = value
    return _listify(root)


def _listify(node: Any) -> Any:
    if isinstance(node, dict):
        node = {k: _listify(v) for k, v in node.items()}
        if node and set(node) == {str(i) for i in range(len(node))}:
            return [node[str(i)] for i in range(len(node))]
    return node


def _quote_hcl(tree: Any) -> Any:
    """What python-hcl2 writes as it is given: plain strings become quoted ones (it writes a bare string as a bare word)."""
    if isinstance(tree, dict):
        return {_hcl_key(k): _quote_hcl(v) for k, v in tree.items()}
    if isinstance(tree, list):
        return [_quote_hcl(v) for v in tree]
    if isinstance(tree, str):
        if (len(tree) >= 2 and tree[0] == '"' and tree[-1] == '"') or tree.startswith("${"):
            return tree
        return '"' + tree.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return tree


_HCL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


def _hcl_key(key: Any) -> Any:
    """An object key that is not a bare HCL name (a.b, 0, x y) is written quoted."""
    key = str(key)
    if key == "__is_block__" or _HCL_NAME.match(key) or (len(key) >= 2 and key[0] == '"' and key[-1] == '"'):
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _indent(source_text: str, default: int = 2) -> int | str:
    """The indentation the source uses: its first indented line's width (at most 8), or a tab."""
    for line in source_text.splitlines():
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            lead = line[: len(line) - len(stripped)]
            return "\t" if lead.startswith("\t") else min(len(lead), 8)
    return default


# ── Writers ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rendered:
    text: str
    unwrap: str | None = None   # the root element varTrack added, which a read-back removes


def _find_none(tree: Any, path: str = "") -> str | None:
    if tree is None:
        return path or "(the file)"
    if isinstance(tree, dict):
        for k, v in tree.items():
            found = _find_none(v, f"{path}.{k}" if path else str(k))
            if found:
                return found
    elif isinstance(tree, list):
        for i, v in enumerate(tree):
            found = _find_none(v, f"{path}.{i}" if path else str(i))
            if found:
                return found
    return None


def _write_json(tree: Any, source_text: str, **_: Any) -> Rendered:
    indent = _indent(source_text)
    return Rendered(json.dumps(tree, indent=indent, ensure_ascii=False, default=str) + "\n")


def _write_yaml(tree: Any, source_text: str, **_: Any) -> Rendered:
    import yaml
    indent = _indent(source_text)
    return Rendered(yaml.safe_dump(tree, sort_keys=False, allow_unicode=True, default_flow_style=False,
                                   indent=indent if isinstance(indent, int) and indent > 0 else 2))


def _write_toml(tree: Any, source_text: str, **_: Any) -> Rendered:
    try:
        import tomli_w
    except ImportError as exc:  # pragma: no cover - a dependency of the orchestrator image
        raise RuntimeError("tomli-w is required to write TOML: pip install tomli-w") from exc
    if not isinstance(tree, dict):
        raise Unrenderable("a TOML file is a table of keys; this one is not")
    missing = _find_none(tree)
    if missing:
        raise Unrenderable(f"{missing} is null, and TOML has no null")
    try:
        return Rendered(tomli_w.dumps(tree))
    except (TypeError, ValueError) as exc:
        raise Unrenderable(f"TOML cannot hold this tree: {exc}") from exc


def _write_hcl(tree: Any, source_text: str, **_: Any) -> Rendered:
    import hcl2
    if not isinstance(tree, dict):
        raise Unrenderable("an HCL file is a body of attributes; this one is not")
    try:
        return Rendered(hcl2.dumps(_quote_hcl(tree)))
    except Exception as exc:
        raise Unrenderable(f"HCL cannot hold this tree: {exc}") from exc


_XML_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


def _write_xml(tree: Any, source_text: str, xml_root: str = "config", **_: Any) -> Rendered:
    import xml.etree.ElementTree as ET

    def name_ok(name: str, what: str) -> str:
        if not _XML_NAME.match(name):
            raise Unrenderable(f"{what} {name!r} is not a name XML can use for an element or an attribute")
        return name

    def element(tag: str, value: Any) -> Any:
        el = ET.Element(name_ok(tag, "the key"))
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "@attrs":
                    if not isinstance(v, dict):
                        raise Unrenderable(f"@attrs of {tag!r} is not a table of attributes")
                    for a, av in v.items():
                        el.set(name_ok(str(a), "the attribute"), _text(av))
                elif k == "#text":
                    el.text = _text(v)
                else:
                    for item in (v if isinstance(v, list) else [v]):
                        el.append(element(str(k), item))
        elif isinstance(value, list):
            raise Unrenderable(f"{tag!r} is a list inside a list, which XML cannot repeat")
        else:
            el.text = _text(value)
        return el

    if not isinstance(tree, dict) or not tree:
        raise Unrenderable("an XML file needs at least one element")
    only_key, only_value = next(iter(tree.items()))
    unwrap: str | None = None
    if len(tree) == 1 and not isinstance(only_value, list) and only_key not in ("@attrs", "#text"):
        root = element(str(only_key), only_value)
    else:
        unwrap = name_ok(xml_root, "the root element")
        root = element(unwrap, tree)
    indent = _indent(source_text)
    ET.indent(root, space="\t" if indent == "\t" else " " * int(indent))
    body = ET.tostring(root, encoding="unicode")
    declaration = '<?xml version="1.0" encoding="UTF-8"?>\n' if source_text.lstrip().startswith("<?xml") else ""
    return Rendered(declaration + body + "\n", unwrap)


def _write_ini(tree: Any, source_text: str, **_: Any) -> Rendered:
    keys = flat(tree)
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment]
    top = {k: v for k, v in keys.items() if "." not in k}
    sections: dict[str, dict[str, str]] = {}
    for k, v in keys.items():
        if "." in k:
            section, _dot, rest = k.partition(".")
            sections.setdefault(section, {})[rest] = v
    if top and sections:
        raise Unrenderable(
            f"{sorted(top)[0]!r} is outside any section, next to the sections {sorted(sections)[:3]}: INI would read it into every section")
    if top:
        parser.read_dict({"DEFAULT": top})
    for section, values in sections.items():
        if section.upper() == "DEFAULT":
            raise Unrenderable(f"{section!r} is the name of INI's default section, so it cannot be a section of the file")
        parser.read_dict({section: values})
    buf = io.StringIO()
    parser.write(buf)
    return Rendered(buf.getvalue())


def _write_kv(tree: Any, source_text: str, properties: bool = False, **_: Any) -> Rendered:
    lines = []
    for k, v in flat(tree).items():
        if not properties and (v != v.strip() or v[:1] in ('"', "'")):
            v = '"' + v + '"'      # quoted, so that its edges are kept: the key=value reader strips the quotes
        lines.append(f"{k}={v}")
    return Rendered("\n".join(lines) + ("\n" if lines else ""))


_WRITERS: dict[str, Callable[..., Rendered]] = {
    "json": _write_json, "yaml": _write_yaml, "toml": _write_toml, "hcl": _write_hcl, "xml": _write_xml,
    "ini": _write_ini, "env": _write_kv, "properties": lambda t, s, **kw: _write_kv(t, s, properties=True),
}


# ── Reading back ──────────────────────────────────────────────────────────────

def parse(text: str, fmt: Format | str, file_path: str = "") -> Any:
    """*text* parsed by the format's own parser. ValueError when it does not parse."""
    from app.parsers import loader
    family = fmt.family if isinstance(fmt, Format) else fmt
    try:
        tree = loader._parse_with(family, text, file_path or "file" + (fmt.suffix if isinstance(fmt, Format) else ""))
    except Exception as exc:
        raise ValueError(f"does not parse as {family}: {exc}") from exc
    if tree is None:
        raise ValueError(f"does not parse as {family}")
    return tree


def read_back(text: str, fmt: Format, unwrap: str | None = None) -> dict[str, str]:
    """The flat keys *text* reads as when parsed as *fmt*: what a sink compares to Git for drift."""
    tree = parse(text, fmt)
    if fmt.family == "hcl":
        tree = clean_hcl(tree)
    if unwrap and isinstance(tree, dict) and set(tree) == {unwrap}:
        tree = tree[unwrap]
    return flat(tree)


def stored_flat(text: str, fmt: Format, *, source_family: str | None = None, xml_root: str = "config") -> dict[str, str]:
    """
    The flat keys a stored file reads as, in the form Git's file has when the pipeline flattens it: what a sink compares to
    Git for drift. *source_family* is the parser family of the file in Git; left out, it is the stored file's own (the
    format was kept). A file converted INTO XML may carry the root element varTrack added; one converted INTO HCL reads
    with its quotes, which the pipeline's own flatten of Git's file has only when that file is HCL too.
    """
    tree = parse(text, fmt)
    if source_family and source_family != fmt.family:
        if fmt.family == "hcl":
            tree = clean_hcl(tree)
        elif fmt.family == "xml" and isinstance(tree, dict) and set(tree) == {xml_root}:
            tree = tree[xml_root]
    return flat(tree)


def render(fmt: Format | str, tree: Any, *, source_family: str = "", source_text: str = "", xml_root: str = "config") -> str:
    """
    *tree* written in *fmt*, and read back to check it. Raises Unrenderable, naming the key, when it would not read as
    what it was.

    *source_family* is the parser family the tree came from: a flat kv tree is nested by its dots for a format that holds
    a tree, and python-hcl2's quirks are dropped from an HCL tree that goes to another format.
    """
    target_fmt = fmt if isinstance(fmt, Format) else TARGETS[fmt]
    if source_family == "hcl" and target_fmt.family != "hcl":
        tree = clean_hcl(tree)
    if source_family in ("kv", "ini") and target_fmt.family in ("yaml", "json", "toml", "hcl", "xml"):
        tree = nest(flat(tree))
    rendered = _WRITERS[target_fmt.name](tree, source_text, xml_root=xml_root)

    intended = flat(clean_hcl(tree) if target_fmt.family == "hcl" else tree)
    try:
        got = read_back(rendered.text, target_fmt, rendered.unwrap)
    except ValueError as exc:
        raise Unrenderable(f"what was written for {target_fmt.name} {exc}") from exc
    if got != intended:
        for key in list(intended) + [k for k in got if k not in intended]:
            if got.get(key) != intended.get(key):
                raise Unrenderable(
                    f"{key!r} would not read back from {target_fmt.name} as it is: "
                    f"{intended.get(key)!r} is written and {got.get(key)!r} is read")
    return rendered.text
