"""
app/pipeline/validator.py
──────────────────────────
CUE schema validator for flat config data.

Architecture
────────────
Schema review is separated from schema application — the validator runs
checks independently, returning structured results, and only the caller
decides whether to reject or warn:

  Validator.validate(flat_data, file_name, strict=False)
      → raises CUEValidationError  if strict=True and validation fails
      → logs a warning             if strict=False and validation fails
      → returns None               on success

CUE validation process
──────────────────────
1. Find the matching .cue schema file in schema_dir:
     <schema_dir>/<file_name>.cue   e.g.  package.json.cue
     <schema_dir>/default.cue       fallback

2. Convert flat_data dict to a CUE value literal:
     { "database.host": "localhost", "database.port": "5432" }
     becomes:
     { "database.host": "localhost", "database.port": "5432" }

3. Run:
     cue vet <schema_file> <data_file>

4. If cue vet exits non-zero → CUEValidationError

If no schema file is found, validation is skipped (no error).
If the CUE binary is not installed, validation is skipped with a warning.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CUE_BIN = os.getenv("CUE_BINARY", "cue")
_CUE_TIMEOUT = int(os.getenv("CUE_TIMEOUT_SECONDS", "15"))
def _parse_cue_version(version_line: str) -> tuple[int, ...]:
    """Extract (major, minor, patch) from 'cue version v0.6.0 linux/amd64'."""
    import re
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)", version_line)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

_CUE_MIN_VERSION: tuple[int, ...] = _parse_cue_version(
    os.getenv("CUE_MIN_VERSION", "v0.6.0")
) or (0, 6, 0)


class CUEValidationError(Exception):
    """Raised when CUE schema validation fails in strict mode."""


class Validator:
    """
    CUE-based flat-data validator.

    Parameters
    ----------
    schema_dir : Path to the tenant's CUE schema directory (from SchemaRegistry).
                 Pass None to skip validation entirely.
    """

    def __init__(self, schema_dir: Optional[Path] = None) -> None:
        self._schema_dir = schema_dir
        self._cue_available: Optional[bool] = None   # lazy-check

    def validate(
        self,
        flat_data: dict[str, str],
        file_name: str,
        strict: bool = False,
    ) -> None:
        """
        Validate *flat_data* against the CUE schema for *file_name*.

        Parameters
        ----------
        flat_data  : { "database.host": "localhost", ... }
        file_name  : basename of the source file, e.g. "package.json"
        strict     : if True, raises CUEValidationError on failure;
                     if False, logs a warning and returns

        Raises
        ------
        CUEValidationError — only when strict=True and validation fails
        """
        schema_path = self._find_schema(file_name)
        if schema_path is None:
            logger.debug("validator: no schema for file=%s, skipping", file_name)
            return

        if not self._is_cue_available():
            logger.warning(
                "validator: CUE binary %r not found — skipping validation for %s",
                _CUE_BIN, file_name,
            )
            return

        error = self._run_cue_vet(flat_data, schema_path, file_name)
        if error:
            msg = f"CUE validation failed for {file_name}: {error}"
            if strict:
                raise CUEValidationError(msg)
            logger.warning("validator: %s", msg)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _find_schema(self, file_name: str) -> Optional[Path]:
        """Return the most-specific matching .cue schema file, or None."""
        if self._schema_dir is None:
            return None

        schema_dir = Path(self._schema_dir)
        if not schema_dir.is_dir():
            return None

        # Exact match: package.json → package.json.cue
        candidate = schema_dir / f"{file_name}.cue"
        if candidate.exists():
            return candidate

        # Fallback: default.cue
        default = schema_dir / "default.cue"
        if default.exists():
            return default

        return None

    def _is_cue_available(self) -> bool:
        """Check CUE binary availability and minimum version (cached after first call)."""
        if self._cue_available is not None:
            return self._cue_available
        try:
            result = subprocess.run(
                [_CUE_BIN, "version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            version_line = (result.stdout or "").strip().split("\n")[0]
            detected = _parse_cue_version(version_line)
            if detected < _CUE_MIN_VERSION:
                logger.warning(
                    "validator: CUE binary version %s is below minimum %s — "
                    "schema syntax may differ; set CUE_MIN_VERSION to suppress",
                    ".".join(str(x) for x in detected),
                    ".".join(str(x) for x in _CUE_MIN_VERSION),
                )
            else:
                logger.debug(
                    "validator: CUE binary version %s ok (min=%s)",
                    ".".join(str(x) for x in detected),
                    ".".join(str(x) for x in _CUE_MIN_VERSION),
                )
            self._cue_available = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._cue_available = False
        return self._cue_available

    def _run_cue_vet(
        self,
        flat_data: dict[str, str],
        schema_path: Path,
        file_name: str,
    ) -> Optional[str]:
        """
        Write flat_data to a temp JSON file and run `cue vet`.

        Returns the error message from CUE on failure, or None on success.
        """
        # CUE vet expects the data as a JSON/CUE value.
        # We pass it as a JSON file alongside the schema.
        data_json = json.dumps(flat_data, indent=2)

        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                prefix="vt_cue_",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                tmp.write(data_json)
                tmp_path = tmp.name

            result = subprocess.run(
                [_CUE_BIN, "vet", str(schema_path), tmp_path],
                capture_output=True,
                text=True,
                timeout=_CUE_TIMEOUT,
            )

            if result.returncode != 0:
                error = (result.stderr or result.stdout or "unknown CUE error").strip()
                logger.debug(
                    "validator: cue vet failed file=%s schema=%s: %s",
                    file_name, schema_path.name, error,
                )
                return error

            logger.debug(
                "validator: cue vet passed file=%s schema=%s",
                file_name, schema_path.name,
            )
            return None

        except subprocess.TimeoutExpired:
            return f"CUE vet timed out after {_CUE_TIMEOUT}s"
        except Exception as exc:
            logger.exception("validator: unexpected error running cue vet")
            return str(exc)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
