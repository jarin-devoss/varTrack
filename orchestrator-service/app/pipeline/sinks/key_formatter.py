"""
app/pipeline/sinks/key_formatter.py
─────────────────────────────────────
Sink-specific key format conversion.

The flattener always produces dot-separated keys: "db.host.port".
Each sink has its own native key format.  NOT every format is valid for
every sink — the allowed combinations are enforced here and in rule.proto
via the SinkKeyFormat message.

  Sink           Allowed formats            Default
  ─────────────  ─────────────────────────  ──────────
  mongo          DOT only                   DOT
  s3             DOT only                   DOT
  vercel         DOT only                   DOT
  helm           DOT only                   DOT
  redis          DOT (HASH), FLAT (STRING)  DOT
  zookeeper      SLASH, SLASH_DOT           SLASH
  configmap      FLAT, DOT                  FLAT
  linux_server   DOT, FLAT                  DOT

MongoDB note
────────────
Mongo is a document store — keys are stored as plain JSON fields, NOT as
path strings.  DOT format is the natural representation:

  Document strategy:  { "key": "db.host", "value": "localhost" }
  File strategy:      { "data": { "db.host": "localhost", "db.port": "5432" } }

Slash or flat keys would be wrong for Mongo and are rejected at construction.
"""
from __future__ import annotations
from enum import Enum


class KeyFormat(Enum):
    DOT       = "dot"        # "db.host.port"    Mongo, S3, Vercel, Helm, Redis HASH
    SLASH     = "slash"      # "/db/host/port"   ZooKeeper standard
    SLASH_DOT = "slash_dot"  # ".db.host.port"   ZooKeeper alt
    FLAT      = "flat"       # "db_host_port"    Redis STRING, ConfigMap safe


# Formats allowed per sink kind.  Enforced at construction time.
SINK_ALLOWED_FORMATS: dict[str, list[KeyFormat]] = {
    "mongo":        [KeyFormat.DOT],
    "s3":           [KeyFormat.DOT],
    "vercel":       [KeyFormat.DOT],
    "helm":         [KeyFormat.DOT],
    "redis":        [KeyFormat.DOT, KeyFormat.FLAT],
    "zookeeper":    [KeyFormat.SLASH, KeyFormat.SLASH_DOT],
    "configmap":    [KeyFormat.FLAT, KeyFormat.DOT],
    "linux_server": [KeyFormat.DOT, KeyFormat.FLAT],
}

SINK_DEFAULT_FORMAT: dict[str, KeyFormat] = {
    "mongo":        KeyFormat.DOT,
    "s3":           KeyFormat.DOT,
    "vercel":       KeyFormat.DOT,
    "helm":         KeyFormat.DOT,
    "redis":        KeyFormat.DOT,
    "zookeeper":    KeyFormat.SLASH,
    "configmap":    KeyFormat.FLAT,
    "linux_server": KeyFormat.DOT,
}


def validate_sink_format(sink_kind: str, fmt: KeyFormat) -> None:
    """Raise ValueError if fmt is not allowed for sink_kind."""
    allowed = SINK_ALLOWED_FORMATS.get(sink_kind)
    if allowed is None:
        return  # unknown sink — skip
    if fmt not in allowed:
        raise ValueError(
            f"Sink {sink_kind!r} does not support key format {fmt.value!r}. "
            f"Allowed: {[f.value for f in allowed]}"
        )


def format_keys(flat_data: dict[str, str], fmt: KeyFormat) -> dict[str, str]:
    """
    Re-key a flat dot-separated dict into the sink's native format.

    Parameters
    ----------
    flat_data : dict[str, str]
        Dot-separated keys from the flattener.
    fmt : KeyFormat
        Target format for this sink.

    Examples
    --------
    >>> format_keys({"db.host": "localhost"}, KeyFormat.SLASH)
    {"/db/host": "localhost"}

    >>> format_keys({"db.host": "localhost"}, KeyFormat.FLAT)
    {"db_host": "localhost"}
    """
    if fmt == KeyFormat.DOT:
        return flat_data  # already correct, no copy needed

    if fmt == KeyFormat.SLASH:
        # "db.host.port" → "/db/host/port"
        return {"/" + k.replace(".", "/"): v for k, v in flat_data.items()}

    if fmt == KeyFormat.SLASH_DOT:
        # "db.host.port" → ".db.host.port"
        return {"." + k: v for k, v in flat_data.items()}

    if fmt == KeyFormat.FLAT:
        # "db.host.port" → "db_host_port"
        return {k.replace(".", "_"): v for k, v in flat_data.items()}

    return flat_data  # fallback


def resolve_destination(template: str, *, tenant: str, env: str) -> str:
    """
    Resolve {tenant} and {env} placeholders in a destination_template string.

    Used by all sinks to derive their storage path/key/name from the
    rule-level destination_template field.

    Examples
    --------
    >>> resolve_destination("/{tenant}/{env}", tenant="acme", env="pr-42")
    '/acme/pr-42'

    >>> resolve_destination("{tenant}:{env}", tenant="acme", env="main")
    'acme:main'
    """
    return template.format(tenant=tenant, env=env)


def key_format_from_str(name: str) -> KeyFormat:
    """Parse rule_config string → KeyFormat.  Case-insensitive."""
    mapping = {
        "dot":       KeyFormat.DOT,
        "slash":     KeyFormat.SLASH,
        "slash_dot": KeyFormat.SLASH_DOT,
        "flat":      KeyFormat.FLAT,
    }
    result = mapping.get(name.lower().strip())
    if result is None:
        raise ValueError(
            f"Unknown key format {name!r}. Available: {list(mapping)}"
        )
    return result
