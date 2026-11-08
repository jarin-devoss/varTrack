"""
app/pipeline/repo_scope.py
───────────────────────────
Which repositories a rule covers, and whether a pushed repository is one of them.

A rule names the repositories it syncs (``repositories``, patterns allowed) and
may exclude some (``exclude_repositories``). A webhook payload is written by
whoever sent it, so the repository it claims is a request, not a fact: before a
payload is processed the repository it names has to be one the rule covers and
does not exclude. A rule that names no repositories covers the one its
``repo_url`` points at.

The gateway's permission engine reads the same patterns when it decides what a
person may see and do (app/authz_client.py sends them as the rule's facts).
"""
from __future__ import annotations

import fnmatch
from typing import Any, Iterable

_GLOB_CHARS = "*?["


class RepositoryNotAllowed(Exception):
    """The payload names a repository the rule does not cover."""


def repo_name(value: str) -> str:
    """'http://gitea:3000/acme/configs.git' → 'acme/configs'; 'acme/configs' is returned as is."""
    from app.ui import ledger
    return ledger.repo_name(value)


def patterns(rule: dict) -> tuple[list[str], list[str]]:
    """(include, exclude) repository patterns of a rule, with its per-repository
    overrides folded in. With no include pattern the rule's own repo_url stands in."""
    include = list(rule.get("repositories") or [])
    exclude = list(rule.get("exclude_repositories") or [])
    for override in rule.get("overrides") or []:
        if override.get("enable"):
            include += override.get("match_repositories") or []
            exclude += override.get("exclude_repositories") or []
    if not include and rule.get("repo_url"):
        include = [repo_name(rule["repo_url"])]
    return include, exclude


DEFAULT_ENV = "default"


def own_branch(rule: dict) -> str:
    """The rule's own branch ("main" when it names none): what feeds "default" and the file_path_map
    environments. "" when branch_map sends that branch to an environment: its files land there."""
    branch = str(rule.get("branch") or "main")
    return "" if branch in (rule.get("branch_map") or {}) else branch


def branch_for(rule: dict, env: str) -> str:
    """The branch whose files land in *env*: the inverse of the ETL's environment resolver
    (env_resolver.resolve), in its order. "" when no branch feeds it.

      1. a branch_map entry maps a branch to *env*: that branch;
      2. *env* is one the file_path_map names: the rule's own branch;
      3. branch_as_env: *env* itself;
      4. *env* is "default", what a push resolves to when nothing else applies: the rule's own branch.

    The gateway's permission engine answers the same question (authz.branchFor); both are held to
    gateway-service/internal/authz/testdata/branch_for.json (tests/unit/test_branch_for_contract.py).
    With sign-in on, the engine's answer is the one used; this is the open web UI's, where nobody signs
    in and no permission is judged."""
    branches = sorted(b for b, e in (rule.get("branch_map") or {}).items() if e == env)
    if branches:
        return branches[0]
    if env in (rule.get("file_path_map") or {}):
        return own_branch(rule)
    if rule.get("branch_as_env"):
        return env
    if env == DEFAULT_ENV:
        return own_branch(rule)
    return ""


def nothing_feeds(rule: dict, env: str) -> str:
    """Why *env* cannot be synced or dry-run when no branch feeds it (the engine's words, too)."""
    return (f"nothing feeds {env} in the {rule.get('datasource', '')} rule: no branch_map entry maps a branch to it, "
            f"branch_as_env is off, and it is not default or a file_path_map environment "
            f"(an earlier version of the rule wrote it; it is not synced any more)")


def is_glob(pattern: str) -> bool:
    return any(c in _GLOB_CHARS for c in pattern)


def matches(repo: str, pattern_list: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(repo.lower(), p.lower()) for p in pattern_list)


def pushed_repositories(payload: Any) -> list[str]:
    """Every repository name a webhook payload claims, as 'owner/name'.

    The platforms spell it differently (repository.full_name, repository.clone_url,
    project.path_with_namespace, project.git_http_url); all of them that are
    present are returned, so a payload whose spellings disagree cannot slip one
    past the check while another passes it."""
    if not isinstance(payload, dict):
        return []
    found: list[str] = []
    repo = payload.get("repository")
    if isinstance(repo, dict):
        for key in ("full_name", "clone_url"):
            value = repo.get(key)
            if isinstance(value, str) and value.strip():
                found.append(repo_name(value.strip()))
    project = payload.get("project")
    if isinstance(project, dict):
        for key in ("path_with_namespace", "git_http_url"):
            value = project.get(key)
            if isinstance(value, str) and value.strip():
                found.append(repo_name(value.strip()))
    return list(dict.fromkeys(n for n in found if n))


def check_repository(rule: dict, payload: Any) -> None:
    """Raise RepositoryNotAllowed unless every repository the payload names is
    covered by *rule* and not excluded by it. A rule with nothing to compare
    against (no repositories and no repo_url) covers whatever it is sent."""
    include, exclude = patterns(rule)
    if not include:
        return
    names = pushed_repositories(payload)
    if not names:
        raise RepositoryNotAllowed("the payload does not name the repository it was pushed to")
    for name in names:
        if matches(name, exclude):
            raise RepositoryNotAllowed(f"repository {name!r} is excluded by the rule")
        if not matches(name, include):
            raise RepositoryNotAllowed(f"repository {name!r} is not covered by the rule")
