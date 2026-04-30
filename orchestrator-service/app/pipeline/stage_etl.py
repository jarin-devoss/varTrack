"""
app/pipeline/stage_etl.py
──────────────────────────
Stage 2: Extract → Transform → Validate.

Order of operations per file
──────────────────────────────
  1. Parse raw file content          (loader.load)
  2. Extract root_key subtree        (rule_config["root_key"])
  3. Resolve environment string      (env_resolver.resolve)
  4. Env-aware slice                 (env_slice.resolve_env_slice)   ← NEW
  5. CUE-validate the raw flat data
  6. Apply variables_map overlay

Step 4 detail
─────────────
After root_key extraction the data may be env-keyed in one of two patterns:

  Pattern 1 — top-level keys are env names:
    { "predev": { "color": "red" }, "default": { "color": "blue" } }

  Pattern 2 — per-key env sub-keys mixed with scalars:
    { "color": { "predev": "green", "default": "blue" }, "age": 43 }

Both patterns are auto-detected.  No rule config flag needed.
If the resolved env is a PR / branch / tag that doesn't exist as a key,
the slice falls back to "default" silently.

Auto-provision
──────────────
When the resolved env is new (e.g. "pr-42" first time), each sink is
responsible for creating the required resource:
  - MongoDB      → new collection (implicit on first insert)
  - ConfigMap    → new ConfigMap object
  - LinuxServer  → new file on disk
The resolved env string is forwarded unchanged; sinks handle provisioning.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.pipeline.models import PayloadContext, ETLResult, ETLFile
from app.tasks.file_collector import collect as collect_files
from app.utils.enums.apply_strategy import ApplyStrategy

logger = logging.getLogger(__name__)

_DEFAULT_ROOT_KEY = "vartrack"


def _record_metric(outcome: str, key_count: int = 0) -> None:
    """Increment ETL file/key counters; silently ignored if metrics unavailable."""
    try:
        from app.monitoring import get_metrics
        m = get_metrics()
        if m:
            m.inc_etl_file(outcome)
            if key_count:
                m.inc_etl_keys(key_count)
    except Exception:
        pass


def _resolve_apply_strategy(rule_config: dict) -> ApplyStrategy:
    raw = rule_config.get("apply_strategy", ApplyStrategy.CLIENT_SIDE)
    try:
        return ApplyStrategy(int(raw))
    except (ValueError, TypeError, KeyError):
        pass
    name = str(raw).upper().removeprefix("APPLY_STRATEGY_")
    return ApplyStrategy[name] if name in ApplyStrategy.__members__ else ApplyStrategy.CLIENT_SIDE


def run_stage_etl(ctx: PayloadContext, datasource: str, tenant_id: str) -> ETLResult:
    from app.pipeline import env_resolver, transformer
    from app.pipeline.env_slice import resolve_env_slice, detect_pattern
    from app.pipeline.validator import Validator, CUEValidationError
    from app.pipeline.secret_resolver import parse_secret_fields, resolve_secrets, mask_secrets
    from app.pipeline.change_logger import parse_logger_fields
    from app.schema_registry.manager import get_schema_manager
    from app.parsers.loader import load as parse_file

    rule_config     = ctx.rule_config
    root_key        = rule_config.get("root_key", _DEFAULT_ROOT_KEY)
    variables_map   = rule_config.get("variables_map", {})
    strict_validate = rule_config.get("strict_validation", False)

    apply_strategy = _resolve_apply_strategy(rule_config)
    if apply_strategy != ApplyStrategy.CLIENT_SIDE:
        logger.warning("apply_strategy=%s not supported; falling back to CLIENT_SIDE", apply_strategy.name)

    raw_files: list[tuple[str, str]] = collect_files(
        payload=ctx.parsed_payload,
        platform=ctx.platform,
        rule_config=rule_config,
        repo_url=ctx.repo_url,
        ref=ctx.ref,
    )

    schema_dir = get_schema_manager().tenant_schema_root(tenant_id)
    validator  = Validator(schema_dir=schema_dir if schema_dir.exists() else None)

    result = ETLResult()

    for file_path, content in raw_files:
        try:
            # ── 3. Resolve environment ────────────────────────────────────────
            env = env_resolver.resolve(
                branch=ctx.branch,
                file_path=file_path,
                pr_number=ctx.pr_number,
                tag=ctx.tag,
                branch_map=rule_config.get("branch_map", {}),
                file_path_map=rule_config.get("file_path_map", {}),
                env_as_branch=rule_config.get("env_as_branch", False),
                env_as_pr=rule_config.get("env_as_pr", False),
                env_as_tags=rule_config.get("env_as_tags", False),
            )

            # ── 1. Parse raw content ──────────────────────────────────────────
            parsed = parse_file(content, file_path)

            # ── 2. Extract root_key subtree ───────────────────────────────────
            # When parsed is a list (e.g. multi-doc YAML or JSON array at root),
            # root_key extraction is skipped and the whole document is used as root.
            if root_key:
                if isinstance(parsed, dict):
                    if root_key in parsed:
                        parsed = parsed[root_key]
                        logger.debug("root_key=%s extracted from %s", root_key, file_path)
                    elif root_key != _DEFAULT_ROOT_KEY:
                        logger.warning("root_key=%s not found in %s, skipping", root_key, file_path)
                        result.skipped.append(file_path)
                        continue
                    # else: _DEFAULT_ROOT_KEY not present — treat whole file as root
                else:
                    logger.debug(
                        "root_key=%s requested but parsed type=%s is not a dict; "
                        "treating whole document as root for file=%s",
                        root_key, type(parsed).__name__, file_path,
                    )

            # ── 4. Env-aware slice ────────────────────────────────────────────
            # Works for any resolved env: "pr-42", "feature/login", "v1.2.3" …
            # Falls back to "default" key if env not found in data.
            # Returns data unchanged if neither pattern is detected.
            if isinstance(parsed, dict):
                pattern = detect_pattern(parsed, env)
                parsed  = resolve_env_slice(parsed, env)
                if pattern != "none":
                    logger.info("env_slice file=%s env=%s pattern=%s", file_path, env, pattern)

            # Re-serialise sliced dict so transformer sees clean JSON
            sliced_content = (
                json.dumps(parsed) if isinstance(parsed, (dict, list)) else content
            )

            # ── 5. Flatten + apply variables_map overlay ─────────────────────
            # CUE validates the final pushed variable names (after variables_map),
            # not the raw config keys, so transform() is called once before validate().
            flat_data = transformer.transform(
                content=sliced_content,
                file_path=file_path,
                variables_map=variables_map,
                env=env,
                branch=ctx.branch,
                repo=ctx.repo_url,
                pr_number=ctx.pr_number,
                tag=ctx.tag,
                root_key="",  # already extracted above
            )

            if not flat_data:
                result.skipped.append(file_path)
                continue

            # Sort flat dict by key so key order is always canonical and
            # deterministic — prevents spurious diff/re-sync on every webhook
            # when YAML/JSON parsers return sibling keys in different orders.
            flat_data = dict(sorted(flat_data.items()))

            # ── 5b. Resolve @secret-annotated fields from Vault ───────────────
            # CUE schema fields annotated with @secret() or @secret(ref="name")
            # have their values fetched from the configured secret manager.
            # This happens AFTER transform (so remapped names are resolved) and
            # BEFORE validation (so the injected values are validated).
            #
            # When dry_run=True the resolved values are immediately masked with
            # "***" so that real secrets never appear in the dry-run report.
            _schema_path = validator._find_schema(Path(file_path).name)
            _secret_fields: dict = {}
            _logger_fields: frozenset = frozenset()
            if _schema_path is not None:
                _secret_fields = parse_secret_fields(_schema_path)
                _logger_fields = parse_logger_fields(_schema_path)
            if rule_config.get("secrets") and _secret_fields:
                flat_data = resolve_secrets(
                    flat_data, _secret_fields, rule_config["secrets"]
                )
                if rule_config.get("dry_run"):
                    flat_data = mask_secrets(flat_data, _secret_fields)

            # ── 6. Validate the final (remapped) output ───────────────────────
            validation_status = "ok"
            try:
                validator.validate(
                    flat_data=flat_data,
                    file_name=Path(file_path).name,
                    strict=strict_validate,
                )
            except CUEValidationError as ve:
                logger.warning("validation failed file=%s: %s", file_path, ve)
                validation_status = "failed" if strict_validate else "warn"
                if strict_validate:
                    result.skipped.append(file_path)
                    continue

            result.files.append(ETLFile(
                file_path=file_path,
                env=env,
                flat_data=flat_data,
                root_key=root_key or "(whole file)",
                validation=validation_status,
                logger_fields=_logger_fields,
                secret_fields=frozenset(_secret_fields.keys()),
            ))

            outcome = "ok" if validation_status == "ok" else "validation_failed"
            _record_metric(outcome, len(flat_data))

        except Exception:
            # Route unexpected exceptions to result.errors (not result.skipped)
            # so the task layer can distinguish "nothing to sync" from crashes.
            logger.exception("etl error file=%s", file_path)
            result.errors.append(file_path)
            _record_metric("error")

    for _ in result.skipped:
        _record_metric("skipped")

    return result


def run_stage_etl_content(
    ctx:       PayloadContext,
    file_path: str,
    content:   str,
    fmt:       str,
    env:       str,
) -> ETLResult:
    """
    ETL Stage 2 variant for CLI sync.

    Accepts raw file content directly instead of collecting files from git.
    Runs the same parse → extract → slice → flatten → secret-resolve →
    validate pipeline as run_stage_etl, but for a single file.
    """
    from app.pipeline import transformer
    from app.pipeline.env_slice import resolve_env_slice, detect_pattern
    from app.pipeline.validator import Validator, CUEValidationError
    from app.pipeline.secret_resolver import parse_secret_fields, resolve_secrets, mask_secrets
    from app.pipeline.change_logger import parse_logger_fields
    from app.schema_registry.manager import get_schema_manager
    from app.parsers.dispatcher import dispatch_parse

    rule_config     = ctx.rule_config
    root_key        = rule_config.get("root_key", _DEFAULT_ROOT_KEY)
    variables_map   = rule_config.get("variables_map", {})
    strict_validate = rule_config.get("strict_validation", False)

    schema_dir = get_schema_manager().tenant_schema_root("cli")
    validator  = Validator(schema_dir=schema_dir if schema_dir and schema_dir.exists() else None)
    result     = ETLResult()

    try:
        parsed = dispatch_parse(content, fmt, file_path)

        if root_key and isinstance(parsed, dict):
            if root_key in parsed:
                parsed = parsed[root_key]
            elif root_key != _DEFAULT_ROOT_KEY:
                result.skipped.append(file_path)
                return result

        if isinstance(parsed, dict):
            pattern = detect_pattern(parsed, env)
            parsed  = resolve_env_slice(parsed, env)
            if pattern != "none":
                logger.info("env_slice file=%s env=%s pattern=%s", file_path, env, pattern)

        sliced_content = (
            json.dumps(parsed) if isinstance(parsed, (dict, list)) else content
        )

        flat_data = transformer.transform(
            content=sliced_content,
            file_path=file_path,
            variables_map=variables_map,
            env=env,
            branch=ctx.branch,
            repo=ctx.repo_url,
            pr_number=ctx.pr_number,
            tag=ctx.tag,
            root_key="",
        )

        if not flat_data:
            result.skipped.append(file_path)
            return result

        flat_data = dict(sorted(flat_data.items()))

        _schema_path   = validator._find_schema(Path(file_path).name)
        _secret_fields: dict      = {}
        _logger_fields: frozenset = frozenset()
        if _schema_path is not None:
            _secret_fields = parse_secret_fields(_schema_path)
            _logger_fields = parse_logger_fields(_schema_path)

        if rule_config.get("secrets") and _secret_fields:
            flat_data = resolve_secrets(flat_data, _secret_fields, rule_config["secrets"])
            if rule_config.get("dry_run"):
                flat_data = mask_secrets(flat_data, _secret_fields)

        validation_status = "ok"
        validation_error  = ""
        if _schema_path is not None:
            try:
                validator.validate_flat(flat_data, _schema_path, strict=strict_validate)
            except CUEValidationError as exc:
                validation_status = "failed" if strict_validate else "warn"
                validation_error  = str(exc)
                if strict_validate:
                    result.skipped.append(file_path)
                    return result

        result.files.append(ETLFile(
            file_path=file_path,
            env=env,
            flat_data=flat_data,
            root_key=root_key,
            validation=validation_status,
            error=validation_error,
            logger_fields=_logger_fields,
            secret_fields=frozenset(_secret_fields.keys()),
        ))

    except Exception:
        logger.exception("etl_content error file=%s", file_path)
        result.errors.append(file_path)

    return result
