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


def extract_changed_filenames(payload: dict, platform: str) -> list[str]:
    names: set[str] = set()
    for commit in payload.get("commits", []):
        for key in ("added", "modified", "removed"):
            names.update(commit.get(key, []))
    if not names and "object_attributes" in payload:
        names.update(payload["object_attributes"].get("files", []))
    return list(names)


def token_from_headers(headers: dict[str, str]) -> str | None:
    auth = headers.get("authorization", headers.get("Authorization", ""))
    return auth[7:] if auth.startswith("Bearer ") else None


# ── Structured prune config ───────────────────────────────────────────────────

class PruneConfig:
    """
    Parsed representation of the rule_config ``prune`` block.

    All four valid forms::

        prune: true                              # enabled, last=false, dry_run=false
        prune: false                             # disabled
        prune: { last: true,  dry_run: false }   # enabled, defer deletion
        prune: { last: false, dry_run: false }   # enabled, immediate deletion

    The structured form may also carry an explicit ``enabled`` key::

        prune: { enabled: false, last: false, dry_run: false }  # disabled
    """

    __slots__ = ("enabled", "last", "dry_run")

    def __init__(self, enabled: bool, last: bool, dry_run: bool) -> None:
        self.enabled = enabled
        self.last    = last
        self.dry_run = dry_run

    def __repr__(self) -> str:
        return (
            f"PruneConfig(enabled={self.enabled}, "
            f"last={self.last}, dry_run={self.dry_run})"
        )

    def with_dry_run(self) -> "PruneConfig":
        """Return a copy with dry_run forced to True (used by the dry-run route)."""
        return PruneConfig(enabled=self.enabled, last=self.last, dry_run=True)


def extract_prune_config(rule_config: dict) -> PruneConfig:
    """
    Parse ``rule_config["prune"]`` into a :class:`PruneConfig`.

    Supports both the new structured form and the legacy boolean form.
    """
    raw = rule_config.get("prune", False)

    if isinstance(raw, dict):
        return PruneConfig(
            enabled=bool(raw.get("enabled", True)),  # structured form defaults to enabled
            last=bool(raw.get("last", False)),
            dry_run=bool(raw.get("dry_run", False)),
        )

    # Legacy boolean: prune: true / false
    return PruneConfig(enabled=bool(raw), last=False, dry_run=False)
