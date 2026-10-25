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

Tenant repos are configured via env vars:
    SCHEMA_TENANT_<ID>_REPO    git clone URL
    SCHEMA_TENANT_<ID>_BRANCH  branch (default: main)
    SCHEMA_TENANT_<ID>_TOKEN   optional auth token
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

from app.config import settings
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

    def is_stale(self) -> bool:
        return (time.time() - self.fetched_at) > settings.SCHEMA_TTL_SECONDS


class SchemaRegistryManager:
    """Thread-safe manager for per-tenant CUE schema registry clones."""

    def __init__(self) -> None:
        self._root = settings.SCHEMA_CACHE_DIR
        self._root.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, _TenantEntry] = {}
        self._tenant_configs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load_env_configs()

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

    def list_tenant_ids(self) -> list[str]:
        """Return all registered tenant IDs (from env-var config or runtime registration)."""
        with self._lock:
            return list(self._tenant_configs.keys())

    def register_tenant(
        self,
        tenant_id: str,
        repo_url: str,
        branch: str = "main",
        token: Optional[str] = None,
    ) -> None:
        """Register (or update) a tenant's schema repo at runtime."""
        with self._lock:
            self._tenant_configs[tenant_id] = {
                "repo_url": repo_url,
                "branch":   branch,
                "token":    token,
            }
        logger.info("registered schema repo tenant=%s repo=%s", tenant_id, repo_url)

    def get_or_clone(
        self,
        tenant_id: str,
        repo_url: Optional[str] = None,
        branch: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Path:
        """
        Return local path of the tenant's schema clone.
        Clones on first call; re-fetches on TTL expiry.
        *branch* defaults to the tenant's registered branch, then "main".
        """
        cfg = self._tenant_configs.get(tenant_id, {})
        repo_url = repo_url or cfg.get("repo_url")
        branch   = branch   or cfg.get("branch", "main")
        token    = token    or cfg.get("token")

        if not repo_url:
            return self._root / tenant_id   # sentinel (empty dir)

        _validate_repo_url(repo_url)

        cache_key = f"{tenant_id}:{_slug(repo_url, branch)}"

        # Hold the lock only to READ cache state, release before network I/O
        # (clone/fetch), then re-acquire to WRITE the result.  This prevents
        # the global lock from being held for the full duration of a git clone.
        with self._lock:
            entry = self._entries.get(cache_key)
            needs_clone   = entry is None
            needs_refresh = (not needs_clone) and entry.is_stale()

        if needs_clone:
            # Clone outside the lock — network I/O should never hold a global lock.
            _t = time.perf_counter()
            new_entry = self._clone(tenant_id, repo_url, branch, token)
            _elapsed = time.perf_counter() - _t
            with self._lock:
                # Double-check: another thread may have cloned while we were busy.
                existing = self._entries.get(cache_key)
                if existing is None:
                    self._entries[cache_key] = new_entry
                    entry = new_entry
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
                _elapsed = time.perf_counter() - _t
                with self._lock:
                    self._entries[cache_key] = refreshed
                    entry = refreshed
                logger.info("schema refreshed tenant=%s sha=%s", tenant_id, entry.commit_sha)
                m = _m()
                if m:
                    try:
                        m.inc_schema_op("refresh", "ok")
                        m.observe_schema_clone(tenant_id, _elapsed)
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("schema refresh failed, using stale: %s", exc)
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
        if self.bundle_error(tenant_id):
            return None
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
        except Exception as exc:
            logger.error("schema clone failed: %s", exc)
            return self._empty_entry(tenant_id, repo_url, branch, local_dir)

        rules, bundle_error = _load_bundle_checked(local_dir, repo_url, token)
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
        repo.remote("origin").pull(entry.branch, depth=1, env=_git_env)
        sha = repo.head.commit.hexsha

        rules, bundle_error = _load_bundle_checked(entry.local_path, entry.repo_url, token)
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
    ) -> _TenantEntry:
        lp = local_path or (self._root / tenant_id)
        lp.mkdir(parents=True, exist_ok=True)
        return _TenantEntry(
            tenant_id=tenant_id,
            repo_url=repo_url,
            branch=branch,
            local_path=lp,
        )

    def _load_env_configs(self) -> None:
        """
        Auto-register tenants from env vars:
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


def _load_bundle(local_dir: Path, repo_url: str = "", token: Optional[str] = None) -> list[dict]:
    """The rules of bundle.json (or rules.json); none when it cannot be used."""
    return _load_bundle_checked(local_dir, repo_url, token)[0]


def _load_bundle_checked(local_dir: Path, repo_url: str = "", token: Optional[str] = None) -> tuple[list[dict], str]:
    """(rules, error): bundle.json (or rules.json) parsed for worker-side rule
    resolution, with the presets its "extends" name resolved
    (app.pipeline.presets) — "local>" being the Git server of the schema repo
    at *repo_url*. A file that does not parse is an error, not an empty
    bundle: a tenant whose layer is missing must not sync without it."""
    from app.pipeline.presets import Origin, resolve_bundle_rules

    origin = Origin.of(repo_url, token) if repo_url else None
    for name in ("bundle.json", "rules.json"):
        p = local_dir / name
        if p.exists():
            try:
                data = json.loads(p.read_text())
                rules = resolve_bundle_rules(data, origin, str(p))
            except Exception as exc:
                logger.error("%s cannot be used — the tenant has no rules until it is fixed: %s", p, exc)
                return [], f"{name}: {exc}"
            rules = [with_current_names(r, str(p)) for r in rules]
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
