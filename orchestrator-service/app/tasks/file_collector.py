"""
app/tasks/file_collector.py
────────────────────────────
Decides which files to pull from git for a given rule and webhook payload,
then returns their content.

A rule names what it reads, so there are two ways:
  1. rule has file_name       → fetch that single file
  2. rule has file_path_map   → fetch each unique path in the map

Nothing reads a file no one named: a push that touched other files does not sync them. A rule that names neither is
refused when it is loaded (rule_layers.refusal), so it never gets here; if one did, this says so instead of guessing.
"""
from __future__ import annotations

import logging

from app.pipeline.git_extractor import GitExtractor

logger = logging.getLogger(__name__)


class NoFileNamed(ValueError):
    """The rule names neither file_name nor file_path_map, so there is nothing it reads."""


def collect(
    payload: dict,
    platform: str,
    rule_config: dict,
    repo_url: str,
    ref: str,
    revision: str = "",
) -> list[tuple[str, str]]:
    """
    Return a list of (file_path, content) pairs to process.

    Parameters
    ----------
    payload:      parsed webhook JSON (not read: what the push touched does not decide what a rule reads)
    platform:     "github" | "gitlab" | "bitbucket"
    rule_config:  VarTrack rule dict
    repo_url:     target repository URL
    ref:          git ref the push was made to (e.g. "refs/heads/main")
    revision:     the commit that was pushed; files are read at it, not at
                  whatever the ref points to by now

    Raises NoFileNamed when the rule names no file.
    """
    extractor = GitExtractor()
    from app.pipeline.git_credentials import credential_for

    token     = credential_for(rule_config, repo_url)
    commit    = revision or None

    # 1 – the one file the rule names
    file_name = rule_config.get("file_name")
    if file_name:
        content = extractor.extract_file(
            repo_url, ref, file_name, token=token, revision=commit
        )
        if content is None:
            logger.warning("file_name=%s not found in %s@%s", file_name, repo_url, ref)
            return []
        return [(file_name, content)]

    # 2 – the file of each environment
    file_path_map = rule_config.get("file_path_map", {})
    if file_path_map:
        results = []
        for fp in sorted(set(file_path_map.values())):
            content = extractor.extract_file(
                repo_url, ref, fp, token=token, revision=commit
            )
            if content is not None:
                results.append((fp, content))
            else:
                logger.warning("file_path_map path=%s not found in %s@%s", fp, repo_url, ref)
        return results

    raise NoFileNamed(
        f"{rule_config.get('platform', platform)}/{rule_config.get('datasource', '?')} names no file to read: "
        f"set file_name or file_path_map in config.cue or the tenant's bundle.json"
    )
