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
1. Choose the schema (Validator.choose), in this order:
     the rule's schema_map: the first entry whose glob matches the file's path   "none" = not checked, on purpose
     the file's own $schema, when the rule has schema_from_file
     <schema_dir>/<file_name>.cue   e.g.  package.json.cue
     <schema_dir>/default.cue       fallback
   A schema the map or the file names that is not in schema_dir is an error, never a fall-back to the next.

2. Convert flat_data dict to a CUE value literal:
     { "database.host": "localhost", "database.port": "5432" }
     becomes:
     { "database.host": "localhost", "database.port": "5432" }

3. Run:
     cue vet <schema_file> <data_file>

4. If cue vet exits non-zero → CUEValidationError

If no schema file is found, nothing checks the file: the caller reports validation "none", and fails it under
strict_validation.
If the CUE binary is not installed there is no way to check: validate() raises CUEUnavailable, which
fails the file under strict_validation and marks it unchecked otherwise. Only an operator who named
the "schema_validation" degraded mode (VARTRACK_ALLOW_DEGRADED) gets files written unvalidated.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.pipeline import schema_paths

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


class CUEUnavailable(CUEValidationError):
    """A schema exists for the file, but the cue program that checks it is not installed."""


@dataclass(frozen=True)
class SchemaChoice:
    """Which schema checks a file, and why: what Validator.choose decided."""
    path:   Optional[Path]   # the schema file to check with; None when nothing checks the file
    name:   str              # the schema in the schema repository ("api/settings.cue"), or "none"
    source: str              # schema_map[N] | file | name | default | none
    note:   str = ""         # said in the file's result: a $schema that was overridden or ignored
    error:  str = ""         # an explicit choice that cannot be honoured: the file fails with this

    @property
    def checked(self) -> bool:
        return self.path is not None


_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


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

    def choose(
        self,
        file_path: str,
        rule_config: Optional[dict] = None,
        file_schema: Any = None,
        *,
        tenant_id: str = "",
        commit: str = "",
    ) -> SchemaChoice:
        """The schema for the file at *file_path* (its path in the repository), in the order of the module
        docstring. *file_schema* is the file's own top-level ``$schema``, read by the caller; it counts only when
        the rule has schema_from_file."""
        rule = rule_config or {}
        where = f"the schema repository of tenant {tenant_id or '?'}" + (f" at {commit[:12]}" if commit else "")
        note = ""
        from_file = file_schema if rule.get("schema_from_file") and isinstance(file_schema, str) else None

        for i, entry in enumerate(rule.get("schema_map") or []):
            if not isinstance(entry, dict) or not schema_paths.glob_match(str(entry.get("match", "")), file_path):
                continue
            source = f"schema_map[{i}]"
            if from_file is not None and from_file != entry.get("schema"):
                note = f"the file's $schema ({from_file}) was overridden by {source}"
            return self._named(str(entry.get("schema", "")), source, f"{source}, {entry.get('match')}", where, note)

        if from_file is not None:
            if _URL.match(from_file):
                note = f"the file's $schema ({from_file}) is a URL, not a schema in the schema repository: ignored"
            else:
                return self._named(from_file, "file", "$schema of the file", where, note)

        found = self._find_schema(Path(file_path).name)
        if found is not None:
            return SchemaChoice(found, found.name, "name" if found.name != "default.cue" else "default", note)
        return SchemaChoice(None, schema_paths.NONE, "none", note)

    def _named(self, schema: str, source: str, why: str, where: str, note: str) -> SchemaChoice:
        """A schema the operator (or the file) named: "none", or one that has to exist."""
        if schema == schema_paths.NONE:
            return SchemaChoice(None, schema_paths.NONE, source, note)
        problem = schema_paths.schema_problem(schema)
        if problem:
            return SchemaChoice(None, schema, source, note, error=f"schema {schema!r} ({why}) {problem}")
        missing = f"schema {schema} ({why}) is not in {where}"
        if self._schema_dir is None or not Path(self._schema_dir).is_dir():
            return SchemaChoice(None, schema, source, note, error=missing)
        try:
            path = schema_paths.join_inside(Path(self._schema_dir), schema)
        except ValueError as exc:
            return SchemaChoice(None, schema, source, note, error=f"{exc} ({why})")
        if not path.is_file():
            return SchemaChoice(None, schema, source, note, error=missing)
        return SchemaChoice(path, schema, source, note)

    def validate(
        self,
        flat_data: dict[str, str],
        file_name: str,
        strict: bool = False,
        schema_path: Optional[Path] = None,
    ) -> bool:
        """
        Validate *flat_data* against the CUE schema for *file_name*.

        Parameters
        ----------
        flat_data  : { "database.host": "localhost", ... }
        file_name  : basename of the source file, e.g. "package.json"
        schema_path: the schema choose() picked; left out, the file name's schema is looked up
        strict     : if True, raises CUEValidationError on failure;
                     if False, logs a warning and returns

        Returns True when the file was checked and passed, False when nothing checked it (no schema, or the
        degraded mode that writes files unvalidated); a failure is logged (strict=False) or raised.

        Raises
        ------
        CUEValidationError — only when strict=True and validation fails
        """
        if schema_path is None:
            schema_path = self._find_schema(file_name)
        if schema_path is None:
            logger.debug("validator: no schema for file=%s, skipping", file_name)
            return False

        if not self._is_cue_available():
            from app import dependencies

            reason = f"cannot validate {file_name}: the {_CUE_BIN!r} program is not installed"
            if dependencies.allowed("schema_validation"):
                logger.warning("%s — writing it unvalidated (VARTRACK_ALLOW_DEGRADED=schema_validation)", reason)
                dependencies.mode_on("schema_validation", reason)
                return False
            raise CUEUnavailable(reason)

        error = self._run_cue_vet(flat_data, schema_path, file_name)
        if error:
            msg = f"CUE validation failed for {file_name}: {error}"
            if strict:
                raise CUEValidationError(msg)
            logger.warning("validator: %s", msg)
            return False
        return True

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
