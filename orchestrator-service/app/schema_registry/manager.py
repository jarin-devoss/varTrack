"""
app/schema_registry/manager.py
────────────────────────────────
Per-tenant CUE schema registry.

What it does
────────────
The orchestrator git-clones a schema repository for each tenant.
That repo contains CUE schema files — one per settings file type
the tenant cares about:

    schema_repo/
        package.json.cue
        appsettings.json.cue
        appsettings.Production.json.cue
        dotenv.cue
        config.toml.cue
        default.cue
        bundle.json            ← optional rule manifest

The manager:
1. Clones each tenant's schema repo on first request.
2. Refreshes stale clones on TTL expiry or explicit invalidate()
   (e.g. triggered by a webhook push to the schema repo).
3. Exposes tenant_schema_root(tenant_id) → Path  for the Validator.
4. Parses bundle.json (if present) so workers can resolve rule_config
   locally without a round-trip to the API.

A tenant's schema repository is the operator's to register: the ``schema_repo`` of a tenant
declared in config.cue. A request never registers or changes one. (Before tenants were
declared, env vars did it; they are still read, with a warning, for one release:
    SCHEMA_TENANT_<ID>_REPO    git clone URL
    SCHEMA_TENANT_<ID>_BRANCH  branch (default: main)
    SCHEMA_TENANT_<ID>_TOKEN   optional auth token)
"""
from __future__ import annotations

import hashlib
import ipaddress as _ipaddress
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app import dependencies
from app.config import settings
from app.dependencies import SchemaUnavailable, SecretUnavailable
from app.pipeline.renamed_options import with_current_names

logger = logging.getLogger(__name__)


def _m():
    try:
        from app.monitoring import get_metrics
        return get_metrics()
    except Exception:
        return None


@dataclass
class _TenantEntry:
    tenant_id: str
    repo_url:  str
    branch:    str
    local_path: Path
    commit_sha: str = ""
    fetched_at: float = field(default_factory=time.time)
    rules: list[dict] = field(default_factory=list)   # parsed from bundle.json
    # Why bundle.json (or a preset it extends) could not be used; the tenant
    # then has no rules.
    bundle_error: str = ""
    # The clone itself failed: this entry holds nothing, and is tried again once it is stale.
    clone_failed: bool = False
    # The tenant's schema generation (see _published_generation) when this entry was fetched.
    generation: int = 0

    def is_stale(self, published: int = 0) -> bool:
        """Older than the TTL, or fetched at another generation than the tenant's schemas are at now."""
        # "Another", not "an older one": the generation is only a label for a state of the schemas. A
        # counter Redis lost (a restart, a flush) starts again from 1; the schemas may have changed in
        # between, so a clone fetched at a different number is fetched again.
        return (time.time() - self.fetched_at) > settings.SCHEMA_TTL_SECONDS or self.generation != published


# Every process (the API, each worker of the pool) keeps its own clones. invalidate() — a push to
# the schema repository, a warm-up — runs in one of them, so it also advances a counter (the tenant's
# schema generation) in Redis; every other process sees that its clone was fetched at another number
# and fetches before it is used again. It is a counter, never a time: clocks differ between
# containers and hosts and can step back, and a "newer" time that is numerically smaller is not seen.
# (a name of its own: an INCR on a key that holds anything but an integer fails)
_GENERATION_KEY = "schema_generation_counter:{tenant}"
_GENERATION_TTL = 30 * 24 * 3600


def _published_generation(tenant_id: str) -> int:
    try:
        from app.utils.redis_cache import get_cache as _get_cache
        rc = _get_cache()
        value = rc.get(_GENERATION_KEY.format(tenant=tenant_id)) if rc is not None else None
        return int(value or 0)
    except Exception:
        return 0   # no Redis: the TTL alone decides, as before


