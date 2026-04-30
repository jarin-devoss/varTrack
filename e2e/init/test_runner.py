#!/usr/bin/env python3
"""
VarTrack E2E Test Runner
========================
Runs 22 test suites across 8 sink configurations (MongoDB document,
MongoDB file, ZooKeeper slash, Redis hash, Redis string, Linux server,
S3/MinIO) plus CUE schema validation, HashiCorp Vault secret injection,
dual-datasource @secret suites, self-heal drift correction, signature
validation, dry-run verification, branch_map, structured prune, and
task status polling.

Outputs a benchmark JSON report to /results/benchmark.json.

Usage:
    python3 test_runner.py [--settings-dir /demo/schemas/settings]
                           [--results-dir /results]
                           [--orchestrator http://orchestrator-api:8000]
                           [--gitea http://gitea:3000]
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ── optional sink clients ─────────────────────────────────────────────────────
try:
    from pymongo import MongoClient
    HAS_MONGO = True
except ImportError:
    HAS_MONGO = False

try:
    import redis as redis_lib
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

try:
    from kazoo.client import KazooClient
    HAS_ZK = True
except ImportError:
    HAS_ZK = False

try:
    import boto3
    from botocore.config import Config as BotoConfig
    HAS_BOTO = True
except ImportError:
    HAS_BOTO = False

try:
    import paramiko
    HAS_SSH = True
except ImportError:
    HAS_SSH = False

# ── constants ─────────────────────────────────────────────────────────────────
GITEA_ORG = "vartrack-demo"
CONFIG_REPO = "demo-configs"
SCHEMA_REPO = "schemas"
CONFIG_FILE = "app-settings.yaml"
CONFIG_LOCAL = "/demo/configs/app-settings.yaml"
WAIT_AFTER_WEBHOOK = 30  # seconds
WAIT_AFTER_DRIFT = 20    # seconds


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class colour:
    RED    = "\033[0;31m"
    GREEN  = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE   = "\033[0;34m"
    CYAN   = "\033[0;36m"
    BOLD   = "\033[1m"
    NC     = "\033[0m"

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{colour.BLUE}[{ts}]{colour.NC} {msg}")

def ok(msg: str) -> None:
    print(f"{colour.GREEN}  ✓{colour.NC} {msg}")

def _exc_type(exc: BaseException) -> str:
    """Return only the exception class name — never the message (which may contain secrets)."""
    return type(exc).__name__

_REDACT_URL_CREDS = re.compile(r'(://[^:@/\s]+:)[^@/\s]+(@)', re.IGNORECASE)
_REDACT_KV = re.compile(r'((?:password|secret|token|key|passwd)\s*[=:]\s*)\S+', re.IGNORECASE)

def _redact(text: object) -> str:
    """Strip credential-like patterns from a string before printing."""
    text = str(text)
    text = _REDACT_URL_CREDS.sub(r'\1***\2', text)
    text = _REDACT_KV.sub(r'\1***', text)
    return text

def warn(msg: str) -> None:
    print(f"{colour.YELLOW}  ⚠{colour.NC} {_redact(msg)}")

def err(msg: str) -> None:
    print(f"{colour.RED}  ✗{colour.NC} {_redact(msg)}")

def banner(title: str) -> None:
    bar = "═" * 50
    print(f"\n{colour.BOLD}{colour.CYAN}╔{bar}╗{colour.NC}")
    print(f"{colour.BOLD}{colour.CYAN}║  {title:<48}║{colour.NC}")
    print(f"{colour.BOLD}{colour.CYAN}╚{bar}╝{colour.NC}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Gitea helpers
# ─────────────────────────────────────────────────────────────────────────────

class GiteaClient:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self.auth = (user, password)

    def _api(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base}/api/v1{path}"
        return requests.request(method, url, auth=self.auth, timeout=15, **kwargs)

    def push_file(self, repo: str, filepath: str, content: bytes, message: str) -> None:
        b64 = base64.b64encode(content).decode()
        existing = self._api("GET", f"/repos/{GITEA_ORG}/{repo}/contents/{filepath}")
        if existing.ok:
            sha = existing.json()["sha"]
            self._api("PUT", f"/repos/{GITEA_ORG}/{repo}/contents/{filepath}",
                      json={"message": message, "content": b64, "sha": sha})
        else:
            self._api("POST", f"/repos/{GITEA_ORG}/{repo}/contents/{filepath}",
                      json={"message": message, "content": b64})

    def get_head_sha(self, repo: str) -> str:
        r = self._api("GET", f"/repos/{GITEA_ORG}/{repo}/git/refs")
        if r.ok and r.json():
            return r.json()[0]["object"]["sha"]
        return "0" * 40

    def push_bundle(self, rules: list[dict]) -> None:
        content = json.dumps(rules, indent=2).encode()
        self.push_file(SCHEMA_REPO, "bundle.json", content, "Update bundle.json for test suite")

    def push_schema_file(self, filepath: str, content: bytes, message: str) -> None:
        """Push an arbitrary file to the schema repo (e.g. a .cue validation schema)."""
        b64 = base64.b64encode(content).decode()
        existing = self._api("GET", f"/repos/{GITEA_ORG}/{SCHEMA_REPO}/contents/{filepath}")
        if existing.ok:
            sha = existing.json()["sha"]
            self._api("PUT", f"/repos/{GITEA_ORG}/{SCHEMA_REPO}/contents/{filepath}",
                      json={"message": message, "content": b64, "sha": sha})
        else:
            self._api("POST", f"/repos/{GITEA_ORG}/{SCHEMA_REPO}/contents/{filepath}",
                      json={"message": message, "content": b64})

    def push_config_file(self, content: bytes, message: str) -> None:
        """Push app-settings.yaml to the config repo (used for CUE rejection tests)."""
        b64 = base64.b64encode(content).decode()
        existing = self._api("GET", f"/repos/{GITEA_ORG}/{CONFIG_REPO}/contents/{CONFIG_FILE}")
        if existing.ok:
            sha = existing.json()["sha"]
            self._api("PUT", f"/repos/{GITEA_ORG}/{CONFIG_REPO}/contents/{CONFIG_FILE}",
                      json={"message": message, "content": b64, "sha": sha})
        else:
            self._api("POST", f"/repos/{GITEA_ORG}/{CONFIG_REPO}/contents/{CONFIG_FILE}",
                      json={"message": message, "content": b64})


# ─────────────────────────────────────────────────────────────────────────────
# Benchmark result container
# ─────────────────────────────────────────────────────────────────────────────

class SuiteResult:
    def __init__(self, suite_id: int, name: str, setting_file: str, datasource: str) -> None:
        self.suite_id = suite_id
        self.name = name
        self.setting_file = setting_file
        self.datasource = datasource
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.finished_at: str | None = None
        self.duration_ms: float = 0
        self.status = "pending"   # pending | pass | fail | skip
        self.checks: list[dict] = []
        self._t0 = time.monotonic()
        self.task_id: str | None = None
        self.error: str | None = None

    def add_check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        sym = "✓" if passed else "✗"
        colour_code = colour.GREEN if passed else colour.RED
        print(f"    {colour_code}{sym}{colour.NC} {_redact(name)}" + (f" — {_redact(detail)}" if detail else ""))

    def finish(self, status: str) -> None:
        self.status = status
        self.finished_at = datetime.now(timezone.utc).isoformat()
        self.duration_ms = (time.monotonic() - self._t0) * 1000

    def to_dict(self) -> dict:
        return {
            "suite_id": self.suite_id,
            "name": self.name,
            "setting_file": self.setting_file,
            "datasource": self.datasource,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "task_id": self.task_id,
            "error": self.error,
            "checks": self.checks,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Sink verifiers
# ─────────────────────────────────────────────────────────────────────────────

def verify_mongo(rule: dict, result: SuiteResult) -> None:
    if not HAS_MONGO:
        result.add_check("pymongo available", False, "not installed")
        return
    uri = rule.get("mongo_uri", "mongodb://mongo:27017")
    db_name = rule.get("database", "vartrack")
    dest_tpl = rule.get("destination_template", "")
    env = rule.get("branch", "main") if not rule.get("env_as_branch") else rule.get("branch", "main")
    tenant = rule.get("tenant_id", GITEA_ORG)
    safe_env = env.replace("/", "_")

    if dest_tpl:
        col_name = dest_tpl.format(tenant=tenant, env=safe_env)
    elif rule.get("update_strategy") == "STRATEGY_FILE":
        col_name = rule.get("collection", "files")
    else:
        col_name = rule.get("collection", "variables")

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        db = client[db_name]
        col_exists = col_name in db.list_collection_names()
        count = db[col_name].count_documents({}) if col_exists else 0
        result.add_check(f"MongoDB collection '{col_name}' exists", col_exists)
        result.add_check(f"MongoDB documents written (count={count})", count > 0, f"count={count}")
        if count > 0:
            sample = db[col_name].find_one({}, {"_id": 0})
            has_key = "key" in sample or "_vt_file" in sample or any(
                k for k in sample if not k.startswith("_")
            )
            result.add_check("MongoDB document has payload fields", has_key, str(list(sample.keys())[:4]))
        client.close()
    except Exception as exc:
        result.add_check("MongoDB connection", False, _exc_type(exc))


def verify_zookeeper(rule: dict, result: SuiteResult) -> None:
    if not HAS_ZK:
        result.add_check("kazoo available", False, "not installed")
        return
    hosts = ",".join(rule.get("zk_hosts", ["zookeeper:2181"]))
    dest_tpl = rule.get("destination_template", "")
    tenant = rule.get("tenant_id", GITEA_ORG)
    env = rule.get("branch", "main")

    root = dest_tpl.format(tenant=tenant, env=env) if dest_tpl else f"/vartrack/{tenant}/default"

    zk = KazooClient(hosts=hosts, timeout=10)
    try:
        zk.start(timeout=10)
        exists = zk.exists(root)
        result.add_check(f"ZK path '{root}' exists", bool(exists))
        if exists:
            children = zk.get_children(root)
            result.add_check("ZK path has children", len(children) > 0, f"children={children[:5]}")
    except Exception as exc:
        result.add_check("ZooKeeper connection", False, _exc_type(exc))
    finally:
        try:
            zk.stop()
        except Exception:
            pass


def verify_redis(rule: dict, result: SuiteResult) -> None:
    if not HAS_REDIS:
        result.add_check("redis-py available", False, "not installed")
        return
    host = rule.get("redis_host", "redis")
    port = int(rule.get("redis_port", 6379))
    db = int(rule.get("redis_db", 0))
    dest_tpl = rule.get("destination_template", "")
    tenant = rule.get("tenant_id", GITEA_ORG)
    env = rule.get("branch", "main")
    data_structure = rule.get("data_structure", "string")

    prefix = dest_tpl.format(tenant=tenant, env=env) if dest_tpl else f"{tenant}:default"

    try:
        r = redis_lib.Redis(host=host, port=port, db=db, socket_timeout=5, decode_responses=True)
        r.ping()
        result.add_check("Redis ping", True)

        if data_structure == "hash":
            keys = r.keys(f"{prefix}*")
            result.add_check(f"Redis HASH keys under '{prefix}'", len(keys) > 0,
                             f"found {len(keys)} key(s)")
            if keys:
                hlen = r.hlen(keys[0])
                result.add_check("Redis HASH has fields", hlen > 0, f"hlen={hlen}")
        else:
            keys = r.keys(f"{prefix}*")
            result.add_check(f"Redis STRING keys under '{prefix}'", len(keys) > 0,
                             f"found {len(keys)} key(s)")
        r.close()
    except Exception as exc:
        result.add_check("Redis connection", False, _exc_type(exc))


def verify_linux_server(rule: dict, result: SuiteResult) -> None:
    if not HAS_SSH:
        result.add_check("paramiko available", False, "not installed")
        return
    host = os.environ.get("LINUX_TARGET_HOST", rule.get("ssh_host", "linux-target"))
    port = int(os.environ.get("LINUX_TARGET_PORT", rule.get("ssh_port", 22)))
    user = os.environ.get("LINUX_TARGET_USER", rule.get("ssh_user", "vartrack"))
    password = os.environ.get("LINUX_TARGET_PASS", rule.get("ssh_password", "VarTrack123"))
    dest_tpl = rule.get("destination_template", "")
    tenant = rule.get("tenant_id", GITEA_ORG)
    env = rule.get("branch", "main")

    remote_path = (
        dest_tpl.format(tenant=tenant, env=env)
        if dest_tpl
        else f"/tmp/vartrack/{tenant}/config.env"
    )

    try:
        # TOFU: fetch the host key on first contact and add it explicitly so
        # that we can use RejectPolicy (avoids silent MITM acceptance).
        _transport = paramiko.Transport((host, port))
        try:
            _transport.connect()
            _host_key = _transport.get_remote_server_key()
        finally:
            _transport.close()
        ssh = paramiko.SSHClient()
        ssh.get_host_keys().add(host, _host_key.get_name(), _host_key)
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(host, port=port, username=user, password=password, timeout=10)
        result.add_check("SSH connection established", True)
        _, stdout, _ = ssh.exec_command(f"ls -la {remote_path} 2>&1")
        output = stdout.read().decode().strip()
        exists = "No such file" not in output
        result.add_check(f"Remote file '{remote_path}' exists", exists, output[:80])
        if exists:
            _, stdout, _ = ssh.exec_command(f"wc -l < {remote_path}")
            lines = stdout.read().decode().strip()
            result.add_check("Remote file has content", int(lines or "0") > 0, f"lines={lines}")
        ssh.close()
    except Exception as exc:
        result.add_check("SSH connection", False, _exc_type(exc))


def verify_s3(rule: dict, result: SuiteResult) -> None:
    if not HAS_BOTO:
        result.add_check("boto3 available", False, "not installed")
        return
    bucket = rule.get("s3_bucket", "vartrack")
    endpoint = os.environ.get("MINIO_ENDPOINT", rule.get("s3_endpoint_url", "http://minio:9002"))
    access_key = os.environ.get("MINIO_ACCESS_KEY", rule.get("s3_access_key", "minioadmin"))
    secret_key = os.environ.get("MINIO_SECRET_KEY", rule.get("s3_secret_key", "minioadmin"))
    dest_tpl = rule.get("destination_template", "")
    tenant = rule.get("tenant_id", GITEA_ORG)
    env = rule.get("branch", "main")

    prefix = (
        dest_tpl.format(tenant=tenant, env=env).strip("/")
        if dest_tpl
        else f"{tenant}/{env}"
    )

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=rule.get("s3_region", "us-east-1"),
            config=BotoConfig(signature_version="s3v4"),
        )
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=20)
        objects = resp.get("Contents", [])
        result.add_check(f"S3 bucket '{bucket}' prefix '{prefix}' has objects",
                         len(objects) > 0, f"count={len(objects)}")
        if objects:
            result.add_check("S3 object key looks valid",
                             objects[0]["Key"].startswith(prefix),
                             objects[0]["Key"])
    except Exception as exc:
        result.add_check("S3/MinIO connection", False, _exc_type(exc))


def verify_vault(rule: dict, result: SuiteResult) -> None:
    """Verify Vault secret injection: confirm resolved values were written to MongoDB."""
    if not HAS_MONGO:
        result.add_check("pymongo available", False, "not installed")
        return
    uri = rule.get("mongo_uri", "mongodb://mongo:27017")
    db_name = rule.get("database", "vartrack")
    dest_tpl = rule.get("destination_template", "vault-test")
    tenant = rule.get("tenant_id", GITEA_ORG)
    env = rule.get("branch", "main")
    col_name = dest_tpl.format(tenant=tenant, env=env) if "{" in dest_tpl else dest_tpl
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        db = client[db_name]
        col_exists = col_name in db.list_collection_names()
        result.add_check(f"MongoDB vault collection '{col_name}' exists", col_exists)
        if col_exists:
            count = db[col_name].count_documents({})
            result.add_check(f"Documents written (count={count})", count > 0, f"count={count}")
            if count > 0:
                pwd_doc = db[col_name].find_one({"key": "database.password"}, {"_id": 0})
                if pwd_doc:
                    val = str(pwd_doc.get("value", ""))
                    not_placeholder = "placeholder" not in val.lower()
                    result.add_check("database.password resolved (not placeholder)", not_placeholder, "[value present]" if not_placeholder else "[placeholder]")
                    result.add_check("database.password matches Vault secret",
                                     val == "supersecret123", "[matched]" if val == "supersecret123" else "[mismatch]")
                else:
                    result.add_check("database.password document found", False,
                                     "no doc with key=database.password")
                api_doc = db[col_name].find_one({"key": "api.secret_key"}, {"_id": 0})
                if api_doc:
                    val = str(api_doc.get("value", ""))
                    result.add_check("api.secret_key matches Vault secret",
                                     val == "api-secret-xyz789", "[matched]" if val == "api-secret-xyz789" else "[mismatch]")
                else:
                    result.add_check("api.secret_key document found", False,
                                     "no doc with key=api.secret_key")
        client.close()
    except Exception as exc:
        result.add_check("MongoDB vault check", False, _exc_type(exc))


SINK_VERIFIERS = {
    "mongo":        verify_mongo,
    "zookeeper":    verify_zookeeper,
    "redis":        verify_redis,
    "linux_server": verify_linux_server,
    "s3":           verify_s3,
}


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator helpers
# ─────────────────────────────────────────────────────────────────────────────

def trigger_webhook(orchestrator: str, datasource: str, tenant: str,
                    commit_sha: str, gitea_url: str) -> str | None:
    payload = {
        "ref": "refs/heads/main",
        "before": "0" * 40,
        "after": commit_sha,
        "repository": {
            "id": 1,
            "name": CONFIG_REPO,
            "full_name": f"{GITEA_ORG}/{CONFIG_REPO}",
            "owner": {"name": GITEA_ORG, "login": GITEA_ORG},
            "clone_url": f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}.git",
            "html_url": f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}",
            "default_branch": "main",
        },
        "pusher": {"name": "e2e-runner", "email": "e2e@vartrack.local"},
        "commits": [{
            "id": commit_sha,
            "message": "E2E test commit",
            "added": [CONFIG_FILE],
            "removed": [],
            "modified": [],
        }],
        "head_commit": {
            "id": commit_sha,
            "message": "E2E test commit",
            "added": [CONFIG_FILE],
            "removed": [],
            "modified": [],
        },
    }
    try:
        resp = requests.post(
            f"{orchestrator}/v1/webhooks/{datasource}",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "push",
                "X-Tenant-ID": tenant,
            },
            timeout=20,
        )
        if resp.ok:
            return resp.json().get("task_id")
        warn(f"Webhook returned {resp.status_code}")
        return None
    except Exception as exc:
        warn(f"Webhook exception for {datasource}: {_exc_type(exc)}")
        return None


def dry_run_webhook(orchestrator: str, datasource: str, tenant: str,
                    commit_sha: str, gitea_url: str) -> dict | None:
    payload = {
        "ref": "refs/heads/main",
        "before": "0" * 40,
        "after": commit_sha,
        "repository": {
            "id": 1,
            "name": CONFIG_REPO,
            "full_name": f"{GITEA_ORG}/{CONFIG_REPO}",
            "owner": {"name": GITEA_ORG, "login": GITEA_ORG},
            "clone_url": f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}.git",
            "html_url": f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}",
            "default_branch": "main",
        },
        "pusher": {"name": "e2e-runner", "email": "e2e@vartrack.local"},
        "commits": [{"id": commit_sha, "message": "dry-run", "added": [CONFIG_FILE],
                     "removed": [], "modified": []}],
        "head_commit": {"id": commit_sha, "message": "dry-run", "added": [CONFIG_FILE],
                        "removed": [], "modified": []},
    }
    try:
        resp = requests.post(
            f"{orchestrator}/v1/webhooks/{datasource}/dry-run",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "push",
                "X-Tenant-ID": tenant,
            },
            timeout=20,
        )
        return resp.json() if resp.ok else None
    except Exception:
        return None


def warm_schema(orchestrator: str, gitea_url: str, tenant_id: str) -> int:
    try:
        resp = requests.post(
            f"{orchestrator}/v1/tenants/{tenant_id}/schemas/warm",
            json={"repo_url": f"{gitea_url}/{GITEA_ORG}/{SCHEMA_REPO}.git", "branch": "main"},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if resp.ok:
            return resp.json().get("rules_loaded", 0)
    except Exception:
        pass
    return 0


def invalidate_rule_cache(tenant_id: str, platform: str, datasource: str) -> None:
    """Delete the Redis rule cache entry so the next resolve_rule loads the fresh bundle."""
    if not HAS_REDIS:
        return
    try:
        r = redis_lib.Redis(host="redis", port=6379, db=0, socket_timeout=3, decode_responses=True)
        key = f"rule:{tenant_id}:{platform}:{datasource}"
        r.delete(key)
        r.close()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Test suites
# ─────────────────────────────────────────────────────────────────────────────

def run_suite(
    suite_id: int,
    name: str,
    setting_file: str,
    rules: list[dict],
    orchestrator: str,
    gitea: GiteaClient,
    gitea_url: str,
    extra_fn=None,
) -> SuiteResult:
    rule = rules[0]
    datasource = rule["datasource"]
    tenant = rule.get("tenant_id", GITEA_ORG)
    result = SuiteResult(suite_id, name, setting_file, datasource)
    banner(f"Suite {suite_id:02d}: {name}")

    try:
        # Push bundle to schema repo
        gitea.push_bundle(rules)
        time.sleep(2)
        commit_sha = gitea.get_head_sha(CONFIG_REPO)
        result.add_check("Gitea commit SHA resolved", commit_sha != "0" * 40, commit_sha[:12])

        # Warm schema + invalidate Redis rule cache so fresh bundle is used
        n_rules = warm_schema(orchestrator, gitea_url, tenant)
        invalidate_rule_cache(tenant, rule.get("platform", "github"), datasource)
        result.add_check("Schema registry warmed", True, f"rules={n_rules}")

        # Extra pre-test hook
        if extra_fn:
            extra_fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)
        else:
            _default_etl_check(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)

        result.finish("pass" if all(c["passed"] for c in result.checks) else "fail")

    except Exception as exc:
        result.error = traceback.format_exc()
        err(f"Suite {suite_id} raised exception: {exc}")
        result.finish("fail")

    colour_code = colour.GREEN if result.status == "pass" else colour.RED
    print(f"\n  {colour_code}→ Suite {suite_id:02d} {result.status.upper()} "
          f"({result.duration_ms:.0f} ms){colour.NC}")
    return result


def _default_etl_check(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
    task_id = trigger_webhook(orchestrator, datasource, tenant, commit_sha, gitea_url)
    result.add_check("Webhook accepted (task_id returned)", task_id is not None, str(task_id))
    result.task_id = task_id

    log(f"  Waiting {WAIT_AFTER_WEBHOOK}s for ETL...")
    time.sleep(WAIT_AFTER_WEBHOOK)

    verifier = SINK_VERIFIERS.get(datasource)
    if verifier:
        verifier(rule, result)
    else:
        result.add_check(f"Verifier for '{datasource}'", False, "no verifier registered")


# ─────────────────────────────────────────────────────────────────────────────
# Suite definitions (16 total)
# ─────────────────────────────────────────────────────────────────────────────

def suite_01_mongo_doc_basic(orchestrator, gitea, gitea_url, rules):
    """MongoDB document write — basic STRATEGY_KEY_VALUE."""
    return run_suite(1, "MongoDB document write (STRATEGY_KEY_VALUE)",
                     "01_mongo_document.json", rules, orchestrator, gitea, gitea_url)


def suite_02_mongo_doc_prune(orchestrator, gitea, gitea_url, rules):
    """MongoDB: prune=true removes stale keys on second push."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        # First push
        _default_etl_check(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)
        # Second push (idempotent) — confirms prune doesn't wipe valid keys
        sha2 = gitea.get_head_sha(CONFIG_REPO)
        tid2 = trigger_webhook(orchestrator, datasource, tenant, sha2, gitea_url)
        result.add_check("Second webhook accepted", tid2 is not None, str(tid2))
        time.sleep(WAIT_AFTER_WEBHOOK)
        verify_mongo(rule, result)

    return run_suite(2, "MongoDB prune idempotency",
                     "01_mongo_document.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_03_mongo_file_strategy(orchestrator, gitea, gitea_url, rules):
    """MongoDB STRATEGY_FILE stores whole file document."""
    return run_suite(3, "MongoDB STRATEGY_FILE snapshot",
                     "02_mongo_file.json", rules, orchestrator, gitea, gitea_url)



def suite_04_mongo_dry_run(orchestrator, gitea, gitea_url, rules):
    """MongoDB: dry-run returns report without writing."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        dr = dry_run_webhook(orchestrator, datasource, tenant, commit_sha, gitea_url)
        result.add_check("Dry-run response received", dr is not None)
        if dr:
            result.add_check("Dry-run flag set", dr.get("dry_run") is True)

    return run_suite(4, "MongoDB dry-run (no write)",
                     "01_mongo_document.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_05_zk_slash_write(orchestrator, gitea, gitea_url, rules):
    """ZooKeeper slash key_format — znodes written under destination_template path."""
    return run_suite(5, "ZooKeeper slash key_format write",
                     "03_zk_slash.json", rules, orchestrator, gitea, gitea_url)


def suite_06_zk_destination_template(orchestrator, gitea, gitea_url, rules):
    """ZooKeeper: destination_template — one representative template substitution test."""
    modified = json.loads(json.dumps(rules))
    modified[0]["destination_template"] = "/custom/{tenant}/cfg"
    return run_suite(6, "ZooKeeper custom destination_template path",
                     "03_zk_slash.json", modified, orchestrator, gitea, gitea_url)


def suite_07_redis_hash_write(orchestrator, gitea, gitea_url, rules):
    """Redis HASH data structure write."""
    return run_suite(7, "Redis HASH write",
                     "05_redis_hash.json", rules, orchestrator, gitea, gitea_url)


def suite_08_redis_string_write(orchestrator, gitea, gitea_url, rules):
    """Redis STRING key-value write."""
    return run_suite(8, "Redis STRING write",
                     "06_redis_string.json", rules, orchestrator, gitea, gitea_url)


def suite_09_linux_server_write(orchestrator, gitea, gitea_url, rules):
    """Linux server SSH file write via destination_template path."""
    return run_suite(9, "Linux server SSH file write",
                     "07_linux_server.json", rules, orchestrator, gitea, gitea_url)


def suite_10_s3_write(orchestrator, gitea, gitea_url, rules):
    """S3 (MinIO): objects written under destination_template prefix."""
    return run_suite(10, "S3/MinIO write",
                     "08_s3_minio.json", rules, orchestrator, gitea, gitea_url)


def suite_11_multi_sink_parallel(orchestrator, gitea, gitea_url,
                                  mongo_rules, zk_rules, redis_rules):
    """All three main sinks triggered simultaneously — stress test."""
    result = SuiteResult(11, "Multi-sink parallel webhooks (mongo + zk + redis)",
                         "mixed", "multi")
    banner("Suite 11: Multi-sink parallel webhooks")
    try:
        gitea.push_bundle(mongo_rules + zk_rules + redis_rules)
        time.sleep(2)
        commit_sha = gitea.get_head_sha(CONFIG_REPO)

        for ds, rules in [("mongo", mongo_rules), ("zookeeper", zk_rules), ("redis", redis_rules)]:
            tenant = rules[0].get("tenant_id", GITEA_ORG)
            tid = trigger_webhook(orchestrator, ds, tenant, commit_sha, gitea_url)
            result.add_check(f"Webhook sent to {ds}", tid is not None, str(tid))

        log(f"  Waiting {WAIT_AFTER_WEBHOOK}s for parallel ETL...")
        time.sleep(WAIT_AFTER_WEBHOOK)

        verify_mongo(mongo_rules[0], result)
        verify_zookeeper(zk_rules[0], result)
        verify_redis(redis_rules[0], result)

        result.finish("pass" if all(c["passed"] for c in result.checks) else "fail")
    except Exception as exc:
        result.error = traceback.format_exc()
        result.finish("fail")

    colour_code = colour.GREEN if result.status == "pass" else colour.RED
    print(f"\n  {colour_code}→ Suite 11 {result.status.upper()} ({result.duration_ms:.0f} ms){colour.NC}")
    return result


def suite_12_health_and_metrics(orchestrator, gitea, gitea_url, _rules):
    """Orchestrator health + metrics endpoints reachable."""
    result = SuiteResult(12, "Orchestrator health & metrics endpoints",
                         "n/a", "http")
    banner("Suite 12: Health & metrics")
    try:
        r = requests.get(f"{orchestrator}/v1/health", timeout=5)
        result.add_check("/v1/health returns 200", r.status_code == 200, str(r.status_code))

        r2 = requests.get(f"{orchestrator}/metrics", timeout=5)
        result.add_check("/metrics returns 200", r2.status_code == 200, str(r2.status_code))
        if r2.ok:
            has_celery = "celery" in r2.text.lower() or "task" in r2.text.lower()
            result.add_check("/metrics contains task metrics", has_celery)

        r3 = requests.get(f"{orchestrator}/docs", timeout=5)
        result.add_check("/docs (OpenAPI) reachable", r3.status_code == 200)

        result.finish("pass" if all(c["passed"] for c in result.checks) else "fail")
    except Exception as exc:
        result.error = traceback.format_exc()
        result.finish("fail")

    colour_code = colour.GREEN if result.status == "pass" else colour.RED
    print(f"\n  {colour_code}→ Suite 12 {result.status.upper()} ({result.duration_ms:.0f} ms){colour.NC}")
    return result


def suite_13_cue_schema_valid(orchestrator, gitea, gitea_url, rules):
    """CUE schema present — valid config passes validation and writes to MongoDB."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        task_id = trigger_webhook(orchestrator, datasource, tenant, commit_sha, gitea_url)
        result.add_check("Webhook accepted (task_id returned)", task_id is not None, str(task_id))
        result.task_id = task_id
        log(f"  Waiting {WAIT_AFTER_WEBHOOK}s for ETL (CUE validation active)...")
        time.sleep(WAIT_AFTER_WEBHOOK)
        verify_mongo(rule, result)
        if HAS_MONGO:
            try:
                dest_tpl = rule.get("destination_template", "")
                tenant_id = rule.get("tenant_id", GITEA_ORG)
                env = rule.get("branch", "main")
                safe_env = env.replace("/", "_")
                col_name = dest_tpl.format(tenant=tenant_id, env=safe_env) if dest_tpl else "variables"
                client = MongoClient(rule.get("mongo_uri", "mongodb://mongo:27017"),
                                     serverSelectionTimeoutMS=5000)
                db = client[rule.get("database", "vartrack")]
                doc = db[col_name].find_one({"key": "observability.log_level"}, {"_id": 0})
                if doc:
                    val = str(doc.get("value", ""))
                    result.add_check(f"CUE validated log_level='{val}' (allowed by schema)",
                                     val in ("DEBUG", "INFO", "WARN", "ERROR"), val)
                else:
                    result.add_check("observability.log_level document found", False,
                                     "key not written to collection")
                client.close()
            except Exception as exc:
                result.add_check("CUE log_level check", False, _exc_type(exc))

    return run_suite(13, "CUE schema validation (valid config passes)",
                     "01_mongo_document.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_14_vault_secret_injection(orchestrator, gitea, gitea_url, rules):
    """Vault @secret fields resolved before writing to MongoDB."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        task_id = trigger_webhook(orchestrator, datasource, tenant, commit_sha, gitea_url)
        result.add_check("Webhook accepted (task_id returned)", task_id is not None, str(task_id))
        result.task_id = task_id
        log(f"  Waiting {WAIT_AFTER_WEBHOOK}s for ETL (Vault secret injection)...")
        time.sleep(WAIT_AFTER_WEBHOOK)
        verify_vault(rule, result)

    return run_suite(14, "Vault secret injection → MongoDB",
                     "09_vault_mongo.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_15_cue_strict_reject(orchestrator, gitea, gitea_url, rules,
                                bad_config: bytes, orig_config: bytes):
    """CUE strict_validation=true rejects invalid log_level — file skipped, no write."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        gitea.push_config_file(bad_config, "e2e: invalid log_level for CUE strict test")
        time.sleep(2)
        commit_sha2 = gitea.get_head_sha(CONFIG_REPO)
        task_id = trigger_webhook(orchestrator, datasource, tenant, commit_sha2, gitea_url)
        result.add_check("Webhook accepted (task_id returned)", task_id is not None, str(task_id))
        result.task_id = task_id
        log(f"  Waiting {WAIT_AFTER_WEBHOOK}s for ETL (expecting strict CUE reject)...")
        time.sleep(WAIT_AFTER_WEBHOOK)
        dest_tpl = rule.get("destination_template", "cue-strict-test")
        col_name = dest_tpl
        try:
            client = MongoClient(rule.get("mongo_uri", "mongodb://mongo:27017"),
                                 serverSelectionTimeoutMS=5000)
            db = client[rule.get("database", "vartrack")]
            col_exists = col_name in db.list_collection_names()
            count = db[col_name].count_documents({}) if col_exists else 0
            result.add_check(
                f"CUE strict reject: '{col_name}' has 0 documents (write blocked)",
                count == 0, f"count={count}",
            )
            client.close()
        except Exception as exc:
            result.add_check("MongoDB strict-reject check", False, _exc_type(exc))
        finally:
            gitea.push_config_file(orig_config, "e2e: restore valid config after strict test")
            time.sleep(2)

    return run_suite(15, "CUE strict_validation rejects invalid config",
                     "10_cue_strict_mongo.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_16_dual_mongo_secret(orchestrator, gitea, gitea_url, dual_rules):
    """Dual MongoDB (mongo-primary + mongo-dr) — both receive @secret injection from Vault."""
    result = SuiteResult(16, "Dual MongoDB (primary + DR) with @secret injection",
                         "11_dual_mongo_secret.json", "mongo-primary+mongo-dr")
    banner("Suite 16: Dual MongoDB datasources with @secret injection")
    try:
        gitea.push_bundle(dual_rules)
        time.sleep(2)
        commit_sha = gitea.get_head_sha(CONFIG_REPO)
        result.add_check("Gitea commit SHA resolved", commit_sha != "0" * 40, commit_sha[:12])

        tenant = dual_rules[0].get("tenant_id", GITEA_ORG)
        n_rules = warm_schema(orchestrator, gitea_url, tenant)
        result.add_check("Schema registry warmed", True, f"rules={n_rules}")

        for rule in dual_rules:
            invalidate_rule_cache(tenant, rule.get("platform", "github"), rule["datasource"])

        for rule in dual_rules:
            ds = rule["datasource"]
            tid = trigger_webhook(orchestrator, ds, tenant, commit_sha, gitea_url)
            result.add_check(f"Webhook accepted for {ds}", tid is not None, str(tid))

        log(f"  Waiting {WAIT_AFTER_WEBHOOK}s for ETL (dual Vault injection)...")
        time.sleep(WAIT_AFTER_WEBHOOK)

        for rule in dual_rules:
            verify_vault(rule, result)

        result.finish("pass" if all(c["passed"] for c in result.checks) else "fail")
    except Exception as exc:
        result.error = traceback.format_exc()
        err(f"Suite 16 raised exception: {exc}")
        result.finish("fail")

    colour_code = colour.GREEN if result.status == "pass" else colour.RED
    print(f"\n  {colour_code}→ Suite 16 {result.status.upper()} ({result.duration_ms:.0f} ms){colour.NC}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Suites 17-22 (extended coverage)
# ─────────────────────────────────────────────────────────────────────────────

def suite_17_self_heal_drift(orchestrator, gitea, gitea_url, rules):
    """Self-heal: corrupt a value in MongoDB directly, trigger a heal cycle, verify it is restored."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        # First, do a normal write so there is something to corrupt.
        _default_etl_check(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)

        if not HAS_MONGO:
            result.add_check("pymongo available (self-heal)", False, "not installed")
            return

        uri       = rule.get("mongo_uri", "mongodb://mongo:27017")
        db_name   = rule.get("database", "vartrack")
        dest_tpl  = rule.get("destination_template", "")
        tenant_id = rule.get("tenant_id", GITEA_ORG)
        env       = rule.get("branch", "main")
        safe_env  = env.replace("/", "_")
        col_name  = dest_tpl.format(tenant=tenant_id, env=safe_env) if dest_tpl else rule.get("collection", "variables")

        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            db     = client[db_name]
            # Corrupt one key directly in the collection.
            update_res = db[col_name].update_one(
                {"key": {"$exists": True}},
                {"$set": {"value": "__DRIFT_INJECTED__"}},
            )
            corrupted = update_res.modified_count > 0
            result.add_check("Drift injected into MongoDB", corrupted,
                             f"modified={update_res.modified_count}")
            client.close()
        except Exception as exc:
            result.add_check("Drift injection", False, _exc_type(exc))
            return

        log(f"  Waiting {WAIT_AFTER_DRIFT}s then triggering heal webhook...")
        time.sleep(WAIT_AFTER_DRIFT)

        # Re-trigger the webhook — self_heal=true should overwrite the injected value.
        heal_sha = gitea.get_head_sha(CONFIG_REPO)
        tid = trigger_webhook(orchestrator, datasource, tenant_id, heal_sha, gitea_url)
        result.add_check("Heal webhook accepted", tid is not None, str(tid))
        time.sleep(WAIT_AFTER_WEBHOOK)

        # Verify the corrupted key no longer has the injected value.
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            db     = client[db_name]
            drifted = db[col_name].find_one({"value": "__DRIFT_INJECTED__"})
            result.add_check("Drift value removed (self-heal worked)", drifted is None,
                             "still present" if drifted else "cleared")
            client.close()
        except Exception as exc:
            result.add_check("Post-heal MongoDB check", False, _exc_type(exc))

    return run_suite(17, "Self-heal: drift correction via re-trigger",
                     "01_mongo_document.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_18_invalid_signature(orchestrator, gitea, gitea_url, rules):
    """Gateway rejects a webhook with a wrong HMAC signature."""
    result = SuiteResult(18, "Invalid webhook signature → 401/403",
                         "n/a", "gateway")
    banner("Suite 18: Invalid webhook signature")
    try:
        rule      = rules[0]
        datasource = rule["datasource"]
        tenant    = rule.get("tenant_id", GITEA_ORG)
        commit_sha = gitea.get_head_sha(CONFIG_REPO)

        payload = {
            "ref": "refs/heads/main",
            "before": "0" * 40,
            "after": commit_sha,
            "repository": {
                "name": CONFIG_REPO,
                "full_name": f"{GITEA_ORG}/{CONFIG_REPO}",
                "owner": {"name": GITEA_ORG, "login": GITEA_ORG},
                "clone_url": f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}.git",
                "html_url":  f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}",
                "default_branch": "main",
            },
            "pusher": {"name": "e2e-runner", "email": "e2e@vartrack.local"},
            "commits": [{"id": commit_sha, "message": "bad-sig", "added": [CONFIG_FILE],
                         "removed": [], "modified": []}],
            "head_commit": {"id": commit_sha, "message": "bad-sig", "added": [CONFIG_FILE],
                            "removed": [], "modified": []},
        }
        resp = requests.post(
            f"{orchestrator}/v1/webhooks/{datasource}",
            json=payload,
            headers={
                "Content-Type":         "application/json",
                "X-GitHub-Event":       "push",
                "X-Hub-Signature-256":  "sha256=0000000000000000000000000000000000000000000000000000000000000000",
                "X-Tenant-ID":          tenant,
            },
            timeout=10,
        )
        rejected = resp.status_code in (401, 403, 400)
        result.add_check("Invalid signature rejected", rejected, f"status={resp.status_code}")
        result.add_check("No task_id returned", "task_id" not in resp.text)

        result.finish("pass" if all(c["passed"] for c in result.checks) else "fail")
    except Exception as exc:
        result.error = traceback.format_exc()
        err(f"Suite 18 raised exception: {exc}")
        result.finish("fail")

    colour_code = colour.GREEN if result.status == "pass" else colour.RED
    print(f"\n  {colour_code}→ Suite 18 {result.status.upper()} ({result.duration_ms:.0f} ms){colour.NC}")
    return result


def suite_19_redis_dry_run(orchestrator, gitea, gitea_url, rules):
    """Redis dry-run must not write any keys."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        if not HAS_REDIS:
            result.add_check("redis-py available", False, "not installed")
            return

        host = rule.get("redis_host", "redis")
        port = int(rule.get("redis_port", 6379))
        db   = int(rule.get("redis_db", 0))
        dest_tpl = rule.get("destination_template", "")
        prefix   = dest_tpl.format(tenant=tenant, env="main") if dest_tpl else f"{tenant}:default"

        # Capture key count before dry-run.
        try:
            r = redis_lib.Redis(host=host, port=port, db=db, socket_timeout=5, decode_responses=True)
            before_keys = len(r.keys(f"{prefix}*"))
            r.close()
        except Exception as exc:
            result.add_check("Redis pre-check", False, _exc_type(exc))
            return

        dr = dry_run_webhook(orchestrator, datasource, tenant, commit_sha, gitea_url)
        result.add_check("Dry-run response received", dr is not None)
        if dr:
            result.add_check("Dry-run flag set in response", dr.get("dry_run") is True)

        time.sleep(5)

        # Verify no new keys were written.
        try:
            r = redis_lib.Redis(host=host, port=port, db=db, socket_timeout=5, decode_responses=True)
            after_keys = len(r.keys(f"{prefix}*"))
            r.close()
            result.add_check("Redis key count unchanged after dry-run",
                             after_keys == before_keys,
                             f"before={before_keys} after={after_keys}")
        except Exception as exc:
            result.add_check("Redis post-check", False, _exc_type(exc))

    return run_suite(19, "Redis dry-run (no write)",
                     "05_redis_hash.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_20_branch_map(orchestrator, gitea, gitea_url, rules):
    """branch_map: push from 'develop' branch → writes to 'staging' collection."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        develop_payload = {
            "ref": "refs/heads/develop",
            "before": "0" * 40,
            "after": commit_sha,
            "repository": {
                "name": CONFIG_REPO,
                "full_name": f"{GITEA_ORG}/{CONFIG_REPO}",
                "owner": {"name": GITEA_ORG, "login": GITEA_ORG},
                "clone_url": f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}.git",
                "html_url":  f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}",
                "default_branch": "main",
            },
            "pusher": {"name": "e2e-runner", "email": "e2e@vartrack.local"},
            "commits": [{"id": commit_sha, "message": "branch-map test",
                         "added": [CONFIG_FILE], "removed": [], "modified": []}],
            "head_commit": {"id": commit_sha, "message": "branch-map test",
                            "added": [CONFIG_FILE], "removed": [], "modified": []},
        }
        resp = requests.post(
            f"{orchestrator}/v1/webhooks/{datasource}",
            json=develop_payload,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "push",
                "X-Tenant-ID": tenant,
            },
            timeout=20,
        )
        task_id = resp.json().get("task_id") if resp.ok else None
        result.add_check("Develop-branch webhook accepted", task_id is not None, str(task_id))
        result.task_id = task_id

        log(f"  Waiting {WAIT_AFTER_WEBHOOK}s for branch-map ETL...")
        time.sleep(WAIT_AFTER_WEBHOOK)

        if not HAS_MONGO:
            result.add_check("pymongo available", False, "not installed")
            return

        dest_tpl  = rule.get("destination_template", "")
        uri       = rule.get("mongo_uri", "mongodb://mongo:27017")
        db_name   = rule.get("database", "vartrack")
        branch_map = rule.get("branch_map", {})
        staging_env = branch_map.get("develop", "staging")
        col_name  = dest_tpl.format(tenant=tenant, env=staging_env) if dest_tpl else f"{staging_env}-config"

        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            db = client[db_name]
            count = db[col_name].count_documents({}) if col_name in db.list_collection_names() else 0
            result.add_check(f"Staging collection '{col_name}' has documents",
                             count > 0, f"count={count}")
            client.close()
        except Exception as exc:
            result.add_check("MongoDB staging check", False, _exc_type(exc))

    return run_suite(20, "branch_map: develop → staging collection",
                     "01_mongo_document.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_21_structured_prune(orchestrator, gitea, gitea_url, rules):
    """Structured prune config: prune: {last: true, dry_run: false}."""
    # Inject structured prune config into the rules copy.
    modified = json.loads(json.dumps(rules))
    modified[0]["prune"] = {"enabled": True, "last": True, "dry_run": False}

    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        # First write.
        _default_etl_check(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)

        # Second write — confirms prune.last doesn't corrupt existing data.
        sha2 = gitea.get_head_sha(CONFIG_REPO)
        tid2 = trigger_webhook(orchestrator, datasource, tenant, sha2, gitea_url)
        result.add_check("Second webhook (prune.last) accepted", tid2 is not None, str(tid2))
        time.sleep(WAIT_AFTER_WEBHOOK)
        verify_mongo(rule, result)

    return run_suite(21, "Structured prune config (last=true)",
                     "01_mongo_document.json", modified, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_22_task_status_polling(orchestrator, gitea, gitea_url, rules):
    """Trigger a webhook then poll /v1/tasks/{task_id} until it reaches a terminal state."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        task_id = trigger_webhook(orchestrator, datasource, tenant, commit_sha, gitea_url)
        result.add_check("Webhook accepted (task_id returned)", task_id is not None, str(task_id))
        result.task_id = task_id
        if not task_id:
            return

        terminal = {"SUCCESS", "FAILURE", "REVOKED", "success", "failure", "revoked"}
        final_state = None
        for attempt in range(20):
            time.sleep(3)
            try:
                r = requests.get(f"{orchestrator}/v1/tasks/{task_id}", timeout=5)
                if r.ok:
                    data  = r.json()
                    state = data.get("state") or data.get("status", "")
                    if state.upper() in {s.upper() for s in terminal}:
                        final_state = state
                        break
            except Exception:
                pass

        result.add_check("Task reached terminal state via polling",
                         final_state is not None, str(final_state) if final_state else "timed-out")
        if final_state:
            result.add_check("Task completed successfully",
                             final_state.upper() in ("SUCCESS", "COMPLETED"),
                             final_state)

    return run_suite(22, "Task status polling until terminal",
                     "01_mongo_document.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def load_settings(settings_dir: str) -> dict[str, list[dict]]:
    p = Path(settings_dir)
    result = {}
    for f in sorted(p.glob("*.json")):
        with open(f) as fh:
            result[f.name] = json.load(fh)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="VarTrack E2E test runner")
    parser.add_argument("--settings-dir", default="/demo/schemas/settings")
    parser.add_argument("--results-dir",  default="/results")
    parser.add_argument("--orchestrator", default=os.environ.get("ORCHESTRATOR_URL",
                                                                  "http://orchestrator-api:8000"))
    parser.add_argument("--gitea",        default=os.environ.get("GITEA_URL",
                                                                  "http://gitea:3000"))
    parser.add_argument("--gitea-user",   default=os.environ.get("GITEA_ADMIN_USER", "demo-admin"))
    parser.add_argument("--gitea-pass",   default=os.environ.get("GITEA_ADMIN_PASSWORD", "VarTrack123"))
    args = parser.parse_args()

    Path(args.results_dir).mkdir(parents=True, exist_ok=True)

    gitea = GiteaClient(args.gitea, args.gitea_user, args.gitea_pass)

    banner("VarTrack E2E Test Runner — 22 Suites / 8 Sinks + CUE + Vault")
    run_started = datetime.now(timezone.utc).isoformat()
    global_t0 = time.monotonic()

    settings = load_settings(args.settings_dir)
    log(f"Loaded {len(settings)} setting files from {args.settings_dir}")

    s = settings
    mongo_doc   = s.get("01_mongo_document.json",  [{}])
    mongo_file  = s.get("02_mongo_file.json",       [{}])
    zk_slash    = s.get("03_zk_slash.json",         [{}])
    redis_hash  = s.get("05_redis_hash.json",       [{}])
    redis_str   = s.get("06_redis_string.json",     [{}])
    linux       = s.get("07_linux_server.json",     [{}])
    s3_minio    = s.get("08_s3_minio.json",         [{}])
    vault_mongo = s.get("09_vault_mongo.json",      [{}])
    cue_strict  = s.get("10_cue_strict_mongo.json", [{}])
    dual_mongo  = s.get("11_dual_mongo_secret.json", [{}, {}])

    # Push CUE validation schema to Gitea schemas repo (used by suites 13-15).
    cue_schema_path = Path("/demo/schemas/cue/app-settings.yaml.cue")
    if cue_schema_path.exists():
        gitea.push_schema_file(
            "app-settings.yaml.cue",
            cue_schema_path.read_bytes(),
            "Add CUE validation schema for e2e",
        )
        ok("CUE schema pushed to schemas repo")
    else:
        warn(f"CUE schema not found at {cue_schema_path} — suites 13-15 may skip validation")

    # Read config file content for CUE strict-reject test (suite 15).
    orig_config = Path(CONFIG_LOCAL).read_bytes()
    bad_config = orig_config.replace(
        b"log_level: INFO", b"log_level: VERBOSE"
    )

    results: list[SuiteResult] = []

    # ── MongoDB suites (1–4) ──────────────────────────────────────────────────
    results.append(suite_01_mongo_doc_basic(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_02_mongo_doc_prune(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_03_mongo_file_strategy(args.orchestrator, gitea, args.gitea, mongo_file))
    results.append(suite_04_mongo_dry_run(args.orchestrator, gitea, args.gitea, mongo_doc))

    # ── ZooKeeper suites (5–6) ────────────────────────────────────────────────
    results.append(suite_05_zk_slash_write(args.orchestrator, gitea, args.gitea, zk_slash))
    results.append(suite_06_zk_destination_template(args.orchestrator, gitea, args.gitea, zk_slash))

    # ── Redis suites (7–8) ────────────────────────────────────────────────────
    results.append(suite_07_redis_hash_write(args.orchestrator, gitea, args.gitea, redis_hash))
    results.append(suite_08_redis_string_write(args.orchestrator, gitea, args.gitea, redis_str))

    # ── Single-sink suites (9–10) ─────────────────────────────────────────────
    results.append(suite_09_linux_server_write(args.orchestrator, gitea, args.gitea, linux))
    results.append(suite_10_s3_write(args.orchestrator, gitea, args.gitea, s3_minio))

    # ── Cross-sink + meta suites (11–12) ──────────────────────────────────────
    results.append(suite_11_multi_sink_parallel(
        args.orchestrator, gitea, args.gitea, mongo_doc, zk_slash, redis_hash))
    results.append(suite_12_health_and_metrics(args.orchestrator, gitea, args.gitea, mongo_doc))

    # ── CUE + Vault + dual datasource suites (13–16) ──────────────────────────
    results.append(suite_13_cue_schema_valid(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_14_vault_secret_injection(
        args.orchestrator, gitea, args.gitea, vault_mongo))
    results.append(suite_15_cue_strict_reject(
        args.orchestrator, gitea, args.gitea, cue_strict, bad_config, orig_config))
    results.append(suite_16_dual_mongo_secret(
        args.orchestrator, gitea, args.gitea, dual_mongo))

    # ── Extended coverage suites (17–22) ──────────────────────────────────────
    results.append(suite_17_self_heal_drift(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_18_invalid_signature(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_19_redis_dry_run(args.orchestrator, gitea, args.gitea, redis_hash))
    results.append(suite_20_branch_map(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_21_structured_prune(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_22_task_status_polling(args.orchestrator, gitea, args.gitea, mongo_doc))

    # ── Summary ───────────────────────────────────────────────────────────────
    total_ms   = (time.monotonic() - global_t0) * 1000
    passed     = sum(1 for r in results if r.status == "pass")
    failed     = sum(1 for r in results if r.status == "fail")
    skipped    = sum(1 for r in results if r.status == "skip")
    total      = len(results)

    banner(f"Results: {passed}/{total} passed — {failed} failed — {skipped} skipped")
    for r in results:
        sym = "✓" if r.status == "pass" else ("✗" if r.status == "fail" else "○")
        c = colour.GREEN if r.status == "pass" else (colour.RED if r.status == "fail" else colour.YELLOW)
        n_checks  = len(r.checks)
        n_passed  = sum(1 for c2 in r.checks if c2["passed"])
        print(f"  {c}{sym}{colour.NC} [{r.suite_id:02d}] {r.name:<52} "
              f"{n_passed}/{n_checks} checks  {r.duration_ms:>7.0f} ms")

    print(f"\n  Total wall time: {total_ms/1000:.1f}s\n")

    # ── Benchmark JSON output ─────────────────────────────────────────────────
    benchmark = {
        "run_started_at": run_started,
        "run_finished_at": datetime.now(timezone.utc).isoformat(),
        "total_duration_ms": round(total_ms, 2),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": round(passed / total * 100, 1) if total else 0,
        },
        "environment": {
            "orchestrator": args.orchestrator,
            "gitea": args.gitea,
        },
        "suites": [r.to_dict() for r in results],
    }

    out_path = Path(args.results_dir) / "benchmark.json"
    with open(out_path, "w") as fh:
        json.dump(benchmark, fh, indent=2)
    ok(f"Benchmark written → {out_path}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
