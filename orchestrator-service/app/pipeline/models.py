from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class PayloadContext:
    """Output of Stage 1 — everything extracted from the raw webhook."""
    platform:       str
    repo_url:       str
    ref:            str
    branch:         str
    commit_sha:     str
    tag:            str | None
    pr_number:      str | None
    rule_config:    dict
    parsed_payload: dict   # the raw parsed JSON — needed by file_collector strategy 3

@dataclass
class ETLFile:
    """One transformed file produced by Stage 2."""
    file_path:     str
    env:           str
    flat_data:     dict[str, str]
    root_key:      str
    validation:    Literal["ok", "warn", "failed"] = "ok"
    error:         str = ""
    # Fields annotated @logger() in the CUE schema — change-log is emitted for
    # these keys on every write.  Empty frozenset when no schema or no @logger.
    logger_fields: frozenset[str] = field(default_factory=frozenset)
    # Fields annotated @secret() — used by change_logger to mask values in logs.
    secret_fields: frozenset[str] = field(default_factory=frozenset)

@dataclass
class ETLResult:
    """Output of Stage 2 — all transformed files."""
    files:   list[ETLFile] = field(default_factory=list)
    skipped: list[str]     = field(default_factory=list)  # empty / parse-fail / no match
    # Distinguishes "legitimately skipped" (empty file, root_key not found,
    # validation failed) from "errored" (unexpected exception during parse/transform).
    # A non-empty errors list signals the task layer to fail or alert.
    errors:  list[str]     = field(default_factory=list)  # unexpected exceptions

@dataclass
class SyncResult:
    """One file's sync outcome from Stage 3."""
    file_path: str
    env:       str
    status:    Literal["ok", "empty", "validation_failed", "error", "skipped", "aborted"]
    sync_mode: str = ""
    written:   int = 0
    pruned:    int = 0
    error:     str = ""
    root_key:  str = ""
