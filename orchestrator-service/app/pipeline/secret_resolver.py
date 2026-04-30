"""
app/pipeline/secret_resolver.py
─────────────────────────────────
Resolve and mask CUE @secret-annotated fields.

CUE @secret annotation syntax
──────────────────────────────
  db_password: string @secret()          → use default secret manager
  api_key:     string @secret(ref="bla") → use secret manager named "bla"

The field name in the CUE file is the flat key as it would appear in flat_data
after transformer.transform() — e.g. "db.password", "api_key".

Secret manager config (rule_config["secrets"])
──────────────────────────────────────────────
  {
    "default": {
      "type": "vault",
      "vault_addr": "https://vault.example.com",
      "auth": {"type": "token", "token": "..."},
      "mount_point": "secret",
      "path_prefix": "myapp/prod",
      "kv_version": 2
    },
    "bla": { ... }
  }

Vault lookup: vault.get_secret(path_prefix, key=field_name)
"""
from __future__ import annotations

import logging
import re
from app.utils.vault_client import VaultClient
from app.utils.secret_masker import register_secret
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Matches CUE field annotations like:
#   "db.password":  string  @secret()
#   db_password:    string  @secret(ref="bla")
#   "some.key":     _       @secret(  ref = "other"  )
# Group 1: field name (with or without quotes)
# Group 2: optional ref value
_SECRET_ANNOTATION_RE = re.compile(
    r'^\s*"?([^":]+?)"?\s*:'  # field name (quoted or bare)
    r'[^@\n]*'                # anything before the annotation (same line only)
    r'@secret\(\s*(?:ref\s*=\s*"([^"]+)")?\s*\)',
    re.MULTILINE,
)


def parse_secret_fields(schema_path: Path) -> dict[str, Optional[str]]:
    """
    Parse a CUE schema file and return all @secret-annotated field names.

    Parameters
    ----------
    schema_path:
        Absolute path to the .cue schema file.

    Returns
    -------
    dict mapping field_name → ref (None means use "default" secret manager).

    Examples
    --------
    Given a schema with:
        "db.password": string @secret()
        "api_key":     string @secret(ref="bla")

    Returns:
        {"db.password": None, "api_key": "bla"}
    """
    if not schema_path or not schema_path.exists():
        return {}

    try:
        text = schema_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("secret_resolver: cannot read schema %s: %s", schema_path, exc)
        return {}

    result: dict[str, Optional[str]] = {}
    for m in _SECRET_ANNOTATION_RE.finditer(text):
        field_name = m.group(1).strip()
        ref: Optional[str] = m.group(2)  # None if @secret(), else "bla"
        result[field_name] = ref
        logger.debug(
            "secret_resolver: found @secret field=%r ref=%r in %s",
            field_name, ref, schema_path.name,
        )

    return result


def resolve_secrets(
    flat_data: dict[str, str],
    secret_fields: dict[str, Optional[str]],
    secrets_config: dict,
) -> dict[str, str]:
    """
    Inject Vault secret values into flat_data for @secret-annotated fields.

    For each field in flat_data that matches a @secret annotation:
      1. Determine which secret manager to use (default or ref="name")
      2. Build a VaultClient from the matching sm_config
      3. Fetch the secret value: vault.get_secret(path_prefix, key=field)
      4. Replace flat_data[field] with the fetched value

    Fields present in secret_fields but absent from flat_data are skipped
    (the field may not exist for this particular file/env).

    Parameters
    ----------
    flat_data:
        Output of transformer.transform() — flat key/value dict.
    secret_fields:
        Output of parse_secret_fields() — {field: ref_or_None}.
    secrets_config:
        rule_config["secrets"] — named secret manager configs.

    Returns
    -------
    Updated flat_data with injected secret values.
    """
    if not secret_fields or not secrets_config:
        return flat_data

    result = dict(flat_data)

    for field, ref in secret_fields.items():
        if field not in result:
            continue  # field not present in this file's flat_data

        sm_name = ref if ref else "default"
        sm_config = secrets_config.get(sm_name)
        if not sm_config:
            logger.warning("secret_resolver: no secret manager config found — skipping field")
            continue

        try:
            value = _fetch_from_sm(sm_config, field)
            register_secret(value)
            result[field] = value
            logger.debug("secret_resolver: injected @secret field")
        except Exception as exc:
            logger.error("secret_resolver: failed to resolve secret field: %s", exc)
            # Keep original value on failure — don't crash the ETL run.

    return result


def mask_secrets(
    flat_data: dict[str, str],
    secret_fields: dict[str, Optional[str]],
) -> dict[str, str]:
    """
    Replace values of @secret-annotated fields with "***".

    Used for dry-run reports so that secret values are never exposed.

    Parameters
    ----------
    flat_data:
        Flat key/value dict (may already have injected values).
    secret_fields:
        Output of parse_secret_fields().

    Returns
    -------
    New dict with secret fields masked.
    """
    if not secret_fields:
        return flat_data

    result = dict(flat_data)
    for field in secret_fields:
        if field in result:
            result[field] = "***"

    return result


# ── Internal ──────────────────────────────────────────────────────────────────

def _fetch_from_sm(sm_config: dict, field: str) -> str:
    """
    Dispatch to the appropriate secret manager backend.

    Currently only "vault" type is supported.
    """
    sm_type = sm_config.get("type", "vault").lower()

    if sm_type == "vault":
        return _fetch_from_vault(sm_config, field)

    raise ValueError(
        f"secret_resolver: unsupported secret manager type {sm_type!r}. "
        "Supported: vault"
    )


def _fetch_from_vault(sm_config: dict, field: str) -> str:
    """
    Fetch a single field value from HashiCorp Vault.

    sm_config keys:
        vault_addr:   Vault server URL
        auth:         auth dict (passed to VaultClient)
        mount_point:  KV engine mount point (default "secret")
        path_prefix:  Vault path to the secret (e.g. "myapp/prod")
        kv_version:   1 or 2 (default 2)
        namespace:    Vault Enterprise namespace (optional)
    """

    client = VaultClient(
        endpoint=sm_config["vault_addr"],
        mount_point=sm_config.get("mount_point", "secret"),
        kv_version=int(sm_config.get("kv_version", 2)),
        namespace=sm_config.get("namespace"),
        auth=sm_config.get("auth"),
        verify_ssl=sm_config.get("verify_ssl", True),
        ssl_ca=sm_config.get("ssl_ca"),
        timeout=int(sm_config.get("timeout", 10)),
    )

    path_prefix = sm_config.get("path_prefix", "")
    return client.get_secret(path_prefix, key=field)
