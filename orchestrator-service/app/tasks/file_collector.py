"""
app/tasks/file_collector.py
────────────────────────────
Decides which files to pull from git for a given rule and webhook payload,
then returns their content.

Three strategies (checked in order):
  1. rule has file_name       → fetch that single file
  2. rule has file_path_map   → fetch each unique path in the map
  3. fallback                 → fetch every file touched in the push commits
"""
from __future__ import annotations

import logging

from app.pipeline.git_extractor import GitExtractor
from app.tasks.payload import extract_changed_filenames

logger = logging.getLogger(__name__)


def collect(
    payload: dict,
    platform: str,
    rule_config: dict,
    repo_url: str,
    ref: str,
) -> list[tuple[str, str]]:
    """
    Return a list of (file_path, content) pairs to process.

    Parameters
    ----------
    payload:      parsed webhook JSON
    platform:     "github" | "gitlab" | "bitbucket"
    rule_config:  VarTrack rule dict
    repo_url:     target repository URL
    ref:          git ref to check out (e.g. "refs/heads/main")
    """
    extractor = GitExtractor()
    token     = rule_config.get("token")

    # Strategy 1 – single tracked file
    file_name = rule_config.get("file_name")
    if file_name:
        content = extractor.extract_file(repo_url, ref, file_name, token=token)
        if content is None:
            logger.warning("file_name=%s not found in %s@%s", file_name, repo_url, ref)
            return []
        return [(file_name, content)]

    # Strategy 2 – env → file_path mapping
    file_path_map = rule_config.get("file_path_map", {})
    if file_path_map:
        results = []
        for fp in sorted(set(file_path_map.values())):
            content = extractor.extract_file(repo_url, ref, fp, token=token)
            if content is not None:
                results.append((fp, content))
            else:
                logger.warning("file_path_map path=%s not found in %s@%s", fp, repo_url, ref)
        return results

    # Strategy 3 – everything touched by the push
    changed_names = extract_changed_filenames(payload, platform)

    # Exclude VarTrack metadata files — they are processed separately and
    # must not be synced to the datasource as config variables.
    from app.pipeline.repo_overrides import VARTRACK_JSON
    changed_names = [f for f in changed_names if f != VARTRACK_JSON]

    max_files = int(rule_config.get("max_changed_files", 500))
    if len(changed_names) > max_files:
        logger.warning(
            "file_collector: push contains %d changed files; capping at %d "
            "(set max_changed_files in rule_config to change the limit)",
            len(changed_names), max_files,
        )
        changed_names = changed_names[:max_files]

    results = []
    for fc in extractor.extract_changed_files(repo_url, ref, changed_names, token=token):
        if fc.content:
            results.append((fc.path, fc.content))
    return results
