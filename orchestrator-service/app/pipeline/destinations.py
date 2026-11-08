"""
app/pipeline/destinations.py
────────────────────────────
Where a sync of one environment writes, as one string.

Two things need it before a sink is opened: the allow-list a tenant's ``destinations`` declare
(a sync to a place that is not the tenant's is refused before anything is written) and the
lease that makes two writers of one place wait for each other, whatever tenant they are. Both
want the *resolved* place: a collection, a key prefix, a znode path, a file path, an object
prefix, a ConfigMap, a release, a project.

Each kind resolves it the way its sink does (the sink's own ``_resolve_path``,
``_key_prefix`` …). The two read the same rule settings, so a rule that moves a sink's
destination moves this one with it.
"""
from __future__ import annotations

import fnmatch
from typing import Any

from app.pipeline.sinks.key_formatter import resolve_destination


def _kind(datasource: str) -> str:
    from app.pipeline.sinks.settings import kind_of
    return kind_of(datasource)


def destination_of(datasource: str, rule_config: dict, tenant_id: str, env: str, settings=None) -> str:
    """The place a sync of *env* writes for the rule, or "" for a kind that has none to name: the
    datasource's typed settings (config.cue's block) and the rule's own destination_template. Nothing is
    connected to."""
    from app.pipeline.sinks.settings import for_rule

    kind = _kind(datasource)
    if not kind:
        return ""
    s = settings or for_rule(rule_config, datasource)
    template = rule_config.get("destination_template") or ""

    if kind == "redis":
        from app.pipeline.sinks.redis import RedisSink
        return RedisSink._prefix_of(template, s.get("key_prefix", ""), tenant_id, datasource, env)
    if kind == "zookeeper":
        from app.pipeline.sinks.zookeeper import ZookeeperSink
        return ZookeeperSink._root_of(s, rule_config, tenant_id, env)
    if kind == "mongo":
        safe = env.replace("/", "_").replace(".", "_").replace("$", "_").replace("\0", "_")
        if template:
            return resolve_destination(template, tenant=tenant_id, env=safe)
        base = s.get("collection") or "configs"
        return f"{base}_{safe}" if s.get("env_as_collection") else str(base)
    if kind == "linux_server":
        from app.pipeline.sinks.linux_server import LinuxServerSink, _safe_filename
        safe = _safe_filename(env)
        if template:
            return resolve_destination(template, tenant=tenant_id, env=safe)
        from pathlib import PurePosixPath
        base = PurePosixPath(LinuxServerSink.file_path_of(s) or "/etc/vartrack/config.env")
        if s.get("env_as_path", False):
            return str(base.parent / safe / f"{base.stem}{base.suffix}")
        if rule_config.get("env_as_filename", True):
            return str(base.parent / f"{base.stem}_{safe}{base.suffix}")
        return str(base)
    if kind == "s3":
        bucket = s.get("bucket", "")
        if template:
            prefix = resolve_destination(template, tenant=tenant_id, env=env).strip("/")
        else:
            prefix = f"{str(s.get('prefix', '')).rstrip('/')}/{env}".lstrip("/")
        return f"{bucket}/{prefix}"
    if kind == "configmap":
        from app.pipeline.sinks.configmap import _k8s_safe_name
        namespace, name = str(s.get("namespace", "")), str(s.get("configmap_name", ""))
        if template:
            return f"{namespace}/{_k8s_safe_name(resolve_destination(template, tenant=tenant_id, env=env))}"
        if s.get("env_as_namespace", False):
            return f"{_k8s_safe_name(env)}/{name}"
        if s.get("env_as_name_suffix", True):
            return f"{namespace}/{name}-{_k8s_safe_name(env)}"
        return f"{namespace}/{name}"
    if kind == "helm":
        release = (resolve_destination(template, tenant=tenant_id, env=env) if template
                   else str(s.get("release_name", "vartrack")))
        return f"{s.get('namespace', 'default')}/{release}"
    return ""


def matches(destination: str, patterns: list[str]) -> bool:
    """Whether *destination* is covered by one of the glob *patterns* ("*" is any run of
    characters, "/" included; case counts, as it does on the stores)."""
    return any(fnmatch.fnmatchcase(destination, p) for p in patterns)


def describe(tenant: dict[str, Any], datasource: str) -> list[str]:
    """The patterns the declared *tenant* may write on *datasource*."""
    return [p for d in tenant.get("destinations") or [] if d.get("datasource") == datasource
            for p in d.get("paths") or []]
