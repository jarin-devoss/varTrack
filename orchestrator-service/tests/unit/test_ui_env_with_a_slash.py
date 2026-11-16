"""
tests/unit/test_ui_env_with_a_slash.py

An environment can be a branch name with a slash (branch_as_env: `feature/login`). The proxy hands the route the decoded
path, so the routes that take an environment in the path have to match one: without it the UI's Keys and Diff, and
Forget, answered 404 for such an environment.
"""
from __future__ import annotations

import pytest
from starlette.routing import Match

from app.api.routers.ui import router


def match(method: str, path: str):
    for route in router.routes:
        found, scope = route.matches({"type": "http", "method": method, "path": path})
        if found == Match.FULL:
            return route.path, scope["path_params"]
    return None


@pytest.mark.parametrize("env", ["production", "feature/login", "release/2026/11", "a-b_c.d"])
def test_the_keys_route_matches_the_environment(env):
    route, params = match("GET", f"/ui/rules/gitea/mongo/targets/{env}/keys")
    assert route.endswith("/targets/{env:path}/keys")
    assert (params["platform"], params["datasource"], params["env"]) == ("gitea", "mongo", env)


@pytest.mark.parametrize("env", ["production", "feature/login", "release/2026/11"])
def test_the_forget_route_matches_the_environment(env):
    route, params = match("DELETE", f"/ui/rules/gitea/mongo/targets/{env}")
    assert route.endswith("/targets/{env:path}")
    assert params["env"] == env


def test_a_path_that_ends_in_keys_is_not_taken_for_the_forget_route_of_another_environment():
    """DELETE .../targets/feature/login/keys is the forget route of an environment called `feature/login/keys`; the GET
    keys route is another method, so the two never meet."""
    assert match("GET", "/ui/rules/gitea/mongo/targets/feature/login/keys")[1]["env"] == "feature/login"
    assert match("DELETE", "/ui/rules/gitea/mongo/targets/feature/login/keys")[1]["env"] == "feature/login/keys"
