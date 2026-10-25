"""
app/pipeline/rule_options.py
────────────────────────────
What each rule option is — its type, whether it merges, who may set it, its
former names — read from the Rule proto's (vartrack.v1.config) annotations.

Every option is defined once, in the proto: rule.proto states an option with
its type and how it behaves across the layers a rule is built from
(options.proto), and the gateway and the watcher read the same descriptor.
This module is the orchestrator's view of it, so a new option is added to
rule.proto and nowhere here:

    FIELDS            the options a config file may set (not the ones varTrack sets)
    TYPES             what JSON type each takes
    STRING_MAPS       the options that are maps of strings
    MERGEABLE         merged key by key across layers
    ADMIN_ONLY        only config.cue sets them
    REPO_OVERRIDABLE  a repository's vartrack.json may set them
    RENAMED           option names that were renamed, old → current
"""
from __future__ import annotations

from google.api import field_behavior_pb2
from google.protobuf.descriptor import FieldDescriptor

from vartrack.v1 import options_pb2
from vartrack.v1.models import datasource_pb2, rule_pb2


def _config(field: FieldDescriptor):
    return field.GetOptions().Extensions[options_pb2.config]


def _output_only(field: FieldDescriptor) -> bool:
    """Set by varTrack, never by a config file (last_applied_at)."""
    return field_behavior_pb2.OUTPUT_ONLY in field.GetOptions().Extensions[field_behavior_pb2.field_behavior]


def _is_map(field: FieldDescriptor) -> bool:
    return field.message_type is not None and field.message_type.GetOptions().map_entry


def _is_repeated(field: FieldDescriptor) -> bool:
    return field.is_repeated if hasattr(field, "is_repeated") else field.label == FieldDescriptor.LABEL_REPEATED


def _python_types(field: FieldDescriptor) -> tuple[type, ...]:
    """The JSON types a config file may give *field*."""
    if _is_map(field):
        return (dict,)
    if _is_repeated(field):
        return (list,)
    if field.message_type is not None:
        # prune takes a block, or true/false: `prune: true` is the common case.
        if field.message_type.full_name == "vartrack.v1.PruneConfig":
            return (bool, dict)
        return (dict,)  # a nested block, and google.protobuf.Struct (force)
    if field.enum_type is not None:
        return (str, int)  # JSON allows the enum's name or its number
    if field.cpp_type == FieldDescriptor.CPPTYPE_BOOL:
        return (bool,)
    if field.cpp_type == FieldDescriptor.CPPTYPE_STRING:
        return (str,)
    return (int, float)


FIELDS: dict[str, FieldDescriptor] = {
    name: field for name, field in rule_pb2.Rule.DESCRIPTOR.fields_by_name.items() if not _output_only(field)
}

TYPES: dict[str, tuple[type, ...]] = {name: _python_types(field) for name, field in FIELDS.items()}

# The options that are maps of strings: their values must be strings.
STRING_MAPS: tuple[str, ...] = tuple(
    name for name, field in FIELDS.items()
    if _is_map(field) and field.message_type.fields_by_name["value"].cpp_type == FieldDescriptor.CPPTYPE_STRING
)

# Merged key by key when two layers set them.
MERGEABLE: tuple[str, ...] = tuple(name for name, field in FIELDS.items() if _config(field).mergeable)

# Only config.cue sets them.
ADMIN_ONLY: frozenset[str] = frozenset(name for name, field in FIELDS.items() if _config(field).admin_only)

# What a repository's vartrack.json may set.
REPO_OVERRIDABLE: frozenset[str] = frozenset(name for name, field in FIELDS.items() if _config(field).repo_overridable)


def _renamed() -> dict[str, str]:
    """Old name → current name, from the rule's options and from every
    datasource's (S3's prefix_as_env was env_as_prefix)."""
    messages = [rule_pb2.Rule.DESCRIPTOR]
    messages += [f.message_type for f in datasource_pb2.DataSource.DESCRIPTOR.fields if f.message_type is not None]
    return {
        old: field.name
        for message in messages
        for field in message.fields
        for old in _config(field).old_names
    }


RENAMED: dict[str, str] = _renamed()
