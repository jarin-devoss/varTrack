from __future__ import annotations
import json

from app.pipeline.models import PayloadContext
from app.tasks.payload import (
    extract_ref, extract_branch, extract_commit,
    extract_tag, extract_pr_number
)
from app.pipeline.schema_utils import rule_from_bundle

def run_stage_payload(
    raw_payload: str,
    platform:    str,
    rule_config: dict,
    datasource:  str,
    tenant_id:   str,
) -> PayloadContext | None:
    """
    Parse the raw webhook body and resolve the matching rule.

    Returns None if the rule cannot be resolved.
    Raises json.JSONDecodeError if the payload is malformed.
    """
    payload = json.loads(raw_payload)

    platform = platform.lower()

    if not rule_config:
        rule_config = rule_from_bundle(platform, datasource, tenant_id) or {}
    if not rule_config:
        return None

    repo_url   = rule_config.get("repo_url", "")
    ref        = extract_ref(payload, platform)
    branch     = extract_branch(payload, platform)
    commit_sha = extract_commit(payload, platform)
    tag        = extract_tag(payload, platform)
    pr_number  = extract_pr_number(payload, platform)

    return PayloadContext(
        platform=platform,
        repo_url=repo_url,
        ref=ref,
        branch=branch,
        commit_sha=commit_sha,
        tag=tag,
        pr_number=pr_number,
        rule_config=rule_config,
        parsed_payload=payload,
    )
