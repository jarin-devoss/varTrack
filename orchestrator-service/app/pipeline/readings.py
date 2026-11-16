"""
app/pipeline/readings.py
────────────────────────
The data `cue vet` is shown: each value with the readings it can have.

A config value is text or a typed value, depending on the file: `port: 8080` is an int in YAML, JSON, TOML and HCL;
`port: "8080"` and `PORT=8080` in a .env, .properties, INI or XML file are text. A schema says what it means with
its own type (`int & >=1024`, `string & =~"^[0-9]+$"`, `bool`). So each value is written for CUE as the form the
file gave it, as the default, next to the other form it can be read as:

    port: 8080        →  *8080 | "8080"
    PORT=8080         →  *"8080" | 8080
    debug: true       →  *true | "true"
    zip: "0123"       →  "0123"             (a leading zero is never a number)
    PORT=             →  *"" | null         (an empty value is also null: `int | null` says "a number, or left empty")

Nothing here changes a value a sink writes: the readings exist only in the file `cue vet` reads.

Which text reads as a number or a boolean is decided by standard parsers, not by patterns:

    number  the text, with no space around it, that json.loads reads as an int or a float (JSON's number grammar:
            no leading zero, sign, separator or base: not 0123, +5, 1_000, 0x1F, .5, 5.; not NaN or Infinity)
    bool    text.lower() is "true" or "false"; not yes, no, on, off, 1, 0

`json.loads` makes nothing CUE reads: it is the gate. Every number it accepts is also a valid CUE number literal with the
same value, so the text itself is written as the number reading. CUE's own grammar is wider (0x1F is 31, 1_000 is 1000)
and is not used for the decision.
"""
from __future__ import annotations

import json
import math
from typing import Any, Mapping


def cue_string(text: str) -> str:
    """*text* as a CUE string literal. JSON's escapes are CUE's, except for characters outside the BMP and lone surrogates."""
    out = []
    for ch in json.dumps(text, ensure_ascii=False):
        code = ord(ch)
        if code > 0xFFFF:
            out.append("\\U%08x" % code)
        elif 0xD800 <= code <= 0xDFFF:
            out.append("\\ufffd")
        else:
            out.append(ch)
    return "".join(out)


def _refuse(constant: str) -> Any:
    raise ValueError(constant)       # NaN, Infinity, -Infinity are not numbers here


def is_number(text: str) -> bool:
    """Whether *text*, as it stands, is a number in JSON's grammar (an int or a float)."""
    # The first character keeps json.loads away from arrays and objects (and their nesting), and ASCII keeps it to
    # the digits JSON means. A space, a sign other than "-" or a leading zero fails in the parser itself.
    if not text or not text.isascii() or text[0] not in "-0123456789" or text != text.strip():
        return False
    try:
        value = json.loads(text, parse_constant=_refuse)
    except ValueError:               # not JSON, or an int with more digits than Python reads
        return False
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def other_reading(text: str) -> str | None:
    """The number or boolean *text* reads as (as a CUE literal), or None when it reads as text only."""
    if is_number(text):
        return text
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered
    return None


def value_expr(text: str, own: Any = None) -> str:
    """
    The CUE for one value: *text* is what flattening wrote, *own* the value as the file had it when it is a bool, int
    or float. Left out, the file's own form is the text.
    """
    if isinstance(own, bool):
        return f"*{'true' if own else 'false'} | {cue_string(text)}"
    if isinstance(own, (int, float)):
        return f"*{text} | {cue_string(text)}" if is_number(text) else cue_string(text)
    reading = other_reading(text)
    if reading is None:
        return cue_string(text)
    return f"*{cue_string(text)} | {reading}"


def data_for_cue(flat_data: Mapping[str, str], own: Mapping[str, Any] | None = None) -> str:
    """The `.cue` file `cue vet` is given: one field per key, one line per field, each with its readings.

    The data file declares no package: it unifies with a schema that has one. One line per key is what lets an error that
    cites a line of this file be mapped back to its key (validator._with_values)."""
    own = own or {}
    lines = []
    for key, text in flat_data.items():
        form = own.get(key, text)
        if text == "" and (form is None or isinstance(form, str)):
            # An empty value is also null (an empty field is null to every reader of text), and the file's own form
            # is the default: a null stays null, an empty string stays "".
            expr = '*null | ""' if form is None else '*"" | null'
        elif isinstance(form, str):
            expr = value_expr(text)
        else:
            expr = value_expr(text, form)
        lines.append(f"{cue_string(str(key))}: {expr}")
    return "\n".join(lines) + "\n"