def _publish_generation(tenant_id: str) -> None:
    try:
        from app.utils.redis_cache import get_cache as _get_cache
        rc = _get_cache()
        if rc is not None and rc.incr(_GENERATION_KEY.format(tenant=tenant_id), ttl=_GENERATION_TTL) is None:
            raise RuntimeError("Redis did not take the new generation")
    except Exception:
        logger.warning("schema generation of tenant %s not published: other processes refresh on their TTL",
                       tenant_id)


class SchemaRegistryManager:
    """Thread-safe manager for per-tenant CUE schema registry clones."""

    def __init__(self) -> None:
        self._root = settings.SCHEMA_CACHE_DIR
        self._root.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, _TenantEntry] = {}
        self._tenant_configs: dict[str, dict] = {}
        self._from_bundle: set[str] = set()     # the entries config.cue's tenants put in _tenant_configs
        self._lock = threading.Lock()
        self._load_env_configs()
        self.sync_declared()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_all_rules(self, tenant_id: str = "default") -> list[dict]:
        """
        Return the rules of *tenant_id*: config.cue's rules with the tenant's
        bundle.json rules laid over them (rule_layers). Tenant "default" also
        has the config.cue rules it owns that no tenant's bundle.json names.

        Called by schema_utils.rules_from_bundle_all() which feeds sync_all_task.
        """
        from app.pipeline.rule_layers import tenant_rules

        if self.bundle_error(tenant_id):
            return []
        own: list[dict] = []
        with self._lock:
            for k, entry in self._entries.items():
                if k.startswith(f"{tenant_id}:"):
                    own = list(entry.rules)
                    break
        return tenant_rules(own, tenant_id, self.claimed_rules())

    def bundle_error(self, tenant_id: str) -> str:
        """Why the tenant's bundle.json cannot be used, or ""."""
        with self._lock:
            return next((e.bundle_error for e in self._entries.values()
                         if e.tenant_id == tenant_id and e.bundle_error), "")

    def claimed_rules(self) -> set[tuple]:
        """The (tenant, platform, datasource) of the tenants' bundle.json rules.
        A registered tenant not loaded yet is loaded first: until then its
        rules would count as tenant "default"'s."""
        self.sync_declared()
        with self._lock:
            loaded = {e.tenant_id for e in self._entries.values()}
            missing = [t for t in self._tenant_configs if t not in loaded]
        for tenant_id in missing:
            try:
                self.get_or_clone(tenant_id)
            except Exception:
                logger.warning("schema repo of tenant %s could not be loaded", tenant_id)
        with self._lock:
            return {(e.tenant_id, r.get("platform"), r.get("datasource"))
                    for e in self._entries.values() for r in e.rules}

    def sync_declared(self) -> None:
        """Bring the registered schema repositories in line with config.cue's tenants (a
        declared tenant's schema_repo wins over an env var of the same name; a tenant taken
        out of config.cue is unregistered). config.cue is re-read when it changes."""
        from app import tenants
        from app.pipeline.datasource_settings import _Secrets, _bundle

        try:
            bundle = _bundle()
            declared = tenants.declared()
        except Exception:
            logger.warning("config.cue's tenants could not be read; keeping the registered schema repositories")
            return
        wanted: dict[str, dict] = {}
        for tenant in declared:
            repo = tenant.get("schema_repo") or {}
            if not repo.get("repo"):
                continue
            secret = _Secrets(bundle, f"tenant {tenant['name']}", repo.get("secret_manager") or "")
            registration = {"repo_url": repo["repo"], "branch": repo.get("branch") or "main", "token": None}
            try:
                if "://" not in repo["repo"] and not repo["repo"].startswith("git@"):
                    # "owner/name" on a platform: the clone URL is that platform's server and the
                    # name; with no token of its own the tenant's schema repository is read with the
                    # platform's, which is the one the operator named for it.
                    from app.pipeline.presets import PresetError, _platform
                    try:
                        endpoint, platform_token = _platform(str(repo.get("platform") or ""))
                    except PresetError as exc:
                        raise SchemaUnavailable(
                            f"schema_repo {repo['repo']!r} names the platform {repo.get('platform')!r}: {exc}") from exc
                    registration["repo_url"] = f"{endpoint}/{repo['repo'].strip('/')}.git"
                    registration["token"] = platform_token
                if repo.get("token"):
                    registration["token"] = secret(repo.get("token"), "schema_repo.token")
            except (SecretUnavailable, SchemaUnavailable) as exc:
                # An anonymous clone, or a clone of "owner/name" as if it were a URL, would fail with
                # a message about the wrong thing: say what it is, and let the tenant's sync fail with that.
                registration["error"] = exc.reason
                dependencies.down(f"schemas:{tenant['name']}", exc.reason)
            wanted[tenant["name"]] = registration
        with self._lock:
            for name in getattr(self, "_from_bundle", set()) - set(wanted):
                self._tenant_configs.pop(name, None)
            self._tenant_configs.update(wanted)
            self._from_bundle = set(wanted)

    def registration(self, tenant_id: str) -> dict:
        """The schema repository registered for *tenant_id* ({} when none): repo_url, branch,
        token."""
        self.sync_declared()
        with self._lock:
            return dict(self._tenant_configs.get(tenant_id) or {})

    def list_tenant_ids(self) -> list[str]:
        """Every tenant with a registered schema repository (declared in config.cue, or from
        the deprecated env vars)."""
        self.sync_declared()
        with self._lock:
            return list(self._tenant_configs.keys())

    def tenant_for_repo(self, repo: str) -> Optional[str]:
        """The tenant whose registered schema repository is *repo* ("owner/name", or a clone
        URL), else None. A schema push belongs to that tenant and to no other: it is what a
        push says it came from, checked against what an operator registered, never the other
        way round."""
        from app.ui.ledger import repo_name

        self.sync_declared()
        wanted = repo_name(repo).lower()
        with self._lock:
            for tenant_id, cfg in self._tenant_configs.items():
                if cfg.get("repo_url") and repo_name(cfg["repo_url"]).lower() == wanted:
                    return tenant_id
        return None

    def registered_branch(self, tenant_id: str) -> str:
        """The branch of the tenant's registered schema repository."""
        return self.registration(tenant_id).get("branch", "main")

    def get_or_clone(self, tenant_id: str) -> Path:
        """
        Return local path of the tenant's schema clone, from the repository, branch and
        credential an operator registered for the tenant (SCHEMA_TENANT_<ID>_REPO, …).
        Clones on first call; re-fetches on TTL expiry. Nothing a request carries chooses
        what is cloned, or with which credential.
        """
        cfg = self.registration(tenant_id)
        if cfg.get("error"):
            raise SchemaUnavailable(f"tenant {tenant_id}: the schema repository cannot be reached: "
                                    f"{cfg['error']}", what=tenant_id)
        repo_url = cfg.get("repo_url")
        branch   = cfg.get("branch", "main")
        token    = cfg.get("token")

        if not repo_url:
            return self._root / tenant_id   # sentinel (empty dir)

        _validate_repo_url(repo_url)

        cache_key = f"{tenant_id}:{_slug(repo_url, branch)}"

        # Hold the lock only to READ cache state, release before network I/O
        # (clone/fetch), then re-acquire to WRITE the result.  This prevents
        # the global lock from being held for the full duration of a git clone.
        # Read before fetching: a change published while this fetch runs makes it stale again.
        published = _published_generation(tenant_id)
        with self._lock:
            entry = self._entries.get(cache_key)
            needs_clone   = entry is None or (entry.clone_failed and entry.is_stale(published))
            needs_refresh = (not needs_clone) and entry.is_stale(published)

        if needs_clone:
            # Clone outside the lock — network I/O should never hold a global lock.
            _t = time.perf_counter()
            new_entry = self._clone(tenant_id, repo_url, branch, token)
            new_entry.generation = published
            _elapsed = time.perf_counter() - _t
            with self._lock:
                # Double-check: another thread may have cloned while we were busy.
                existing = self._entries.get(cache_key)
                if existing is None or existing.clone_failed:
                    self._entries[cache_key] = new_entry
                    entry = new_entry
                    if new_entry.clone_failed:
                        dependencies.down(f"schemas:{tenant_id}", new_entry.bundle_error)
                    else:
                        dependencies.up(f"schemas:{tenant_id}")
                    logger.info("schema cloned tenant=%s sha=%s", tenant_id, new_entry.commit_sha)
                    m = _m()
                    if m:
                        try:
                            m.inc_schema_op("clone", "ok")
                            m.observe_schema_clone(tenant_id, _elapsed)
                        except Exception:
                            pass
                else:
                    entry = existing   # use the version the other thread stored
            if entry is not new_entry:
                shutil.rmtree(new_entry.local_path, ignore_errors=True)

        elif needs_refresh:
            # Refresh outside the lock — same reasoning as clone.
            try:
                _t = time.perf_counter()
                refreshed = self._refresh(entry, token)
                refreshed.generation = published
                _elapsed = time.perf_counter() - _t
                with self._lock:
                    self._entries[cache_key] = refreshed
                    entry = refreshed
                dependencies.up(f"schemas:{tenant_id}")
                logger.info("schema refreshed tenant=%s sha=%s", tenant_id, entry.commit_sha)
                m = _m()
                if m:
                    try:
                        m.inc_schema_op("refresh", "ok")
                        m.observe_schema_clone(tenant_id, _elapsed)
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("schema refresh failed, using stale: %s", dependencies.scrub(exc))
                dependencies.stale(f"schemas:{tenant_id}", f"refresh failed: {exc}")
                m = _m()
                if m:
                    try:
                        m.inc_schema_op("refresh", "error")
                    except Exception:
                        pass
        else:
            m = _m()
            if m:
                try:
                    m.inc_schema_op("get", "hit")
                except Exception:
                    pass

        return entry.local_path

    def invalidate(self, tenant_id: str) -> None:
        """Force-expire all clones for a tenant (called on schema webhook push)."""
        with self._lock:
            keys = [k for k in self._entries if k.startswith(f"{tenant_id}:")]
            paths_to_delete = [self._entries.pop(k).local_path for k in keys]
        for path in paths_to_delete:
            shutil.rmtree(path, ignore_errors=True)
        # The other processes' clones are older now too.
        _publish_generation(tenant_id)
        # Purge rule-config Redis cache entries for this tenant so workers
        # pick up the new bundle.json on the next resolve_rule() call.
        try:
            from app.utils.redis_cache import get_cache as _get_cache
            rc = _get_cache()
            if rc is not None:
                rc.delete_pattern(f"rule:{tenant_id}:*")
        except Exception:
            pass
        logger.info("schema cache invalidated tenant=%s", tenant_id)

    def schema_commit(self, tenant_id: str) -> str:
        """The commit of the schema repository this process holds for the tenant, "" when it holds
        none. A sync reports it, so which schemas validated it is on record."""
        with self._lock:
            for key, entry in self._entries.items():
                if key.startswith(f"{tenant_id}:") and not entry.clone_failed:
                    return entry.commit_sha
        return ""

    def current_schema_root(self, tenant_id: str) -> Path:
        """The tenant's schema dir, fetched first when this process's clone is older than the TTL or
        than a change another process published. A sync validates against this, never against
        whatever clone the process happens to hold. When the fetch fails, the clone already held is
        used (and the dependency is marked stale by get_or_clone)."""
        try:
            self.get_or_clone(tenant_id)
        except SchemaUnavailable:
            raise
        except Exception:
            logger.warning("schema repo of tenant %s could not be refreshed; validating with the clone held",
                           tenant_id)
        return self.tenant_schema_root(tenant_id)

    def tenant_schema_root(self, tenant_id: str) -> Path:
        """
        Return the most-recently cloned schema dir for a tenant.
        Does NOT trigger a new clone.

        Falls back to scanning the shared volume on disk so that Celery workers
        (separate processes with their own in-memory registry) can locate schemas
        cloned by the API process without a warm-up round-trip.
        """
        with self._lock:
            for k, entry in self._entries.items():
                if k.startswith(f"{tenant_id}:"):
                    return entry.local_path
        # Disk fallback: the API may have cloned the schema to the shared volume.
        # Return the most-recently modified subdirectory so workers don't need
        # their own in-memory warm-up state.
        tenant_dir = self._root / tenant_id
        if tenant_dir.is_dir():
            try:
                subdirs = sorted(
                    (p for p in tenant_dir.iterdir() if p.is_dir()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if subdirs:
                    return subdirs[0]
            except OSError:
                pass
        return tenant_dir   # fallback sentinel

    def resolve_rule(
        self,
        platform: str,
        datasource: str,
        tenant_id: str = "default",
    ) -> Optional[dict]:
        """
        Resolve rule_config: config.cue's rule with the tenant's bundle.json
        rule laid over it (rule_layers).

        The bundle.json rule goes through a Redis read-through cache
        (TTL=300s) keyed by ``rule:<tenant_id>:<platform>:<datasource>`` so
        Celery workers on different processes avoid repeatedly deserialising
        bundle.json. Returns None if no match is found.
        """
        from app.pipeline.rule_layers import rule_for
        from app.utils.redis_cache import get_cache as _get_cache
        reason = self.bundle_error(tenant_id)
        if reason:
            # The tenant's schema repository could not be read or its bundle.json cannot be used:
            # "no such rule" would be false, and its files must not be written as if it had no schemas.
            raise SchemaUnavailable(f"tenant {tenant_id}: {reason}", what=tenant_id)
        _rc = _get_cache()
        _cache_key = f"rule:{tenant_id}:{platform}:{datasource}"
        if _rc is not None:
            cached = _rc.get(_cache_key)
            if cached is not None:
                # may be the sentinel {} meaning "no match"; cached before an upgrade, it may use old names
                return rule_for(platform, datasource, with_current_names(cached) or None, tenant_id,
                                self.claimed_rules())

        found: Optional[dict] = None
        with self._lock:
            for k, entry in self._entries.items():
                if k.startswith(f"{tenant_id}:"):
                    for rule in entry.rules:
                        if rule.get("platform") == platform and rule.get("datasource") == datasource:
                            found = rule
                            break
                if found is not None:
                    break
        if found is not None and _rc is not None:
            _rc.set(_cache_key, found, ttl=300)
        return rule_for(platform, datasource, found, tenant_id, self.claimed_rules())

    # ── Internal ──────────────────────────────────────────────────────────────

    def _clone(
        self,
        tenant_id: str,
        repo_url: str,
        branch: str,
        token: Optional[str],
    ) -> _TenantEntry:
        try:
            import git
        except ImportError:
            logger.error("GitPython not installed – cannot clone schema repo")
            return self._empty_entry(tenant_id, repo_url, branch)

        # Each clone gets a directory of its own: the worker processes share
        # this volume, and two clones into one directory break each other.
        tenant_dir = self._root / tenant_id
        tenant_dir.mkdir(parents=True, exist_ok=True)
        local_dir = Path(tempfile.mkdtemp(prefix=_slug(repo_url, branch) + "-", dir=tenant_dir))

        clone_url = _inject_token(repo_url, token)
        # Disable TTY prompts — workers run headless and a prompt would hang
        # the clone indefinitely if credentials are missing or wrong.
        _git_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": (
                "ssh -o BatchMode=yes "
                "-o StrictHostKeyChecking=accept-new "
                "-o ConnectTimeout=10"
            ),
        }
        try:
            repo = git.Repo.clone_from(
                clone_url, str(local_dir),
                depth=1, single_branch=True, branch=branch,
                env=_git_env,
            )
            sha = repo.head.commit.hexsha
            repo.close()   # GitPython keeps a `git cat-file` process per Repo until it is closed
        except Exception as exc:
            reason = dependencies.scrub(exc)
            logger.error("schema clone failed: %s", reason)
            return self._empty_entry(tenant_id, repo_url, branch, local_dir,
                                     bundle_error=f"the schema repository {repo_url} could not be cloned: {reason}")

        rules, bundle_error = _load_bundle_checked(local_dir, repo_url, token, tenant_id)
        _log_missing_schemas(tenant_id, local_dir, rules, sha)
        return _TenantEntry(
            tenant_id=tenant_id,
            repo_url=repo_url,
            branch=branch,
            local_path=local_dir,
            commit_sha=sha,
            rules=rules,
            bundle_error=bundle_error,
        )

    def _refresh(self, entry: _TenantEntry, token: Optional[str]) -> _TenantEntry:
        # Let the original exception propagate so callers can distinguish
        # transient network errors (retry) from structural errors like a
        # corrupt clone (re-clone).
        import git
        _git_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": (
                "ssh -o BatchMode=yes "
                "-o StrictHostKeyChecking=accept-new "
                "-o ConnectTimeout=10"
            ),
        }
        repo = git.Repo(str(entry.local_path))
        # The clone is a shallow cache of the remote branch: take what the remote has now, never merge.
        # A shallow `pull` cannot reconcile a branch it holds one commit of with one that moved on
        # ("Need to specify how to reconcile divergent branches"), and every refresh after the first
        # push failed, so the tenant kept validating against, and reading @secret fields from, the
        # schemas it had when it was first cloned.
        try:
            with repo.git.custom_environment(**_git_env):
                repo.git.fetch("--depth=1", "origin", entry.branch)
                repo.git.reset("--hard", "FETCH_HEAD")
            sha = repo.head.commit.hexsha
        finally:
            repo.close()   # GitPython keeps a `git cat-file` process per Repo until it is closed

        rules, bundle_error = _load_bundle_checked(entry.local_path, entry.repo_url, token, entry.tenant_id)
        _log_missing_schemas(entry.tenant_id, entry.local_path, rules, sha)
        return _TenantEntry(
            tenant_id=entry.tenant_id,
            repo_url=entry.repo_url,
            branch=entry.branch,
            local_path=entry.local_path,
            commit_sha=sha,
            rules=rules,
            bundle_error=bundle_error,
        )

    def _empty_entry(
        self,
        tenant_id: str,
        repo_url: str,
        branch: str,
        local_path: Optional[Path] = None,
        bundle_error: str = "",
    ) -> _TenantEntry:
        lp = local_path or (self._root / tenant_id)
        lp.mkdir(parents=True, exist_ok=True)
        return _TenantEntry(
            tenant_id=tenant_id,
            repo_url=repo_url,
            branch=branch,
            local_path=lp,
            bundle_error=bundle_error,
            clone_failed=bool(bundle_error),
        )

    def _load_env_configs(self) -> None:
        """
        Register tenants from env vars (deprecated, see the module docstring):
            SCHEMA_TENANT_ACME_REPO=https://...
            SCHEMA_TENANT_ACME_BRANCH=main
            SCHEMA_TENANT_ACME_TOKEN=ghp_xxx
        """
        prefix = "SCHEMA_TENANT_"
        seen: dict[str, dict] = {}
        for key, val in os.environ.items():
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]                  # "ACME_REPO"
            parts = rest.split("_", 1)
            if len(parts) != 2:
                continue
            tenant_id, field_name = parts[0].lower(), parts[1].lower()
            seen.setdefault(tenant_id, {})[field_name] = val

        for tenant_id, cfg in seen.items():
            if "repo" in cfg:
                logger.warning(
                    "SCHEMA_TENANT_%s_* is deprecated: declare the tenant's schema_repo in config.cue "
                    "(tenants: [{name: %r, schema_repo: {...}}]); the env vars are read for this release only",
                    tenant_id.upper(), tenant_id)
                self._tenant_configs[tenant_id] = {
                    "repo_url": cfg["repo"],
                    "branch":   cfg.get("branch", "main"),
                    "token":    cfg.get("token"),
                }
                logger.debug("auto-registered schema tenant=%s from env", tenant_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

_ALLOWED_REPO_SCHEMES = {"https", "http", "ssh", "git"}

# Blocked IP ranges: loopback, link-local (cloud IMDS), private RFC-1918, IPv6 ULA.
# These are checked AFTER DNS resolution to defeat DNS-rebinding attacks where
# an attacker registers e.g. metadata.attacker.com → 169.254.169.254.
_BLOCKED_NETWORKS = [
    _ipaddress.ip_network("127.0.0.0/8"),      # loopback
    _ipaddress.ip_network("169.254.0.0/16"),    # link-local / AWS+GCP+Azure IMDS
    _ipaddress.ip_network("10.0.0.0/8"),        # RFC-1918
    _ipaddress.ip_network("172.16.0.0/12"),     # RFC-1918
    _ipaddress.ip_network("192.168.0.0/16"),    # RFC-1918
    _ipaddress.ip_network("::1/128"),           # IPv6 loopback
    _ipaddress.ip_network("fc00::/7"),          # IPv6 ULA
    _ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def _validate_repo_url(repo_url: str) -> None:
    """
    Reject repo URLs that could trigger SSRF or local file reads.

    Two-stage check:
    1. Scheme allowlist — rejects file://, ftp://, etc.
    2. DNS-resolved IP blocklist — defeats DNS-rebinding attacks where a
       hostname resolves to a cloud metadata IP (169.254.169.254) or a
       private network address.  String-matching the hostname alone is
       insufficient because an attacker can register metadata.evil.com and
       point its A record at 169.254.169.254.

    The GIT_ALLOWED_HOSTS env var (comma-separated hostnames) bypasses the
    IP blocklist for explicitly trusted internal hosts (e.g. e2e/dev stacks).
    """
    import os
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(repo_url)
    if parsed.scheme not in _ALLOWED_REPO_SCHEMES:
        raise ValueError(
            f"repo_url scheme {parsed.scheme!r} not allowed "
            f"(allowed: {sorted(_ALLOWED_REPO_SCHEMES)})"
        )

    hostname = parsed.hostname or ""
    if not hostname:
        return  # bare ssh:// git@ style URLs have no hostname component to check

    # Explicitly trusted hostnames bypass the IP blocklist.
    _allowed_hosts = {
        h.strip().lower()
        for h in os.environ.get("GIT_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }
    if hostname.lower() in _allowed_hosts:
        return

    # Resolve all addresses the hostname maps to and reject any that land in
    # a blocked network.  Uses getaddrinfo so IPv6 AAAA records are covered.
    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Unresolvable host — git will fail anyway; let it surface naturally.
        return

    for _family, _type, _proto, _canonname, sockaddr in results:
        ip_str = sockaddr[0]
        try:
            addr = _ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        for network in _BLOCKED_NETWORKS:
            if addr in network:
                raise ValueError(
                    f"repo_url {repo_url!r} resolves to blocked address "
                    f"{ip_str} (network {network})"
                )


def _slug(repo_url: str, branch: str) -> str:
    h = hashlib.sha256(f"{repo_url}|{branch}".encode()).hexdigest()[:8]
    name = repo_url.rstrip("/").rsplit("/", 1)[-1].replace(".git", "")
    return f"{name}-{h}"


def _inject_token(repo_url: str, token: Optional[str]) -> str:
    if not token or not repo_url.startswith("https://"):
        return repo_url
    # Avoid f-string so the token doesn't appear in tracebacks.
    # The returned URL must never be logged — log repo_url instead.
    bare = repo_url[len("https://"):]
    return "https://oauth2:" + token + "@" + bare


_missing_logged: set[str] = set()


def _log_missing_schemas(tenant_id: str, local_dir: Path, bundle_rules: list[dict], commit: str) -> None:
    """Say, once per schema and commit, that a schema_map entry of this tenant names a schema the schema repository
    does not have. The sync of a file it covers fails (app.pipeline.validator.Validator.choose); this says so before
    the first push does. The entries are the tenant's bundle.json rules' and config.cue's rules it may layer."""
    from app.pipeline import rule_layers
    from app.pipeline.schema_paths import NONE, join_inside, schema_problem

    named: list[tuple[str, str, str]] = []
    for label, rules in (("bundle.json", bundle_rules),
                         ("config.cue", [r for r in rule_layers.cue_rules() if rule_layers._allowed(r, tenant_id)])):
        for rule in rules:
            for i, entry in enumerate(rule.get("schema_map") or []):
                if isinstance(entry, dict) and isinstance(entry.get("schema"), str):
                    named.append((f"{label} rule {rule.get('platform')}/{rule.get('datasource')} schema_map[{i}]",
                                  entry["schema"], str(entry.get("match", ""))))
    for where, schema, match in named:
        if schema == NONE or schema_problem(schema):
            continue
        try:
            present = join_inside(local_dir, schema).is_file()
        except ValueError:
            present = False
        key = f"{tenant_id}|{commit}|{where}|{schema}"
        if not present and key not in _missing_logged:
            _missing_logged.add(key)
            logger.warning("tenant %s: %s sends %s to schema %s, which is not in its schema repository at %s — "
                           "those files will fail their sync", tenant_id, where, match, schema, (commit or "?")[:12])


def _load_bundle(local_dir: Path, repo_url: str = "", token: Optional[str] = None) -> list[dict]:
    """The rules of bundle.json (or rules.json); none when it cannot be used."""
    return _load_bundle_checked(local_dir, repo_url, token)[0]


def _load_bundle_checked(local_dir: Path, repo_url: str = "", token: Optional[str] = None,
                         tenant_id: str = "") -> tuple[list[dict], str]:
    """(rules, error): bundle.json (or rules.json) parsed for worker-side rule
    resolution, with the presets its "extends" name resolved
    (app.pipeline.presets) — "local>" being the Git server of the schema repo
    at *repo_url*. A file that does not parse is an error, not an empty
    bundle: a tenant whose layer is missing must not sync without it."""
    from app.pipeline.presets import Origin, resolve_bundle_rules, scope_of

    origin = Origin.of(repo_url, token) if repo_url else None
    scope = scope_of(tenant_id)
    for name in ("bundle.json", "rules.json"):
        p = local_dir / name
        if p.exists():
            try:
                data = json.loads(p.read_text())
                rules = resolve_bundle_rules(data, origin, str(p), scope=scope)
            except Exception as exc:
                logger.error("%s cannot be used — the tenant has no rules until it is fixed: %s", p, exc)
                return [], f"{name}: {exc}"
            rules = [with_current_names(r, str(p)) for r in rules]
            from app.pipeline.schema_paths import schema_map_problems
            problems = [m for i, r in enumerate(rules)
                        for m in schema_map_problems(r.get("schema_map"), f"rules[{i}].schema_map")]
            if problems:
                logger.error("%s cannot be used — the tenant has no rules until it is fixed: %s", p, "; ".join(problems))
                return [], f"{name}: {'; '.join(problems)}"
            logger.debug("loaded %d rules from %s", len(rules), p)
            return rules, ""
    return [], ""


# ── Process-level singleton ───────────────────────────────────────────────────

_manager_instance: Optional[SchemaRegistryManager] = None
_manager_lock = threading.Lock()


def get_schema_manager() -> SchemaRegistryManager:
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:
                _manager_instance = SchemaRegistryManager()
    return _manager_instance
