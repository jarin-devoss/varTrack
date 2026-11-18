"""
app/parsers/hcl_parser.py
──────────────────────────
Parse HashiCorp Configuration Language (HCL) files:
  .tf, .hcl

Uses python-hcl2 when available; falls back to the kv_parser for
simple  key = "value"  assignments. A .tfvars file does not come here: the
detector reads it as key=value lines.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse(content: str, file_path: str = "") -> Any:
    try:
        import hcl2       # type: ignore   pip install python-hcl2
        import io
        if hasattr(hcl2, "loads"):
            # Without comments: python-hcl2 would make each one a ``__comments__`` key, a setting nobody wrote, which a sink
            # that writes the file back (it cannot write comments) would then never hold.
            try:
                return hcl2.loads(content, serialization_options=hcl2.SerializationOptions(with_comments=False))
            except TypeError:       # a python-hcl2 without the option
                return hcl2.loads(content)
        return hcl2.load(io.StringIO(content))
    except ImportError:
        logger.debug(
            "python-hcl2 not installed – falling back to KV parser for %s", file_path
        )
    except Exception as exc:
        logger.warning(
            "python-hcl2 parse error for %s (%s) – falling back to KV parser",
            file_path, exc,
        )
    from app.parsers import kv_parser
    return kv_parser.parse(content, file_path)
