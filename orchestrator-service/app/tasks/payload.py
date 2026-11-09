"""
app/tasks/payload.py
─────────────────────
Helpers to extract fields from raw git webhook payloads.
Supports GitHub, GitLab, Bitbucket.
"""
from __future__ import annotations


def extract_ref(payload: dict, platform: str) -> str:
    if platform in ("github", "gitea", "gitlab"):
        return payload.get("ref", "")
    if platform == "bitbucket":
        changes = payload.get("push", {}).get("changes", [])
        if changes:
            name = changes[0].get("new", {}).get("name", "")
            return f"refs/heads/{name}"
    return payload.get("ref", "")


def extract_repo_url(payload: dict) -> str:
    """The clone URL of the pushed repository: repository.clone_url (GitHub,
    Gitea), else project.git_http_url (GitLab)."""
    repo = payload.get("repository")
    if isinstance(repo, dict) and repo.get("clone_url"):
        return str(repo["clone_url"])
    project = payload.get("project")
    if isinstance(project, dict) and project.get("git_http_url"):
        return str(project["git_http_url"])
    return ""


def extract_branch(payload: dict, platform: str) -> str:
    return extract_ref(payload, platform).removeprefix("refs/heads/")


def extract_commit(payload: dict, platform: str) -> str:
    return payload.get("after", payload.get("checkout_sha", ""))


def extract_tag(payload: dict, platform: str) -> str | None:
    ref = extract_ref(payload, platform)
    return ref.removeprefix("refs/tags/") if ref.startswith("refs/tags/") else None


def extract_pr_number(payload: dict, platform: str) -> str | None:
    pr = payload.get("pull_request") or payload.get("object_attributes")
    if pr:
        num = pr.get("number") or pr.get("iid")
        return str(num) if num else None
    return None


# ── Structured prune config ───────────────────────────────────────────────────

class PruneConfig:
    """
    Parsed representation of the rule_config ``prune`` block.

    All valid forms::

        prune: true                              # enabled
        prune: false                             # disabled

    The structured form carries an explicit ``enabled`` key, and the glob
    patterns of keys never to delete (the proto's PruneConfig)::

        prune: { enabled: false }                # disabled
        prune: { enabled: true, protection: ["SYSTEM_*"] }
        prune: { protection: ["SYSTEM_*"] }      # disabled: protection only

    Prune runs once per environment, after all its files are written, so the
    former ``last`` key has nothing left to do, and ``dry_run`` (report what
    would be deleted, delete nothing) is a dry run's job: neither is read, and
    neither enables prune. ``prune: { last: true }`` is a disabled block.
    """

    __slots__ = ("enabled", "protection")

    def __init__(self, enabled: bool, protection: list[str] | None = None) -> None:
        self.enabled    = enabled
        self.protection = list(protection or [])

    def __repr__(self) -> str:
        return f"PruneConfig(enabled={self.enabled}, protection={self.protection})"


def extract_prune_config(rule_config: dict) -> PruneConfig:
    """
    Parse ``rule_config["prune"]`` into a :class:`PruneConfig`.

    Supports both the structured form and the legacy boolean form. The flat
    key prune_protection (the proto's former field, still found in bundle.json
    files) fills what the structured form leaves out.
    """
    raw = rule_config.get("prune", False)
    protection = list(rule_config.get("prune_protection") or [])

    if isinstance(raw, dict):
        # A block that only lists protection (a preset's) is not enabled.
        return PruneConfig(enabled=bool(raw.get("enabled", False)), protection=list(raw.get("protection") or protection))

    # Legacy boolean: prune: true / false
    return PruneConfig(enabled=bool(raw), protection=protection)
