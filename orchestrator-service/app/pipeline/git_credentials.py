"""
app/pipeline/git_credentials.py
───────────────────────────────
The credential a rule's own repositories are read with.

A rule has no token of its own, so before this a private repository could not be cloned at all (a clone with
no credential fails, and used to be reported as "no files"). The credential is the one of the rule's platform
in config.cue (the one sign-in and presets already use for that platform), and it goes only to the Git server
that platform names: a rule covering ``org/*`` takes its repository from the push payload, which is the
sender's claim, and a claim never gets to say where a credential is sent.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _host(url: str) -> str:
    return urlparse(url).netloc.rsplit("@", 1)[-1].lower()


def credential_for(rule_config: dict, repo_url: str) -> Optional[str]:
    """The token to read *repo_url* with for a rule: one the rule carries, else its platform's from
    config.cue when *repo_url* is on that platform's server, else None (an anonymous read)."""
    if rule_config.get("token"):
        return str(rule_config["token"])
    name = rule_config.get("platform") or ""
    if not name or not repo_url:
        return None
    from app.pipeline.presets import PresetError, _platform

    try:
        endpoint, token = _platform(str(name))
    except PresetError:
        return None
    if not token or not endpoint:
        return None
    if _host(endpoint) != _host(repo_url):
        logger.debug("git credential of platform %s not sent: %s is not on its server", name, _host(repo_url))
        return None
    return token
