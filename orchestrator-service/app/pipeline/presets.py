"""
app/pipeline/presets.py
───────────────────────
Presets: shared rule settings in JSON files that a tenant's bundle.json, its
rules, and a repository's vartrack.json name in "extends".

  "extends": ["local>vartrack-demo/presets:production"]

A preset is "[{source}>]{org}/{repo}[:{name} | //{dir}/{name}][#{ref}]",
read like this:

  {source}>    a platform of config.cue ("gitea", "github-cloud"); "local>",
               or no prefix, is the Git server of the file naming the preset
               (inside a preset, that preset's own server)
  :{name}      the file {name}.json at the repository's root
  //{dir}/{name}  the file {dir}/{name}.json
  (neither)    default.json
  #{ref}       a branch or tag (default: main)

A preset file is a JSON object of rule settings. It may extend presets
itself, and its "description" is dropped. The presets are merged left to
right, the settings beside "extends" go on top, and a preset already in the
chain is skipped.
A preset that cannot be read, or is not a JSON object, raises PresetError.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PRESET = re.compile(
    r"^(?:(?P<source>[\w.-]+)>)?"
    r"(?P<repo>[\w.-]+/[\w.-]+)"
    r"(?:(?::(?P<name>[\w.-]+))|(?://(?P<path>[\w./-]+)))?"
    r"(?:#(?P<ref>[\w./-]+))?$"
)


class PresetError(Exception):
    """A preset that cannot be resolved."""


@dataclass(frozen=True)
class Preset:
    source: str   # "" or "local": the naming file's Git server
    repo: str
    file: str
    ref: str


@dataclass(frozen=True)
class Origin:
    """The Git server of the file that names presets: what "local>" means."""
    base_url: str               # "http://gitea:3000"
    token: Optional[str] = None

    @classmethod
    def of(cls, repo_url: str, token: Optional[str] = None) -> "Origin":
        u = urlparse(repo_url)
        return cls(f"{u.scheme}://{u.netloc.rsplit('@', 1)[-1]}", token)


def parse_preset(text: str) -> Preset:
    m = _PRESET.match(text.strip())
    if not m:
        raise PresetError(f"preset {text!r}: expected [platform>]org/repo[:name | //dir/name][#ref]")
    if m["path"]:
        file = m["path"].strip("/")
    else:
        file = m["name"] or "default"
    if not file.endswith(".json"):
        file += ".json"
    return Preset(source=m["source"] or "", repo=m["repo"], file=file, ref=m["ref"] or "main")


# (repo_url, ref, path, token) → content, or None when there is no such file.
Fetch = Callable[[str, str, str, Optional[str]], Optional[str]]


def _git_fetch(repo_url: str, ref: str, path: str, token: Optional[str]) -> Optional[str]:
    from app.pipeline.git_extractor import GitExtractor

    full_ref = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
    content = GitExtractor().extract_file(repo_url, full_ref, path, token=token)
    if content is None and not ref.startswith("refs/"):
        content = GitExtractor().extract_file(repo_url, f"refs/tags/{ref}", path, token=token)
    return content


def _platform(name: str) -> tuple[str, Optional[str]]:
    """(endpoint, token) of config.cue's platform *name* ("{kind}" or "{kind}-{tag}")."""
    from app.pipeline.datasource_settings import _bundle, _Secrets

    bundle = _bundle()
    for wrapper in bundle.get("platforms") or []:
        for kind, cfg in (wrapper or {}).items():
            if not isinstance(cfg, dict):
                continue
            if (f"{kind}-{cfg['tag']}" if cfg.get("tag") else kind) != name:
                continue
            endpoint = str(cfg.get("endpoint") or ("https://github.com" if kind == "github" else ""))
            token = _Secrets(bundle, f"platform {name}")(cfg.get("token"), "token") if cfg.get("token") else None
            return endpoint.rstrip("/"), token
    raise PresetError(f"no platform {name!r} in config.cue")


class Resolver:
    """Resolves the "extends" of settings named from *origin*'s Git server."""

    def __init__(self, origin: Origin, fetch: Optional[Fetch] = None) -> None:
        self._origin = origin
        self._fetch = fetch or _git_fetch
        self._cache: dict[tuple[str, str, str], dict] = {}

    def resolve(self, config: dict, chain: tuple[str, ...] = (), origin: Optional[Origin] = None) -> dict:
        """*config* with its presets merged under it and "extends" removed.
        *origin* is where "local>" in *config* points (default: the resolver's)."""
        from app.pipeline.rule_layers import merge_child

        origin = origin or self._origin

        extends = config.get("extends") or []
        if isinstance(extends, str):
            extends = [extends]
        if not isinstance(extends, list):
            raise PresetError(f"extends must be a list of presets, not {type(extends).__name__}")
        merged: dict = {}
        for name in extends:
            if name in chain:
                logger.debug("preset %s already in %s — skipped", name, " → ".join(chain))
                continue
            preset, preset_origin = self._load(str(name), origin)
            merged = merge_child(merged, self.resolve(preset, chain + (str(name),), preset_origin))
        own = {k: v for k, v in config.items() if k != "extends"}
        return merge_child(merged, own)

    def _load(self, name: str, origin: Origin) -> tuple[dict, Origin]:
        preset = parse_preset(name)
        if preset.source in ("", "local"):
            base, token = origin.base_url, origin.token
        else:
            base, token = _platform(preset.source)
        url = f"{base}/{preset.repo}.git"
        key = (url, preset.ref, preset.file)
        if key not in self._cache:
            content = self._fetch(url, preset.ref, preset.file, token)
            if content is None:
                raise PresetError(f"preset {name}: no {preset.file} at {preset.ref} in {preset.repo}")
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                raise PresetError(f"preset {name}: {preset.file} is not JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise PresetError(f"preset {name}: {preset.file} must hold a JSON object")
            data.pop("description", None)
            self._cache[key] = data
        return dict(self._cache[key]), Origin(base, token)


def resolve_bundle_rules(data: Any, origin: Optional[Origin], source: str,
                         fetch: Optional[Fetch] = None) -> list[dict]:
    """The rules of a bundle.json (a list, or {"extends": [...], "rules": [...]})
    with their presets resolved: the file's own "extends" first, then each
    rule's. A preset that cannot be resolved raises PresetError for the whole
    file, so no rule of the tenant syncs without the settings it asked for."""
    if isinstance(data, list):
        rules, top = data, []
    elif isinstance(data, dict):
        rules, top = data.get("rules") or [], data.get("extends") or []
    else:
        raise PresetError(f"{source}: expected a list of rules or an object with \"rules\"")
    out: list[dict] = []
    resolver = Resolver(origin, fetch) if origin is not None else None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        wanted = (top if isinstance(top, list) else [top]) + _as_list(rule.get("extends"))
        if not wanted:
            out.append(rule)
            continue
        where = f"the {rule.get('platform')}/{rule.get('datasource')} rule"
        if resolver is None:
            raise PresetError(f"{source}: {where} extends presets, but where they live is unknown")
        try:
            out.append(resolver.resolve({**rule, "extends": wanted}))
        except PresetError as exc:
            raise PresetError(f"{where}: {exc}") from exc
    return out


def _as_list(value: Any) -> list:
    if not value:
        return []
    return value if isinstance(value, list) else [value]
