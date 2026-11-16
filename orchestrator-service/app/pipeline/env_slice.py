"""
app/pipeline/env_slice.py
──────────────────────────
Environment-aware data slicing, inserted between root_key extraction
and the flattener in stage_etl.py.

A file can hold the settings of every environment. Which keys are environments is not guessed: it is what the
rule declares (rule_environments), plus the environment being synced and `default`. Two rules read the file.

At the root
───────────
The keys that are environments (an object under a key the rule names, or `default`) are environment objects. Every other
key is the base, the settings every environment shares. The result for an environment is

    deep_merge(base, default, the environment's own)

Objects are merged key by key, at every depth. A list, a scalar or null in the environment replaces what `default` has.
A root made only of objects, one of them `default` or the environment, is keyed by environment whatever its names: a
key for a branch deleted since is an environment object nobody reads, never base settings.

    {"default": {"db": {"host": "a", "port": 1}}, "production": {"db": {"host": "b"}}}
    env=production  →  {"db": {"host": "b", "port": 1}}

    {"timeout": 30, "default": {"db": "a"}, "production": {"db": "b"}}
    env=production  →  {"timeout": 30, "db": "b"}

Under the root
──────────────
Every object, nested or inside a list item, is tested: an object whose keys are all `default` or environments is an
environment map, and is replaced by the value for the environment, or for `default`.

    {"replicas": {"default": 2, "production": 6}}          env=production → {"replicas": 6}
    {"timeouts": {"default": 30, "upload": 300}}           not an environment map (`upload` is not one): written whole

An environment the file does not name
─────────────────────────────────────
With neither the environment nor `default`, a file keyed by environments has nothing for this one: NoEnvironment is raised,
and the caller skips the file or fails it (strict_validation). The other environments' settings are never written under it.
The exception: root settings that belong to no environment (the base) are the answer for an environment the file does not
name.

This function is sink-agnostic. The sink receives a clean flat-ready dict plus the resolved env string it should use as its
namespace / label.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT = "default"


class NoEnvironment(ValueError):
    """A file keyed by environments names neither this environment nor `default`. *path* is where ("" is the root)."""

    def __init__(self, path: str = "") -> None:
        super().__init__(path)
        self.path = path


class Environments:
    """The names a rule declares as environments: a set, and `pr-<number>` when the rule makes pull requests environments."""

    def __init__(self, names: Iterable[str] = (), pull_requests: bool = False) -> None:
        self.names = frozenset(names)
        self.pull_requests = pull_requests

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        if key in self.names:
            return True
        return self.pull_requests and key.startswith("pr-") and key[3:].isascii() and key[3:].isdigit()


def rule_environments(rule_config: dict, env: str = "", branches: Iterable[str] = (), tags: Iterable[str] = ()) -> Environments:
    """
    The environments *rule_config* (the rule as layered: config.cue, the tenant's bundle.json, the repository's
    vartrack.json, force) declares: the values of branch_map, the keys of file_path_map, every branch of the repository
    with branch_as_env, every tag with tag_as_env, and pr-<number> with pr_as_env.
    """
    names = set(str(v) for v in (rule_config.get("branch_map") or {}).values())
    names.update(str(k) for k in (rule_config.get("file_path_map") or {}))
    if rule_config.get("branch_as_env"):
        names.update(branches)
    if rule_config.get("tag_as_env"):
        names.update(tags)
    if env:
        names.add(env)
    return Environments(names, bool(rule_config.get("pr_as_env")))


def deep_merge(*parts: Any) -> Any:
    """Objects merge key by key, at every depth; anything else (a list, a scalar, null) replaces what came before."""
    result: Any = {}
    for part in parts:
        result = _merge(result, part)
    return result


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        merged = dict(base)
        for key, value in over.items():
            merged[key] = _merge(base[key], value) if key in base else value
        return merged
    return over


class _Names:
    """What counts as an environment while one file is read: the rule's, the environment itself, and `default`."""

    def __init__(self, env: str, environments: Any) -> None:
        self.env = env
        self.environments = environments if environments is not None else ()

    def __contains__(self, key: object) -> bool:
        return key == self.env or key == DEFAULT or key in self.environments


def _is_environment_map(node: dict, names: _Names) -> bool:
    return bool(node) and all(key in names for key in node)


def _resolve(node: Any, names: _Names, path: str) -> Any:
    """*node* with every environment map in it, itself included, replaced by the value for the environment."""
    if isinstance(node, list):
        return [_resolve(item, names, f"{path}.{i}" if path else str(i)) for i, item in enumerate(node)]
    if not isinstance(node, dict):
        return node
    if _is_environment_map(node, names):
        env = names.env
        if env in node and DEFAULT in node:
            chosen = deep_merge(node[DEFAULT], node[env])
        elif env in node:
            chosen = node[env]
        elif DEFAULT in node:
            chosen = node[DEFAULT]
        else:
            raise NoEnvironment(path)
        return _resolve(chosen, names, path)
    return _resolve_children(node, names, path)


def _resolve_children(node: dict, names: _Names, path: str) -> dict:
    """*node* itself stays an object (the root of a file, an environment's own settings); what is under it is resolved."""
    return {key: _resolve(value, names, f"{path}.{key}" if path else str(key)) for key, value in node.items()}


def _root(data: dict, names: _Names) -> tuple[dict, dict]:
    """(the environment objects of the root, its base)."""
    if all(isinstance(v, dict) for v in data.values()) and (names.env in data or DEFAULT in data):
        return dict(data), {}
    objects = {k: v for k, v in data.items() if isinstance(v, dict) and k in names}
    return objects, {k: v for k, v in data.items() if k not in objects}


def resolve_env_slice(data: Any, env: str, environments: Any = None) -> Any:
    """
    Slice env-keyed data for the given env.

    Parameters
    ----------
    data : dict
        Parsed dict after root_key extraction.
    env : str
        Resolved environment (branch name, "pr-42", "v1.2.3", mapped name …).
    environments : Environments | a container of names
        The environments the rule declares (rule_environments). Left out, only *env* and `default` are environments.

    Returns
    -------
    dict — env-resolved, flat-ready. Data with no environments in it comes back with only its nested environment maps
    resolved. Raises NoEnvironment when the file is keyed by environments and names neither *env* nor `default`.
    """
    if not isinstance(data, dict) or not env:
        return data
    names = _Names(env, environments)
    objects, base = _root(data, names)
    if not objects:
        return _resolve_children(data, names, "")
    own, shared = objects.get(env), objects.get(DEFAULT)
    if own is None and shared is None:
        if not base:
            raise NoEnvironment("")
        return _resolve_children(base, names, "")
    parts = [_resolve_children(part, names, "") for part in (base, shared, own) if isinstance(part, dict)]
    return deep_merge(*parts)


def detect_pattern(data: dict, env: str, environments: Any = None) -> str:
    """Which shape the file has, for the log: "pattern_1" (every root key an environment), "pattern_3" (environment objects next to
    settings of no environment), "pattern_2" (environment maps under the root only) or "none"."""
    if not isinstance(data, dict) or not env:
        return "none"
    names = _Names(env, environments)
    objects, base = _root(data, names)
    if objects:
        return "pattern_3" if base else "pattern_1"
    try:
        return "pattern_2" if _resolve_children(data, names, "") != data else "none"
    except NoEnvironment:
        return "pattern_2"
