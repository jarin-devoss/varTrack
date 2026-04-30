"""
app/pipeline/change_logger.py
──────────────────────────────
Parse CUE @logger-annotated fields and emit structured change-log entries.

CUE @logger annotation syntax
──────────────────────────────
  app_port:          int    @logger()
  database.host:     string @logger()
  database.password: string @secret() @logger()   # secret → value masked in logs

When a @logger-annotated field changes value between ETL runs, a structured
log record is emitted at INFO level:

  Non-secret field:
    field changed  datasource=mongo  env=production  field=app_port
                   from=8080  to=9090  file=configs/app.yaml

  Secret field (also has @secret):
    field changed  datasource=mongo  env=production  field=database.password
                   changed=true  file=configs/app.yaml
    (old/new values are never logged for secret fields)
"""
from __future__ import annotations

import logging
import re
from collections.abc import Collection
from pathlib import Path
from typing import Optional

_log = logging.getLogger("vartrack.change_logger")

# Matches: "field.name": <type> @logger()
# Group 1: field name (quoted or bare)
_LOGGER_ANNOTATION_RE = re.compile(
    r'^\s*"?([^":]+?)"?\s*:'   # field name (quoted or bare)
    r'[^@\n]*'                  # anything before the annotation on the same line
    r'@logger\(\s*\)',
    re.MULTILINE,
)


def parse_logger_fields(schema_path: Optional[Path]) -> frozenset[str]:
    """
    Parse a CUE schema file and return all @logger-annotated field names.

    Parameters
    ----------
    schema_path:
        Absolute path to the .cue schema file, or None.

    Returns
    -------
    frozenset of field names (as they appear in flat_data after transform).
    """
    if not schema_path or not schema_path.exists():
        return frozenset()

    try:
        text = schema_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log.warning("change_logger: cannot read schema %s: %s", schema_path, exc)
        return frozenset()

    fields: set[str] = set()
    for m in _LOGGER_ANNOTATION_RE.finditer(text):
        field_name = m.group(1).strip()
        fields.add(field_name)
        _log.debug("change_logger: found @logger field=%r in %s", field_name, schema_path.name)

    return frozenset(fields)


def emit_field_changes(
    *,
    logger_fields: frozenset[str],
    secret_fields: Collection[str],
    old_values: dict[str, str],
    new_values: dict[str, str],
    datasource: str,
    env: str,
    file_path: str,
) -> None:
    """
    Compare old vs new values for @logger-annotated fields and emit change logs.

    For each field in logger_fields that exists in new_values:
    - If the value changed (or there was no old value):
        - Non-secret → log from/to values
        - Secret     → log that it changed without revealing values

    Parameters
    ----------
    logger_fields:
        Fields annotated with @logger() in the CUE schema.
    secret_fields:
        Fields also annotated with @secret() — values are masked in logs.
    old_values:
        Values read from the sink before this write (may be empty if the
        sink does not implement read_values).
    new_values:
        The flat_data being written to the sink.
    datasource:
        Datasource name (e.g. "mongo", "mongo-primary").
    env:
        Environment name (e.g. "production", "pr-42").
    file_path:
        Config file path being synced.
    """
    secret_set = frozenset(secret_fields)

    for field in logger_fields:
        if field not in new_values:
            continue

        new_val = new_values[field]
        old_val = old_values.get(field)

        if old_val == new_val:
            continue  # no change — nothing to log

        is_secret = field in secret_set

        if is_secret:
            _log.info(
                "field changed  datasource=%s  env=%s  field=%s  changed=true  file=%s",
                datasource, env, field, file_path,
            )
        elif old_val is None:
            # Field is new (first write or sink didn't return old value)
            _log.info(
                "field changed  datasource=%s  env=%s  field=%s  to=%r  file=%s",
                datasource, env, field, new_val, file_path,
            )
        else:
            _log.info(
                "field changed  datasource=%s  env=%s  field=%s  from=%r  to=%r  file=%s",
                datasource, env, field, old_val, new_val, file_path,
            )
