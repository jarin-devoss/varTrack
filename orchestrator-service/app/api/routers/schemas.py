"""
app/api/routers/schemas.py
───────────────────────────
GET  /v1/tenants/{tenant_id}/schemas       →  list loaded CUE schema files
POST /v1/tenants/{tenant_id}/schemas/warm  →  force-clone schema repo in API process
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

router = APIRouter()


class SchemaListResponse(BaseModel):
    tenant_id:   str
    schema_root: str
    cue_files:   list[str]


class SchemaWarmRequest(BaseModel):
    repo_url: str
    branch:   str = "main"
    token:    Optional[str] = None


class SchemaWarmResponse(BaseModel):
    tenant_id:  str
    status:     str
    rules_loaded: int


# FIX (Dependency Injection): expose schema_manager via a Depends function so
# it is easy to override in tests without monkey-patching app.state.
def get_schema_manager(request: Request):
    return getattr(request.app.state, "schema_manager", None)


@router.get("/tenants/{tenant_id}/schemas", response_model=SchemaListResponse)
async def list_schemas(
    tenant_id: str, mgr=Depends(get_schema_manager)
) -> SchemaListResponse:
    # FIX (Deprecation): get_event_loop() is deprecated in Python 3.10+;
    # get_running_loop() is the correct call inside an async context.
    loop = asyncio.get_running_loop()

    if mgr is not None:
        root = await loop.run_in_executor(None, mgr.tenant_schema_root, tenant_id)
    else:
        root = None

    # FIX (Event Loop Block): rglob() makes synchronous os.scandir / os.stat
    # calls for every entry.  On large schema repos this freezes the event
    # loop for the full duration of the directory walk.  Push it to the
    # executor alongside the other blocking I/O above.
    def gather_files(r):
        if r is None or not r.exists():
            return []
        return [str(p.relative_to(r)) for p in r.rglob("*.cue")]

    try:
        cue_files: list[str] = await loop.run_in_executor(None, gather_files, root)
    except (FileNotFoundError, OSError):
        # TOCTOU: schema dir was deleted between exists() and rglob()
        # (e.g. concurrent invalidate()).  Return empty list — the next
        # request will trigger a fresh clone.
        cue_files = []

    return SchemaListResponse(
        tenant_id=tenant_id,
        schema_root=str(root) if root else "",
        cue_files=sorted(cue_files),
    )


@router.post("/tenants/{tenant_id}/schemas/warm", response_model=SchemaWarmResponse)
async def warm_schema(
    tenant_id: str, body: SchemaWarmRequest, mgr=Depends(get_schema_manager)
) -> SchemaWarmResponse:
    """
    Force-clone a tenant's schema repo inside the API process so that
    resolve_rule() can return rule configs for incoming webhooks.

    Needed for bootstrap: the schema-registry webhook runs in the Celery
    worker process (different address space); this endpoint runs the same
    get_or_clone() call inside the API process so the in-memory registry
    is populated before the first webhook arrives.
    """
    if mgr is None:
        return SchemaWarmResponse(tenant_id=tenant_id, status="no_manager", rules_loaded=0)

    # FIX (Deprecation): use get_running_loop() instead of get_event_loop().
    loop = asyncio.get_running_loop()
    # Invalidate first so the warm always fetches a fresh clone — prevents
    # stale rules being served when the schema repo was recreated (e.g. demo
    # teardown + re-run) and git-pull would diverge.
    await loop.run_in_executor(None, mgr.invalidate, tenant_id)
    await loop.run_in_executor(
        None,
        lambda: mgr.get_or_clone(tenant_id, body.repo_url, body.branch, body.token),
    )
    # FIX (Event Loop Block): get_all_rules() may parse/deserialise CUE files
    # or acquire the threading.Lock — push it to the executor so the event
    # loop is not blocked during rule loading.
    rules = await loop.run_in_executor(None, mgr.get_all_rules, tenant_id)
    return SchemaWarmResponse(
        tenant_id=tenant_id,
        status="ok",
        rules_loaded=len(rules),
    )
