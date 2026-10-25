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

Secret manager config (effective_secrets)
─────────────────────────────────────────
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

Where the secret managers come from (effective_secrets)
───────────────────────────────────────────────────────
config.cue's secret_managers, by tag ("other-vault"), by full name
("vault-other-vault") and as "default": the rule's secret_manager, else the
one tagged "default", else the only one. Only config.cue declares them; a
rule's own "secrets" block was removed (renamed_options.REMOVED_OPTIONS).

Where the secret is (per field)
───────────────────────────────
A value written as "@secret(mount/path#key)" in the config file names the
secret: path (the manager's mount point may lead it) and key. Any other
value is looked up at the manager's path_prefix under the field's name.
"""
from __future__ import annotations

import logging
import re
from typing import Any

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
        effective_secrets() — named secret manager configs.

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
            logger.warning("secret_resolver: no secret manager %r — skipping field", sm_name)
            continue

        path, key = _secret_location(result[field], field, sm_config)
        try:
            value = _fetch_from_sm(sm_config, path, key)
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

_VALUE_REF_RE = re.compile(r"^@secret\((?P<path>[^#()]+)#(?P<key>[^#()]+)\)$")


def _secret_location(value: Any, field: str, sm_config: dict) -> tuple[str, str]:
    """(path, key) of a field's secret: from an "@secret(mount/path#key)"
    value, else the manager's path_prefix and the field's name."""
    m = _VALUE_REF_RE.match(str(value).strip()) if isinstance(value, str) else None
    if not m:
        return sm_config.get("path_prefix", ""), field
    path = m.group("path").strip().strip("/")
    mount = str(sm_config.get("mount_point") or "secret").strip("/")
    if path.startswith(mount + "/"):
        path = path[len(mount) + 1:]
    return path, m.group("key").strip()


def effective_secrets(rule_config: dict, bundle: dict | None = None) -> dict:
    """The secret managers @secret() fields of this rule are read from:
    config.cue's secret_managers, "default" being the rule's secret_manager
    when it names one."""
    from app.pipeline.datasource_settings import _bundle

    if bundle is None:
        bundle = _bundle()
    bundle = bundle or {}
    from_cue = secret_managers_from_bundle(bundle)
    chosen = str(rule_config.get("secret_manager") or "")
    if chosen:
        if chosen in from_cue:
            from_cue["default"] = from_cue[chosen]
        else:
            logger.warning("secret_resolver: the rule's secret_manager %r is not in the bundle's secret_managers", chosen)
    return from_cue


def secret_managers_from_bundle(bundle: dict) -> dict:
    """config.cue's vault secret managers in this module's config form, keyed
    by tag, by full name ("vault", "vault-{tag}") and as "default"."""
    from app.ui.sessions import _seconds, _vault_auth

    out: dict[str, dict] = {}
    entries: list[tuple[str, str, dict]] = []
    for wrapper in bundle.get("secret_managers") or []:
        for kind, cfg in (wrapper or {}).items():
            if kind != "vault" or not isinstance(cfg, dict):
                continue
            tag = str(cfg.get("tag") or "")
            name = f"vault-{tag}" if tag else "vault"
            try:
                auth = _vault_auth(cfg, name)
            except Exception as exc:
                logger.warning("secret_resolver: secret manager %s left out: %s", name, exc)
                continue
            entry = {
                "type": "vault",
                "vault_addr": cfg.get("endpoint", ""),
                "mount_point": cfg.get("mount_point") or "secret",
                "path_prefix": cfg.get("path_prefix") or "",
                "kv_version": int(cfg.get("kv_version") or 2),
                "namespace": cfg.get("namespace") or None,
                "auth": auth,
                "verify_ssl": cfg.get("verify_ssl", True) is not False,
                "ssl_ca": cfg.get("ssl_ca") or None,
                "timeout": _seconds(cfg.get("timeout"), 10),
            }
            entries.append((tag, name, entry))
            out[name] = entry
            if tag:
                out[tag] = entry
    if "default" not in out and len(entries) == 1:
        out["default"] = entries[0][2]
    return out


def _fetch_from_sm(sm_config: dict, path: str, key: str) -> str:
    """
    Dispatch to the appropriate secret manager backend.

    Currently only "vault" type is supported.
    """
    sm_type = sm_config.get("type", "vault").lower()

    if sm_type == "vault":
        return _fetch_from_vault(sm_config, path, key)

    raise ValueError(
        f"secret_resolver: unsupported secret manager type {sm_type!r}. "
        "Supported: vault"
    )


def _fetch_from_vault(sm_config: dict, path: str, key: str) -> str:
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

    return client.get_secret(path, key=key)
