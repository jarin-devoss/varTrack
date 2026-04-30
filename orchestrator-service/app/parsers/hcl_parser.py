"""
app/parsers/hcl_parser.py
──────────────────────────
Parse HashiCorp Configuration Language (HCL) files:
  .tf, .tfvars, .hcl

Uses python-hcl2 when available; falls back to the kv_parser for
simple  key = "value"  assignments in .tfvars files.
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
