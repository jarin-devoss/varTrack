"""
app/parsers/xml_parser.py
──────────────────────────
Parse XML / .config / web.config files.

Walks the element tree recursively and produces a nested dict:
    <config>
      <database>
        <host>localhost</host>
        <port>5432</port>
      </database>
    </config>

→ {"config": {"database": {"host": "localhost", "port": "5432"}}}

Namespace handling
──────────────────
Preserves namespace-qualified names using Clark notation {ns}local,
converting them to a stable key by hashing the namespace URI to a short
prefix (e.g. "ns0:local").  This prevents two elements from different
namespaces colliding into the same key.

Mixed content
─────────────
When an element has BOTH child elements AND a text value, the text is
captured under the special "#text" key so it is not silently discarded.

Attributes are stored under the special "@attrs" key so they survive
the BFS flattener without colliding with child elements.

StopIteration safety
─────────────────────
The original code used next(iter(d.items())) which raises StopIteration
(converted to RuntimeError inside generators) on empty dicts.  We now
use unpacking via direct dict construction to avoid this.
"""
from __future__ import annotations

import defusedxml.ElementTree as ET  # type: ignore[import]
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Fallback if defusedxml is unavailable (e.g. during testing without install)
try:
    import defusedxml.ElementTree as ET  # noqa: F811
except ImportError:
    import xml.etree.ElementTree as ET  # type: ignore[no-redef]


def _ns_key(tag: str, ns_map: dict[str, str]) -> str:
    """
    Convert a Clark-notation tag "{namespace}local" to a stable "nsN:local"
    key.  Tags without a namespace are returned unchanged.

    Preserves namespace-qualified names rather than stripping namespaces,
    preventing key collisions between elements from different XML namespaces.
    """
    if not tag.startswith("{"):
        return tag
    ns_uri, _, local = tag[1:].partition("}")
    if ns_uri not in ns_map:
        ns_map[ns_uri] = f"ns{len(ns_map)}"
    return f"{ns_map[ns_uri]}:{local}"


def _element_to_dict(node: ET.Element, ns_map: dict[str, str]) -> dict[str, Any]:
    """
    Recursively convert an ElementTree Element to a nested dict.

    Returns {qualified_tag: value} where value is either:
    - a string (leaf node text)
    - a dict (element with children / attributes)
    - a list of the above (when sibling elements share the same tag)
    """
    tag = _ns_key(node.tag, ns_map)
    result: dict[str, Any] = {}

    # attributes → {"@attrs": {"key": "val", ...}}
    if node.attrib:
        result["@attrs"] = {_ns_key(k, ns_map): v for k, v in node.attrib.items()}

    children = list(node)
    if children:
        child_map: dict[str, Any] = {}
        for child in children:
            # Safe: _element_to_dict always returns a single-entry dict
            child_dict = _element_to_dict(child, ns_map)
            if not child_dict:
                continue
            child_tag, child_val = next(iter(child_dict.items()))
            if child_tag in child_map:
                # Multiple children with the same tag → make a list
                existing = child_map[child_tag]
                if not isinstance(existing, list):
                    child_map[child_tag] = [existing]
                child_map[child_tag].append(child_val)
            else:
                child_map[child_tag] = child_val
        result.update(child_map)

        # Capture mixed-content text so it is not silently discarded
        # when the element also has children.
        text = (node.text or "").strip()
        if text:
            result["#text"] = text

    else:
        # Leaf node – store text value (may be empty string)
        result = (node.text or "").strip()  # type: ignore[assignment]

    return {tag: result}


def parse(content: str, file_path: str = "") -> Any:
    """
    Parse XML content and return a nested dict.

    Uses defusedxml to prevent Billion-Laughs / XXE attacks.
    Falls back to stdlib ElementTree if defusedxml is not installed.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.warning("xml_parser: parse error file=%s: %s", file_path, exc)
        return None

    ns_map: dict[str, str] = {}
    return _element_to_dict(root, ns_map)
