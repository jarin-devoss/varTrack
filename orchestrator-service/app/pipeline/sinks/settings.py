"""
app/pipeline/sinks/settings.py
──────────────────────────────
What a sink is built from: its datasource's typed settings, as config.cue declares them.

A datasource block of config.cue is a proto message (MongoConfig, RedisConfig, …). The sink is handed that
message, parsed with the orchestrator's own protos, instead of a flat dict whose keys a mapping table
renamed: a field the proto declares reaches the sink under its own name and type, a field it does not
declare cannot be read, and a field no sink reads says why on the field itself (unused_reason;
tests/unit/test_config_contract.py checks both ways). The rule's own options (destination_template,
key_format, …) stay in the rule; the two do not overlap.

``get(field, default)`` is the field's typed value when config.cue sets it, else *default*: "set" means
written in config.cue, so an explicit ``false`` is not mistaken for "unset". ``secret(field)`` resolves a
credential (a string, {file:} or {ref:}) and raises SecretUnavailable when it cannot be read; ``seconds``
reads a Duration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from google.protobuf import json_format
from google.protobuf.message import Message

_SECRET_REF = "vartrack.v1.SecretRef"


def _message_class(kind: str) -> type[Message]:
    from vartrack.v1.models import datasource_pb2

    descriptor = datasource_pb2.DataSource.DESCRIPTOR.fields_by_name.get(kind)
    if descriptor is None or descriptor.message_type is None:
        raise ValueError(f"no datasource kind {kind!r} (known: "
                         f"{', '.join(f.name for f in datasource_pb2.DataSource.DESCRIPTOR.fields)})")
    return type(getattr(datasource_pb2.DataSource(), kind))


@dataclass(frozen=True)
class Settings:
    kind: str                       # "mongo", "redis", …
    name: str                       # "mongo-primary": the datasource's full name
    message: Message                # the typed block (MongoConfig, …)
    present: frozenset[str] = frozenset()
    _raw: dict = field(default_factory=dict, repr=False)
    _secret: Optional[Callable[[Any, str], Optional[str]]] = field(default=None, repr=False)

    def has(self, name: str) -> bool:
        """Whether config.cue sets *name*."""
        self._field(name)
        return name in self.present

    def get(self, name: str, default: Any = None) -> Any:
        """The typed value of *name* when config.cue sets it, else *default*. A repeated field is a list,
        an enum its name ("STRATEGY_DOCUMENT"), with *_UNSPECIFIED counting as unset."""
        descriptor = self._field(name)
        if name not in self.present:
            return default
        value = getattr(self.message, name)
        if _repeated(descriptor):
            return list(value)
        if descriptor.enum_type is not None:
            enum_name = descriptor.enum_type.values_by_number[value].name
            return default if enum_name.endswith("_UNSPECIFIED") else enum_name
        return value

    def seconds(self, name: str, default: Optional[int] = None) -> Optional[int]:
        """A Duration field in whole seconds."""
        if not self.has(name):
            return default
        return int(getattr(self.message, name).ToTimedelta().total_seconds())

    def secret(self, name: str) -> Optional[str]:
        """The credential in *name*, resolved; None when config.cue does not set it."""
        descriptor = self._field(name)
        if descriptor.message_type is None or descriptor.message_type.full_name != _SECRET_REF:
            raise TypeError(f"{self.kind}.{name} is not a credential")
        if name not in self.present:
            return None
        resolve = self._secret or _plain_secret
        return resolve(self._raw.get(name), name)

    def raw(self, name: str) -> Any:
        """*name* as config.cue wrote it, for a field whose shape the sink inspects (ssh_key)."""
        self._field(name)
        return self._raw.get(name)

    def _field(self, name: str):
        descriptor = self.message.DESCRIPTOR.fields_by_name.get(name)
        if descriptor is None:
            raise AttributeError(f"{self.message.DESCRIPTOR.name} has no field {name!r}")
        return descriptor

    # ── building it ───────────────────────────────────────────────────────────

    @classmethod
    def parse(cls, kind: str, name: str, block: dict,
              secret: Optional[Callable[[Any, str], Optional[str]]] = None) -> "Settings":
        """The settings of a config.cue datasource block (the bundle as vartrack-bundle exports it)."""
        message = _message_class(kind)()
        json_format.ParseDict(block, message, ignore_unknown_fields=False)
        return cls(kind=kind, name=name, message=message, present=frozenset(block), _raw=dict(block), _secret=secret)

    @classmethod
    def of(cls, kind: str, name: str = "", **fields: Any) -> "Settings":
        """Settings written out in code, for tests and tools: a credential may be a plain string and a
        Duration a number of seconds, as config.cue's shorthands allow."""
        message_class = _message_class(kind)
        block: dict[str, Any] = {}
        for key, value in fields.items():
            descriptor = message_class.DESCRIPTOR.fields_by_name.get(key)
            if descriptor is None:
                raise AttributeError(f"{message_class.DESCRIPTOR.name} has no field {key!r}")
            if descriptor.message_type is not None and descriptor.message_type.full_name == _SECRET_REF \
                    and isinstance(value, str):
                value = {"value": value}
            elif descriptor.message_type is not None and descriptor.message_type.full_name == "google.protobuf.Duration" \
                    and isinstance(value, (int, float)):
                value = f"{value}s"
            block[key] = value
        return cls.parse(kind, name or kind, block)


def _repeated(descriptor) -> bool:
    is_repeated = getattr(descriptor, "is_repeated", None)  # newer protobuf; label is deprecated there
    if is_repeated is not None:
        return bool(is_repeated() if callable(is_repeated) else is_repeated)
    return descriptor.label == descriptor.LABEL_REPEATED


def _plain_secret(value: Any, field_name: str) -> Optional[str]:
    """A credential without a secret manager: a value or a file."""
    from app.pipeline.datasource_settings import _Secrets
    return _Secrets({}, "datasource")(value, field_name)


def kind_of(datasource: str) -> str:
    """The sink kind of a datasource name: "mongo-primary" → "mongo", "linux_server-web" → "linux_server"."""
    from app.pipeline.sinks import SINK_REGISTRY

    key = datasource.lower().replace("-", "_")
    return next((k for k in sorted(SINK_REGISTRY, key=len, reverse=True) if key == k or key.startswith(k + "_")), "")


def for_rule(rule_config: dict, datasource: Optional[str] = None) -> Settings:
    """The settings of the rule's datasource as config.cue declares them; empty settings (every field its
    default) for a datasource config.cue does not declare, which is what a sink without a block reads."""
    name = str(datasource or rule_config.get("datasource") or "")
    found = load(name, rule_config)
    if found is not None:
        return found
    kind = kind_of(name)
    if not kind:
        raise ValueError(f"no sink for datasource {name!r}")
    return Settings.of(kind, name)


def load(datasource: str, rule_config: Optional[dict] = None, bundle: Optional[dict] = None) -> Optional[Settings]:
    """The settings config.cue declares for *datasource* ("redis", "mongo-primary"), or None when it
    declares none by that name. Credentials resolve through the rule's secret_manager."""
    from app.pipeline.datasource_fields import warn_unused
    from app.pipeline.datasource_settings import _bundle, _Secrets, find_datasource

    bundle = _bundle() if bundle is None else bundle
    kind, block = find_datasource(bundle, datasource)
    if kind is None or block is None:
        return None
    warn_unused(kind, block, datasource)
    manager = str((rule_config or {}).get("secret_manager") or "")
    return Settings.parse(kind, datasource, block, _Secrets(bundle, datasource, manager))
