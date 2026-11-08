#!/usr/bin/env python3
"""
VarTrack E2E Test Runner
========================
Runs 22 test suites across 8 sink configurations (MongoDB document,
MongoDB file, ZooKeeper slash, Redis hash, Redis string, Linux server,
S3/MinIO) plus CUE schema validation, HashiCorp Vault secret injection,
dual-datasource @secret suites, self-heal drift correction, forged and unsigned
calls refused, dry-run verification, branch_map, structured prune, and
task status polling.

Every suite waits for the task it started (GET /v1/tasks/{id}), never for the clock, and asserts what the
task itself reports: its state, the files it synced, and the commit of the tenant's schema repository it
validated against. A suite that cannot pass is a bug to fix, not to retry: the run exits 1 when any suite
fails, and CI reads benchmark.json (23 suites, 0 failed, 0 skipped).

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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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
DEMO_TENANT = "demo"          # the tenant config.cue declares: the demo's repositories are its own
CONFIG_REPO = "demo-configs"
SCHEMA_REPO = "schemas"
CONFIG_FILE = "app-settings.yaml"
CONFIG_LOCAL = "/demo/configs/app-settings.yaml"
# The ceiling on how long a suite waits for a task it started. A task finishes in well under a second;
# this only turns a hung one into a failed run. E2E_TIMEOUT (seconds) overrides it.
E2E_TIMEOUT = max(1, min(int(os.environ.get("E2E_TIMEOUT", "60")), 300))


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
    """Strip credential-like patterns from a string before printing: URL passwords, key=value secrets,
    every bearer grant and the demo's own INTERNAL_AUTH_SECRET."""
    import grant
    text = str(text)
    text = _REDACT_URL_CREDS.sub(r'\1***\2', text)
    text = _REDACT_KV.sub(r'\1***', text)
    return grant.redact(text)

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

    def push_file(self, repo: str, filepath: str, content: bytes, message: str) -> str:
        """Create or update *filepath* on main and return the commit the branch is at afterwards. A file
        that already holds exactly *content* is not committed again; the branch's head is then the answer.
        Raises when Gitea refuses: a suite must not go on as if its push had landed."""
        path = f"/repos/{GITEA_ORG}/{repo}/contents/{filepath}"
        b64 = base64.b64encode(content).decode()
        existing = self._api("GET", path)
        if existing.ok:
            if existing.json().get("content", "").replace("\n", "") == b64:
                return self.get_head_sha(repo)
            resp = self._api("PUT", path, json={"message": message, "content": b64, "sha": existing.json()["sha"]})
        else:
            resp = self._api("POST", path, json={"message": message, "content": b64})
        if not resp.ok:
            raise RuntimeError(f"could not push {repo}/{filepath}: {resp.status_code} {resp.text[:160]}")
        return resp.json()["commit"]["sha"]

    def get_head_sha(self, repo: str, branch: str = "main") -> str:
        """The commit *branch* is at (the first ref of the repository is not necessarily main's)."""
        r = self._api("GET", f"/repos/{GITEA_ORG}/{repo}/branches/{branch}")
        if r.ok:
            return r.json()["commit"]["id"]
        return "0" * 40

    def ensure_branch(self, repo: str, branch: str, from_branch: str = "main") -> None:
        """Create *branch* in the config repo from *from_branch*; fine if it is there already."""
        resp = self._api("POST", f"/repos/{GITEA_ORG}/{repo}/branches",
                         json={"new_branch_name": branch, "old_branch_name": from_branch})
        if resp.status_code not in (201, 409):
            raise RuntimeError(f"could not create branch {branch}: {resp.status_code} {resp.text[:120]}")

    def push_bundle(self, rules: list[dict]) -> str:
        """Put *rules* in the tenant's schema repository as bundle.json; returns the commit they are at."""
        content = json.dumps(rules, indent=2).encode()
        return self.push_file(SCHEMA_REPO, "bundle.json", content, "Update bundle.json for test suite")

    def push_schema_file(self, filepath: str, content: bytes, message: str) -> str:
        """Push an arbitrary file to the schema repo (e.g. a .cue validation schema)."""
        return self.push_file(SCHEMA_REPO, filepath, content, message)

    def push_config_file(self, content: bytes, message: str) -> str:
        """Push app-settings.yaml to the config repo (used for CUE rejection tests)."""
        return self.push_file(CONFIG_REPO, CONFIG_FILE, content, message)


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
        # The commit of the schema repository the suite's rules were pushed at: every task of the suite
        # has to report having validated against it.
        self.schema_commit: str = ""

    def add_check(self, name: str, passed: bool, detail: str = "") -> None:
        at = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]   # UTC, like the services' logs
        self.checks.append({"name": name, "passed": passed, "detail": _redact(detail) if detail else "", "at": at})
        sym = "✓" if passed else "✗"
        colour_code = colour.GREEN if passed else colour.RED
        print(f"    {at} {colour_code}{sym}{colour.NC} {_redact(name)}" + (f" — {_redact(detail)}" if detail else ""))

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
            "schema_commit": self.schema_commit,
            "error": self.error,
            "checks": self.checks,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Sink verifiers
# ─────────────────────────────────────────────────────────────────────────────

_ABSENT = object()


def mongo_value(db, col_name: str, key: str):
    """(found, value) of one config key, wherever the Mongo sink keeps it: a document of its own (STRATEGY_KEY_VALUE:
    {key, value}), or nested under `data` in the environment's document (STRATEGY_DOCUMENT and STRATEGY_FILE, where
    "database.password" is data.database.password)."""
    doc = db[col_name].find_one({"key": key}, {"_id": 0})
    if doc is not None:
        return True, doc.get("value")
    for document in db[col_name].find({"data": {"$exists": True}}, {"_id": 0, "data": 1}):
        current = document.get("data")
        for part in key.split("."):
            current = current.get(part, _ABSENT) if isinstance(current, dict) else _ABSENT
        if current is not _ABSENT:
            return True, current
    return False, None


def verify_mongo(rule: dict, result: SuiteResult) -> None:
    if not HAS_MONGO:
        result.add_check("pymongo available", False, "not installed")
        return
    uri = rule.get("mongo_uri", "mongodb://mongo:27017")
    db_name = rule.get("database", "vartrack")
    dest_tpl = rule.get("destination_template", "")
    env = rule.get("branch", "main") if not rule.get("branch_as_env") else rule.get("branch", "main")
    tenant = rule.get("tenant_id", DEMO_TENANT)
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


def verify_mongo_per_file(rule: dict, result: SuiteResult) -> None:
    """A Mongo datasource on STRATEGY_DOCUMENT or STRATEGY_FILE: one document per
    synced file, the file under "data" - and no document per key, which is what
    STRATEGY_KEY_VALUE writes. (A datasource's update_strategy is set in
    config.cue only: a rule asking for another one is ignored, so these suites
    name datasources of their own.)"""
    if not HAS_MONGO:
        result.add_check("pymongo available", False, "not installed")
        return
    uri = rule.get("mongo_uri", "mongodb://mongo:27017")
    db_name = rule.get("database", "vartrack")
    tenant = rule.get("tenant_id", DEMO_TENANT)
    env = rule.get("branch", "main").replace("/", "_")
    col_name = rule["destination_template"].format(tenant=tenant, env=env)
    strategy = rule.get("update_strategy", "")
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        col = client[db_name][col_name]
        exists = col_name in client[db_name].list_collection_names()
        result.add_check(f"MongoDB collection '{col_name}' exists", exists)
        docs = list(col.find({}, {"_id": 0})) if exists else []
        result.add_check(f"{strategy}: one document for the one synced file (count={len(docs)})",
                         len(docs) == 1, f"count={len(docs)}")
        result.add_check(f"{strategy}: no document per key",
                         not any("key" in d for d in docs), "found a per-key document" if any("key" in d for d in docs) else "")
        if len(docs) == 1:
            doc = docs[0]
            data = doc.get("data")
            result.add_check("document names the file it holds", doc.get("_vt_file") == CONFIG_FILE,
                             str(doc.get("_vt_file")))
            result.add_check("document belongs to the datasource", doc.get("_vt_datasource") == rule["datasource"],
                             str(doc.get("_vt_datasource")))
            result.add_check('"data" is the file as a tree (database.pool_size is nested)',
                             isinstance(data, dict) and isinstance(data.get("database"), dict)
                             and data["database"].get("pool_size") == "10",
                             str(list(data)[:4]) if isinstance(data, dict) else type(data).__name__)
        client.close()
    except Exception as exc:
        result.add_check("MongoDB connection", False, _exc_type(exc))


def verify_zookeeper(rule: dict, result: SuiteResult) -> None:
    if not HAS_ZK:
        result.add_check("kazoo available", False, "not installed")
        return
    hosts = ",".join(rule.get("zk_hosts", ["zookeeper:2181"]))
    dest_tpl = rule.get("destination_template", "")
    tenant = rule.get("tenant_id", DEMO_TENANT)
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
    tenant = rule.get("tenant_id", DEMO_TENANT)
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


def _ssh_client(rule: dict):
    """An SSH connection to the demo's linux target (host key learned on first contact and then checked)."""
    host = os.environ.get("LINUX_TARGET_HOST", rule.get("ssh_host", "linux-target"))
    port = int(os.environ.get("LINUX_TARGET_PORT", rule.get("ssh_port", 22)))
    user = os.environ.get("LINUX_TARGET_USER", rule.get("ssh_user", "vartrack"))
    password = os.environ.get("LINUX_TARGET_PASS", rule.get("ssh_password", "VarTrack123"))
    # TOFU: fetch the host key on first contact and add it explicitly so
    # that we can use RejectPolicy (avoids silent MITM acceptance).
    _transport = paramiko.Transport((host, port))
    try:
        _transport.connect()
        _host_key = _transport.get_remote_server_key()
    finally:
        _transport.close()
    ssh = paramiko.SSHClient()
    # paramiko looks a host up as "[host]:port" unless the port is 22.
    known_as = host if port == 22 else f"[{host}]:{port}"
    ssh.get_host_keys().add(known_as, _host_key.get_name(), _host_key)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    ssh.connect(host, port=port, username=user, password=password, timeout=10)
    return ssh


def _linux_path(rule: dict, env: str) -> str:
    tenant = rule.get("tenant_id", DEMO_TENANT)
    dest_tpl = rule.get("destination_template", "")
    return dest_tpl.format(tenant=tenant, env=env) if dest_tpl else f"/tmp/vartrack/{tenant}/config.env"


def verify_linux_server(rule: dict, result: SuiteResult) -> None:
    if not HAS_SSH:
        result.add_check("paramiko available", False, "not installed")
        return
    remote_path = _linux_path(rule, rule.get("branch", "main"))

    try:
        ssh = _ssh_client(rule)
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
    tenant = rule.get("tenant_id", DEMO_TENANT)
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
    tenant = rule.get("tenant_id", DEMO_TENANT)
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
                found, pwd = mongo_value(db, col_name, "database.password")
                if found:
                    val = str(pwd)
                    not_placeholder = "placeholder" not in val.lower()
                    result.add_check("database.password resolved (not placeholder)", not_placeholder, "[value present]" if not_placeholder else "[placeholder]")
                    result.add_check("database.password matches Vault secret",
                                     val == "supersecret123", "[matched]" if val == "supersecret123" else "[mismatch]")
                else:
                    result.add_check("database.password found", False,
                                     "not stored as a key document nor under data")
                found, secret_key = mongo_value(db, col_name, "api.secret_key")
                if found:
                    val = str(secret_key)
                    result.add_check("api.secret_key matches Vault secret",
                                     val == "api-secret-xyz789", "[matched]" if val == "api-secret-xyz789" else "[mismatch]")
                else:
                    result.add_check("api.secret_key found", False,
                                     "not stored as a key document nor under data")
        client.close()
    except Exception as exc:
        result.add_check("MongoDB vault check", False, _exc_type(exc))


SINK_VERIFIERS = {
    "mongo":        verify_mongo,
    "mongo-document": verify_mongo_per_file,
    "mongo-file":   verify_mongo_per_file,
    "zookeeper":    verify_zookeeper,
    "redis":        verify_redis,
    "redis-hash":   verify_redis,
    "redis-string": verify_redis,
    "linux_server": verify_linux_server,
    "s3":           verify_s3,
}


# ── a suite starts with its own destinations empty ────────────────────────────

def _destination_names(rule: dict, envs: tuple[str, ...]) -> list[str]:
    """What the rule's destination template names, for each environment it can write."""
    tenant = rule.get("tenant_id", DEMO_TENANT)
    tpl = rule.get("destination_template", "")
    return sorted({tpl.format(tenant=tenant, env=env.replace("/", "_")) for env in envs}) if tpl else []


def reset_destination(rule: dict, envs: tuple[str, ...] = ("main", "default")) -> list[str]:
    """Empty what *rule* writes to, so a suite finds in the store only what it wrote itself, whatever an
    earlier run (or a self-heal between runs) left there. Only the rule's own destination is touched.
    Returns what was removed. A store that cannot be reached is a warning here: the suite that needs it
    fails on its own checks."""
    datasource = rule.get("datasource", "")
    removed: list[str] = []
    names = _destination_names(rule, envs)
    try:
        if datasource.startswith("mongo") and HAS_MONGO:
            client = MongoClient(rule.get("mongo_uri", "mongodb://mongo:27017"), serverSelectionTimeoutMS=5000)
            db = client[rule.get("database", "vartrack")]
            for name in names or [rule.get("collection", "variables")]:
                if name in db.list_collection_names() and db[name].count_documents({}):
                    db.drop_collection(name)
                    removed.append(f"mongo {name}")
            client.close()
        elif datasource.startswith("zookeeper") and HAS_ZK:
            zk = KazooClient(hosts=",".join(rule.get("zk_hosts", ["zookeeper:2181"])), timeout=10)
            zk.start(timeout=10)
            try:
                for root in names or [f"/vartrack/{rule.get('tenant_id', DEMO_TENANT)}/default"]:
                    if zk.exists(root):
                        zk.delete(root, recursive=True)
                        removed.append(f"zookeeper {root}")
            finally:
                zk.stop()
        elif datasource.startswith("redis") and HAS_REDIS:
            r = redis_lib.Redis(host=rule.get("redis_host", "redis"), port=int(rule.get("redis_port", 6379)),
                                db=int(rule.get("redis_db", 0)), socket_timeout=5, decode_responses=True)
            for prefix in names or [f"{rule.get('tenant_id', DEMO_TENANT)}:default"]:
                keys = r.keys(f"{prefix}*")
                if keys:
                    r.delete(*keys)
                    removed.append(f"redis {prefix}* ({len(keys)} keys)")
            r.close()
        elif datasource.startswith("linux_server") and HAS_SSH:
            ssh = _ssh_client(rule)
            try:
                for env in envs:
                    path = _linux_path(rule, env.replace("/", "_"))
                    _, stdout, _ = ssh.exec_command(f"test -e {path} && rm -f {path} && echo removed")
                    if "removed" in stdout.read().decode():
                        removed.append(f"file {path}")
            finally:
                ssh.close()
        elif datasource.startswith("s3") and HAS_BOTO:
            s3 = boto3.client(
                "s3",
                endpoint_url=os.environ.get("MINIO_ENDPOINT", rule.get("s3_endpoint_url", "http://minio:9002")),
                aws_access_key_id=os.environ.get("MINIO_ACCESS_KEY", rule.get("s3_access_key", "minioadmin")),
                aws_secret_access_key=os.environ.get("MINIO_SECRET_KEY", rule.get("s3_secret_key", "minioadmin")),
                region_name=rule.get("s3_region", "us-east-1"),
                config=BotoConfig(signature_version="s3v4"),
            )
            bucket = rule.get("s3_bucket", "vartrack")
            for prefix in [n.strip("/") for n in names]:
                listed = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
                for obj in listed:
                    s3.delete_object(Bucket=bucket, Key=obj["Key"])
                if listed:
                    removed.append(f"s3 {bucket}/{prefix} ({len(listed)} objects)")
    except Exception as exc:
        warn(f"could not empty the destination of {datasource}: {_exc_type(exc)}")
    if removed:
        log(f"  emptied what an earlier run left: {', '.join(removed)}")
    return removed


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator helpers
# ─────────────────────────────────────────────────────────────────────────────

# ── the orchestrator, called the way the gateway calls it ─────────────────────

class OrchestratorClient(str):
    """The orchestrator's base URL, which signs every call it makes.

    It is a str so the suites' ``f"{orchestrator}/v1/…"`` keep working; ``.get`` and ``.post`` add
    ``Authorization: Bearer <grant>`` to the call, with a fresh grant for exactly that call (audience sync,
    subject e2e-runner, platform gitea, the datasource of the URL when it has one, no tenant: the tenant of
    a push is derived from the pushed repository). With no INTERNAL_AUTH_SECRET the orchestrator runs open
    and nothing is added. Suite 18 uses ``.raw`` to send the calls that must be refused."""

    secret: str = ""

    def __new__(cls, url: str, secret: str = "") -> "OrchestratorClient":
        obj = super().__new__(cls, url.rstrip("/"))
        obj.secret = secret
        return obj

    def grant(self, *, datasource: str = "", tenant: str = "", platform: str = "gitea",
              audience: str = "sync", secret: str | None = None, ttl: int = 60,
              now: float | None = None) -> str:
        import grant
        return grant.mint(audience, secret=self.secret if secret is None else secret, subject="e2e-runner",
                          tenant=tenant, platform=platform, datasource=datasource, ttl=ttl, now=now)

    def headers(self, datasource: str = "", extra: dict | None = None) -> dict:
        out = dict(extra or {})
        if self.secret:
            out["Authorization"] = f"Bearer {self.grant(datasource=datasource)}"
        return out

    def raw(self, method: str, path: str, *, headers: dict | None = None, timeout: int = 20, **kw):
        """A call with exactly the headers given: nothing is signed for it."""
        return requests.request(method, f"{self}{path}", headers=headers or {}, timeout=timeout, **kw)

    def post(self, path: str, *, datasource: str = "", headers: dict | None = None, timeout: int = 20, **kw):
        return self.raw("POST", path, headers=self.headers(datasource, headers), timeout=timeout, **kw)

    def get(self, path: str, *, datasource: str = "", headers: dict | None = None, timeout: int = 20, **kw):
        return self.raw("GET", path, headers=self.headers(datasource, headers), timeout=timeout, **kw)


def _client(orchestrator: str) -> OrchestratorClient:
    """*orchestrator* as a signing client (a plain URL signs with the demo secret from the environment)."""
    if isinstance(orchestrator, OrchestratorClient):
        return orchestrator
    return OrchestratorClient(orchestrator, os.environ.get("INTERNAL_AUTH_SECRET", ""))


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
        resp = _client(orchestrator).post(
            f"/v1/webhooks/{datasource}",
            datasource=datasource,
            json=payload,
            headers={"Content-Type": "application/json", "X-Gitea-Event": "push"},
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
        resp = _client(orchestrator).post(
            f"/v1/webhooks/{datasource}/dry-run",
            datasource=datasource,
            json=payload,
            headers={"Content-Type": "application/json", "X-Gitea-Event": "push"},
        )
        if resp.ok:
            return resp.json()
        warn(f"Dry-run returned {resp.status_code}: {_detail(resp)}")
        return None
    except Exception as exc:
        warn(f"Dry-run exception for {datasource}: {_exc_type(exc)}")
        return None


def warm_schema(orchestrator: str, tenant_id: str) -> dict:
    """Have the orchestrator fetch the tenant's schema repository afresh, and say what it now holds:
    {"rules_loaded", "schema_commit"}. Raises when it cannot: a suite must not run on schemas that were
    never loaded. (The warm-up also drops the orchestrator's cached rules for the tenant, through the
    product: nothing here reaches into its Redis.)"""
    # The body names no repository: a tenant's schema repository is the operator's to declare
    # (config.cue's tenants), and a request cannot choose or change it.
    resp = _client(orchestrator).post(
        f"/v1/tenants/{tenant_id}/schemas/warm",
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"schema warm-up of tenant {tenant_id} was refused: {resp.status_code} {_detail(resp)}")
    body = resp.json()
    return {"rules_loaded": body.get("rules_loaded", 0), "schema_commit": body.get("schema_commit", "")}


# ── waiting for a task, and what it says ──────────────────────────────────────

@dataclass
class TaskOutcome:
    """What a task reports when it has finished, or why it has not.

    *state* is varTrack's own (SUCCEEDED or FAILED, from the task's result), or TIMEOUT / ERROR when the
    task did not finish or could not be asked; *detail* then says what it was doing or what was refused."""
    task_id: str
    state: str
    message: str = ""
    files: list = field(default_factory=list)
    schema_commit: str = ""
    dry_run: bool = False
    detail: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.state in ("SUCCEEDED", "FAILED")

    def said(self) -> str:
        return f"{self.state}: {self.message or self.detail}"[:240]


def _outcome_from_result(task_id: str, finished: dict) -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id,
        state=str(finished.get("state", "")),
        message=str(finished.get("message", "")),
        files=list(finished.get("files") or []),
        schema_commit=str(finished.get("schema_commit") or ""),
        dry_run=bool(finished.get("dry_run")),
        raw=finished,
    )


_POLL_INTERVALS = (0.05, 0.1, 0.2, 0.5, 1.0)


def _detail(resp) -> str:
    """The `detail` an orchestrator error carries, or the start of its body."""
    try:
        return str(resp.json().get("detail", ""))[:200]
    except Exception:
        return resp.text[:200]


def wait_for_task(orchestrator: str, task_id: str, *, timeout: float | None = None) -> TaskOutcome:
    """Poll GET /v1/tasks/{id} until the task has finished, and return what it reports.

    Finished is varTrack's own result state (SUCCEEDED or FAILED), never Celery's: a task whose function
    returned is SUCCESS to Celery even when its sync failed. A refused poll (4xx) ends the wait at once, and
    so does a task that raised. Only a task still running when *timeout* (E2E_TIMEOUT) runs out is a
    TIMEOUT, and it says what the task was doing."""
    client = _client(orchestrator)
    limit = E2E_TIMEOUT if timeout is None else timeout
    start = time.monotonic()
    attempt = 0
    last = "no answer yet"
    while time.monotonic() - start < limit:
        try:
            resp = client.get(f"/v1/tasks/{task_id}", timeout=10)
        except requests.RequestException as exc:
            last = f"the poll failed ({_exc_type(exc)})"
        else:
            if resp.status_code == 500:            # the task raised
                return TaskOutcome(task_id, "FAILED", message=_detail(resp), detail="the task raised")
            if 400 <= resp.status_code < 500:
                return TaskOutcome(task_id, "ERROR",
                                   detail=f"polling the task was refused: {resp.status_code} {_detail(resp)}")
            if resp.ok:
                body = resp.json()
                finished = body.get("result")
                if isinstance(finished, dict) and finished.get("state") in ("SUCCEEDED", "FAILED"):
                    return _outcome_from_result(task_id, finished)
                if body.get("state") == "REVOKED":
                    return TaskOutcome(task_id, "ERROR", detail="the task was revoked")
                stage = finished.get("stage") if isinstance(finished, dict) else None
                last = f"celery state {body.get('state')}" + (f", stage {stage}" if stage else "")
            else:
                last = f"HTTP {resp.status_code}"
        time.sleep(_POLL_INTERVALS[min(attempt, len(_POLL_INTERVALS) - 1)])
        attempt += 1
    return TaskOutcome(task_id, "TIMEOUT", detail=f"still running after {limit:.0f}s ({last})")


def ui_session(orchestrator: str, gitea_url: str, user: str = "demo-admin", password: str = "VarTrack123") -> requests.Session:
    """A session on the orchestrator's UI API, signed in the way a person signs in: through Gitea's own login form and
    consent page. The UI API (/v1/ui/*) takes a session, not a service grant, once the bundle has a `ui` block.

    The browser would reach the orchestrator through the UI on :8080 and Gitea on :3000; in the compose network they
    are orchestrator-api:8000 and gitea:3000, so the two addresses in the redirects are swapped for those."""
    def csrf(html: str) -> str:
        m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
        if not m:
            raise RuntimeError("Gitea's page has no CSRF token: the sign-in cannot go on")
        return m.group(1)

    orch = str(orchestrator).rstrip("/")
    base = gitea_url.rstrip("/")

    def absolute(location: str) -> str:
        return location if location.startswith("http") else base + location

    mine, gitea = requests.Session(), requests.Session()
    url = mine.get(f"{orch}/v1/auth/login", params={"platform": "gitea", "return_to": "/"},
                   allow_redirects=False, timeout=15).headers["Location"]
    for _ in range(8):
        if "/auth/callback" in url:
            callback = url.split("/auth/callback", 1)[1]
            done = mine.get(f"{orch}/v1/auth/callback{callback}", allow_redirects=False, timeout=15)
            if "error=" in done.headers.get("Location", ""):
                raise RuntimeError(f"the UI sign-in was refused: {done.headers['Location']}")
            return mine
        url = absolute(url.replace("http://localhost:3000", base))
        page = gitea.get(url, allow_redirects=False, timeout=15)
        if page.is_redirect:
            url = absolute(page.headers["Location"])
            continue
        if 'name="user_name"' in page.text:
            page = gitea.post(f"{gitea_url.rstrip('/')}/user/login", allow_redirects=False, timeout=15,
                              data={"_csrf": csrf(page.text), "user_name": user, "password": password})
            url = absolute(page.headers.get("Location", url))
            continue
        if 'name="granted"' in page.text:
            form = {"_csrf": csrf(page.text), "granted": "true"}
            for name in ("client_id", "state", "scope", "nonce", "redirect_uri"):
                m = re.search(rf'name="{name}"\s+value="([^"]*)"', page.text)
                if m:
                    form[name] = m.group(1).replace("&amp;", "&")
            url = absolute(gitea.post(f"{base}/login/oauth/grant", data=form, allow_redirects=False,
                                      timeout=15).headers["Location"])
            continue
        raise RuntimeError(f"the UI sign-in stopped at {url}")
    raise RuntimeError("the UI sign-in did not finish")


def _files_summary(files: list) -> str:
    if not files:
        return "no files"
    return "; ".join(
        f"{f.get('file')}@{f.get('env')}: {f.get('status')} written={f.get('written', 0)}"
        + (f" ({f['error']})" if f.get("error") else "")
        for f in files)[:300]


def check_schema_commit(result: "SuiteResult", outcome: TaskOutcome, label: str = "") -> None:
    """The task validated against the schemas this suite pushed, not against an older copy a worker held."""
    want = result.schema_commit
    if not want:
        return
    result.add_check(f"{label}Validated against the schemas pushed for this suite",
                     outcome.schema_commit == want,
                     f"task={outcome.schema_commit[:12] or '(none)'} pushed={want[:12]}")


def check_synced(result: "SuiteResult", outcome: TaskOutcome, label: str = "") -> bool:
    """The task finished SUCCEEDED, every file it synced is ok with keys written, and it ran on the
    schemas the suite pushed. Returns whether the task finished at all (more to check if so)."""
    result.add_check(f"{label}Task finished SUCCEEDED", outcome.state == "SUCCEEDED", outcome.said())
    if not outcome.finished:
        return False
    synced = bool(outcome.files) and all(
        f.get("status") == "ok" and f.get("written", 0) > 0 for f in outcome.files)
    result.add_check(f"{label}Every file synced (status ok, keys written)", synced, _files_summary(outcome.files))
    check_schema_commit(result, outcome, label)
    return True


def check_rejected(result: "SuiteResult", outcome: TaskOutcome, reason: str, label: str = "") -> None:
    """The task finished FAILED for *reason* and synced nothing."""
    result.add_check(f"{label}Task finished FAILED", outcome.state == "FAILED", outcome.said())
    if not outcome.finished:
        return
    named = reason in outcome.message or any(reason in str(f.get("error", "")) for f in outcome.files)
    result.add_check(f"{label}The failure names its reason ({reason})", named, outcome.message[:200])
    result.add_check(f"{label}No file was synced", not any(f.get("status") == "ok" for f in outcome.files),
                     _files_summary(outcome.files))
    check_schema_commit(result, outcome, label)


def sync_and_wait(result: "SuiteResult", orchestrator: str, datasource: str, tenant: str,
                  commit_sha: str, gitea_url: str, label: str = "") -> TaskOutcome | None:
    """Push webhook for *commit_sha*, wait for its task, check it synced. None when nothing was accepted."""
    task_id = trigger_webhook(orchestrator, datasource, tenant, commit_sha, gitea_url)
    result.add_check(f"{label}Webhook accepted (task_id returned)", task_id is not None, str(task_id))
    if not task_id:
        return None
    result.task_id = task_id
    outcome = wait_for_task(orchestrator, task_id)
    check_synced(result, outcome, label)
    return outcome


def check_dry_run(result: "SuiteResult", rule: dict, orchestrator: str, gitea_url: str, commit_sha: str,
                  tenant: str, datasource: str) -> TaskOutcome | None:
    """A dry run previews the sync the push would run: the report's task SUCCEEDED, validated every file,
    named the environment the real push writes, and ran on this suite's schemas. (The endpoint waits for
    the task and returns its result as `report`; a task it stopped waiting for is polled here.)"""
    dr = dry_run_webhook(orchestrator, datasource, tenant, commit_sha, gitea_url)
    result.add_check("Dry-run response received", dr is not None)
    if dr is None:
        return None
    result.task_id = dr.get("task_id")
    report = dr.get("report")
    outcome = (_outcome_from_result(dr["task_id"], report) if isinstance(report, dict)
               else wait_for_task(orchestrator, dr["task_id"]))
    result.add_check("Dry-run task finished SUCCEEDED", outcome.state == "SUCCEEDED", outcome.said())
    if not outcome.finished:
        return outcome
    result.add_check("Dry-run flag set in the report", outcome.dry_run is True)
    previewed = bool(outcome.files) and all(
        f.get("validation") == "ok" and f.get("keys", 0) > 0 for f in outcome.files)
    result.add_check("Dry-run previews every file (validated, keys found)", previewed,
                     "; ".join(f"{f.get('file')}: {f.get('validation')} keys={f.get('keys')}" for f in outcome.files))
    want_env = "main" if rule.get("branch_as_env") else "default"
    result.add_check(f"Dry-run previews the environment the push writes ({want_env})",
                     bool(outcome.files) and all(f.get("env") == want_env for f in outcome.files),
                     ", ".join(str(f.get("env")) for f in outcome.files))
    check_schema_commit(result, outcome, "Dry-run: ")
    return outcome


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
    envs: tuple[str, ...] = ("main", "default"),
) -> SuiteResult:
    rule = rules[0]
    datasource = rule["datasource"]
    tenant = rule.get("tenant_id", DEMO_TENANT)
    result = SuiteResult(suite_id, name, setting_file, datasource)
    banner(f"Suite {suite_id:02d}: {name}")

    try:
        # What the suite finds in the store is what it wrote: start from an empty destination.
        for r in rules:
            reset_destination(r, envs)

        # The suite's rules go to the tenant's schema repository. The commit they land at is what the
        # warm-up has to hold and what every task of the suite has to report having validated against.
        result.schema_commit = gitea.push_bundle(rules)
        commit_sha = gitea.get_head_sha(CONFIG_REPO)
        result.add_check("Gitea commit SHA resolved", commit_sha != "0" * 40, commit_sha[:12])

        # The orchestrator fetches the schemas afresh (and forgets the tenant's cached rules) so the bundle
        # just pushed is the one used; it says which commit it now holds.
        warmed = warm_schema(orchestrator, tenant)
        result.add_check("Schema registry warmed at the pushed commit",
                         warmed["schema_commit"] == result.schema_commit and warmed["rules_loaded"] > 0,
                         f"rules={warmed['rules_loaded']} holds={warmed['schema_commit'][:12] or '(none)'} "
                         f"pushed={result.schema_commit[:12]}")

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
    outcome = sync_and_wait(result, orchestrator, datasource, tenant, commit_sha, gitea_url)

    verifier = SINK_VERIFIERS.get(datasource)
    if verifier:
        verifier(rule, result)
    else:
        result.add_check(f"Verifier for '{datasource}'", False, "no verifier registered")
    return outcome


def _variant(rules: list[dict], **changes) -> list[dict]:
    """A copy of *rules* (never the shared ones) with the first rule's options changed: how a suite gets a
    destination of its own, so what it finds in the store is what it wrote."""
    out = json.loads(json.dumps(rules))
    out[0].update(changes)
    return out


def _mongo_collection(rule: dict) -> str:
    tpl = rule.get("destination_template", "")
    env = rule.get("branch", "main").replace("/", "_")
    return tpl.format(tenant=rule.get("tenant_id", DEMO_TENANT), env=env) if tpl else rule.get("collection", "variables")


def verify_mongo_untouched(rule: dict, result: SuiteResult, what: str) -> None:
    """The rule's collection holds no document (or does not exist): nothing was written there."""
    if not HAS_MONGO:
        result.add_check("pymongo available", False, "not installed")
        return
    col_name = _mongo_collection(rule)
    try:
        client = MongoClient(rule.get("mongo_uri", "mongodb://mongo:27017"), serverSelectionTimeoutMS=5000)
        db = client[rule.get("database", "vartrack")]
        count = db[col_name].count_documents({}) if col_name in db.list_collection_names() else 0
        result.add_check(f"{what}: '{col_name}' has 0 documents", count == 0, f"count={count}")
        client.close()
    except Exception as exc:
        result.add_check(f"{what}: MongoDB check", False, _exc_type(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Suite definitions (23 total)
# ─────────────────────────────────────────────────────────────────────────────

def suite_01_mongo_doc_basic(orchestrator, gitea, gitea_url, rules):
    """MongoDB STRATEGY_DOCUMENT: the file as one nested document."""
    return run_suite(1, "MongoDB document write (STRATEGY_DOCUMENT)",
                     "12_mongo_document_strategy.json", rules, orchestrator, gitea, gitea_url)


def suite_02_mongo_doc_prune(orchestrator, gitea, gitea_url, rules):
    """MongoDB: prune=true removes stale keys on second push."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        # First push
        _default_etl_check(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)
        # Second push (idempotent) — confirms prune doesn't wipe valid keys
        sha2 = gitea.get_head_sha(CONFIG_REPO)
        sync_and_wait(result, orchestrator, datasource, tenant, sha2, gitea_url, label="Second push: ")
        verify_mongo(rule, result)

    return run_suite(2, "MongoDB prune idempotency",
                     "01_mongo_document.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_03_mongo_file_strategy(orchestrator, gitea, gitea_url, rules):
    """MongoDB STRATEGY_FILE stores whole file document, not a document per key."""
    return run_suite(3, "MongoDB STRATEGY_FILE snapshot",
                     "02_mongo_file.json", rules, orchestrator, gitea, gitea_url)


def suite_04_mongo_dry_run(orchestrator, gitea, gitea_url, rules):
    """MongoDB: a dry run reports the sync the push would run, and writes nothing."""
    own = _variant(rules, destination_template="e2e-dry-run-{env}")

    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        check_dry_run(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)
        # The dry run has finished (the endpoint waited for its task): nothing it could write is pending.
        verify_mongo_untouched(rule, result, "Dry run wrote nothing")

    return run_suite(4, "MongoDB dry-run (no write)",
                     "01_mongo_document.json", own, orchestrator, gitea, gitea_url, extra_fn=_fn)


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
    """All three main sinks triggered at once; each task is waited for, and each sink has its own output."""
    mongo_rules = _variant(mongo_rules, destination_template="e2e-parallel-{env}")
    # (this rule does not map the branch to an environment, so its path ends in "default", as its settings file does)
    zk_rules = _variant(zk_rules, destination_template="/e2e-parallel/{tenant}/default")
    redis_rules = _variant(redis_rules, destination_template="e2e-parallel:{env}:config")
    result = SuiteResult(11, "Multi-sink parallel webhooks (mongo + zk + redis)",
                         "mixed", "multi")
    banner("Suite 11: Multi-sink parallel webhooks")
    try:
        for r in mongo_rules + zk_rules + redis_rules:
            reset_destination(r)
        result.schema_commit = gitea.push_bundle(mongo_rules + zk_rules + redis_rules)
        commit_sha = gitea.get_head_sha(CONFIG_REPO)
        warmed = warm_schema(orchestrator, DEMO_TENANT)
        result.add_check("Schema registry warmed at the pushed commit",
                         warmed["schema_commit"] == result.schema_commit and warmed["rules_loaded"] > 0,
                         f"rules={warmed['rules_loaded']} holds={warmed['schema_commit'][:12] or '(none)'} "
                         f"pushed={result.schema_commit[:12]}")

        started = []
        for ds, rules in [("mongo", mongo_rules), ("zookeeper", zk_rules), (redis_rules[0]["datasource"], redis_rules)]:
            tenant = rules[0].get("tenant_id", DEMO_TENANT)
            tid = trigger_webhook(orchestrator, ds, tenant, commit_sha, gitea_url)
            result.add_check(f"Webhook sent to {ds}", tid is not None, str(tid))
            if tid:
                started.append((ds, tid))

        # All three are running at once; each is waited for by its own task.
        for ds, tid in started:
            check_synced(result, wait_for_task(orchestrator, tid), label=f"{ds}: ")

        verify_mongo(mongo_rules[0], result)
        verify_zookeeper(zk_rules[0], result)
        verify_redis(redis_rules[0], result)

        result.finish("pass" if all(c["passed"] for c in result.checks) else "fail")
    except Exception as exc:
        result.error = traceback.format_exc()
        err(f"Suite 11 raised exception: {exc}")
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
        r = _client(orchestrator).get("/v1/health", timeout=5)
        result.add_check("/v1/health returns 200", r.status_code == 200, str(r.status_code))

        r2 = _client(orchestrator).get("/metrics", timeout=5)
        result.add_check("/metrics returns 200", r2.status_code == 200, str(r2.status_code))
        if r2.ok:
            has_celery = "celery" in r2.text.lower() or "task" in r2.text.lower()
            result.add_check("/metrics contains task metrics", has_celery)

        r3 = _client(orchestrator).get("/docs", timeout=5)
        result.add_check("/docs (OpenAPI) reachable", r3.status_code == 200)

        result.finish("pass" if all(c["passed"] for c in result.checks) else "fail")
    except Exception:
        result.error = traceback.format_exc()
        result.finish("fail")

    colour_code = colour.GREEN if result.status == "pass" else colour.RED
    print(f"\n  {colour_code}→ Suite 12 {result.status.upper()} ({result.duration_ms:.0f} ms){colour.NC}")
    return result


def suite_13_cue_schema_valid(orchestrator, gitea, gitea_url, rules):
    """CUE schema present — valid config passes validation and writes to MongoDB."""
    own = _variant(rules, destination_template="e2e-cue-valid-{env}")

    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        sync_and_wait(result, orchestrator, datasource, tenant, commit_sha, gitea_url)
        verify_mongo(rule, result)
        if HAS_MONGO:
            try:
                col_name = _mongo_collection(rule)
                client = MongoClient(rule.get("mongo_uri", "mongodb://mongo:27017"),
                                     serverSelectionTimeoutMS=5000)
                db = client[rule.get("database", "vartrack")]
                found, level = mongo_value(db, col_name, "observability.log_level")
                if found:
                    val = str(level)
                    result.add_check(f"CUE validated log_level='{val}' (allowed by schema)",
                                     val in ("DEBUG", "INFO", "WARN", "ERROR"), val)
                else:
                    result.add_check("observability.log_level found", False,
                                     "key not written to collection")
                client.close()
            except Exception as exc:
                result.add_check("CUE log_level check", False, _exc_type(exc))

    return run_suite(13, "CUE schema validation (valid config passes)",
                     "01_mongo_document.json", own, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_14_vault_secret_injection(orchestrator, gitea, gitea_url, rules):
    """Vault @secret fields resolved before writing to MongoDB."""
    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        sync_and_wait(result, orchestrator, datasource, tenant, commit_sha, gitea_url)
        verify_vault(rule, result)

    return run_suite(14, "Vault secret injection → MongoDB",
                     "09_vault_mongo.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_15_cue_strict_reject(orchestrator, gitea, gitea_url, rules,
                                bad_config: bytes, orig_config: bytes):
    """CUE strict_validation=true rejects invalid log_level — the task FAILS naming it, and nothing is written.

    The invalid config is pushed before the strict rule is: while the rule exists, the config repository
    never holds a valid file for a self-heal to sync into the collection the suite then expects empty."""
    try:
        bad_sha = gitea.push_config_file(bad_config, "e2e: invalid log_level for CUE strict test")
    except Exception:
        failed = SuiteResult(15, "CUE strict_validation rejects invalid config", "10_cue_strict_mongo.json",
                             rules[0]["datasource"])
        failed.error = traceback.format_exc()
        failed.add_check("The invalid config was pushed", False, "see the suite's error")
        failed.finish("fail")
        return failed

    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        task_id = trigger_webhook(orchestrator, datasource, tenant, bad_sha, gitea_url)
        result.add_check("Webhook accepted (task_id returned)", task_id is not None, str(task_id))
        if task_id:
            result.task_id = task_id
            # The task is finished when it says so: only then is "nothing written" a fact.
            check_rejected(result, wait_for_task(orchestrator, task_id), "strict_validation")
        verify_mongo_untouched(rule, result, "CUE strict reject (write blocked)")

    try:
        return run_suite(15, "CUE strict_validation rejects invalid config",
                         "10_cue_strict_mongo.json", rules, orchestrator, gitea, gitea_url, extra_fn=_fn)
    finally:
        gitea.push_config_file(orig_config, "e2e: restore valid config after strict test")


def suite_16_dual_mongo_secret(orchestrator, gitea, gitea_url, dual_rules):
    """Dual MongoDB (mongo-primary + mongo-dr) — both receive @secret injection from Vault."""
    result = SuiteResult(16, "Dual MongoDB (primary + DR) with @secret injection",
                         "11_dual_mongo_secret.json", "mongo-primary+mongo-dr")
    banner("Suite 16: Dual MongoDB datasources with @secret injection")
    try:
        for r in dual_rules:
            reset_destination(r)
        result.schema_commit = gitea.push_bundle(dual_rules)
        commit_sha = gitea.get_head_sha(CONFIG_REPO)
        result.add_check("Gitea commit SHA resolved", commit_sha != "0" * 40, commit_sha[:12])

        tenant = dual_rules[0].get("tenant_id", DEMO_TENANT)
        warmed = warm_schema(orchestrator, tenant)
        result.add_check("Schema registry warmed at the pushed commit",
                         warmed["schema_commit"] == result.schema_commit and warmed["rules_loaded"] > 0,
                         f"rules={warmed['rules_loaded']} holds={warmed['schema_commit'][:12] or '(none)'} "
                         f"pushed={result.schema_commit[:12]}")

        started = []
        for rule in dual_rules:
            ds = rule["datasource"]
            tid = trigger_webhook(orchestrator, ds, tenant, commit_sha, gitea_url)
            result.add_check(f"Webhook accepted for {ds}", tid is not None, str(tid))
            if tid:
                started.append((ds, tid))

        for ds, tid in started:
            check_synced(result, wait_for_task(orchestrator, tid), label=f"{ds}: ")

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
    """Self-heal: corrupt a value in MongoDB directly, re-sync, verify the value is restored."""
    own = _variant(rules, destination_template="e2e-self-heal-{env}")

    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        # First, do a normal write so there is something to corrupt.
        _default_etl_check(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)

        if not HAS_MONGO:
            result.add_check("pymongo available (self-heal)", False, "not installed")
            return

        uri       = rule.get("mongo_uri", "mongodb://mongo:27017")
        db_name   = rule.get("database", "vartrack")
        tenant_id = rule.get("tenant_id", DEMO_TENANT)
        col_name  = _mongo_collection(rule)

        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            db     = client[db_name]
            # Corrupt one key directly in the collection: its own document under STRATEGY_KEY_VALUE, else the value
            # nested under `data` in the environment's document.
            update_res = db[col_name].update_one(
                {"key": {"$exists": True}},
                {"$set": {"value": "__DRIFT_INJECTED__"}},
            )
            if update_res.modified_count == 0:
                update_res = db[col_name].update_one(
                    {"data.observability.log_level": {"$exists": True}},
                    {"$set": {"data.observability.log_level": "__DRIFT_INJECTED__"}},
                )
            corrupted = update_res.modified_count > 0
            result.add_check("Drift injected into MongoDB", corrupted,
                             f"modified={update_res.modified_count}")
            client.close()
        except Exception as exc:
            result.add_check("Drift injection", False, _exc_type(exc))
            return

        # Re-trigger the webhook — self_heal=true should overwrite the injected value. The re-sync is
        # waited for by its task, not by a pause.
        heal_sha = gitea.get_head_sha(CONFIG_REPO)
        sync_and_wait(result, orchestrator, datasource, tenant_id, heal_sha, gitea_url, label="Heal: ")

        # Verify the corrupted key no longer has the injected value.
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            db     = client[db_name]
            drifted = (db[col_name].find_one({"value": "__DRIFT_INJECTED__"})
                       or db[col_name].find_one({"data.observability.log_level": "__DRIFT_INJECTED__"}))
            result.add_check("Drift value removed (self-heal worked)", drifted is None,
                             "still present" if drifted else "cleared")
            client.close()
        except Exception as exc:
            result.add_check("Post-heal MongoDB check", False, _exc_type(exc))

    return run_suite(17, "Self-heal: drift correction via re-trigger",
                     "01_mongo_document.json", own, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_18_unsigned_calls_refused(orchestrator, gitea, gitea_url, rules):
    """The orchestrator refuses a call that is not signed properly, and a refused call changes nothing.

    With internal authentication on, every route but the health probes needs a valid grant (a signed token
    the gateway mints and the orchestrator verifies). This suite sends each kind of route the callers it must
    turn away (no credential, the wrong scheme, a forged signature, an expired grant, a grant for another
    service, for another datasource, for another tenant) and the one it must accept. It checks the exact
    status and message of each, so a call refused for the wrong reason does not pass, and afterwards that
    none of the refused calls left a task behind. The gateway's own HMAC check on a platform's webhook is
    not what runs here: it needs the gateway in production mode, and is covered by the gateway's tests."""
    result = SuiteResult(18, "Unsigned or forged calls to the orchestrator are refused",
                         "n/a", "orchestrator")
    banner("Suite 18: Unsigned or forged calls are refused")
    try:
        client = _client(orchestrator)
        if not client.secret:
            result.add_check("The orchestrator runs with internal authentication on", False,
                             "INTERNAL_AUTH_SECRET is not set: it runs open, so nothing can be refused")
            result.finish("fail")
            colour_code = colour.RED
            print(f"\n  {colour_code}→ Suite 18 FAIL ({result.duration_ms:.0f} ms){colour.NC}")
            return result

        datasource = rules[0]["datasource"]
        other_datasource = "zookeeper" if datasource != "zookeeper" else "mongo"
        real_sha = gitea.get_head_sha(CONFIG_REPO)
        refused_sha = "0123456789abcdef0123456789abcdef01234567"     # a push no refused call may queue
        task_probe = str(uuid.uuid4())

        def push(sha: str) -> dict:
            return {
                "ref": "refs/heads/main", "before": "0" * 40, "after": sha,
                "repository": {
                    "name": CONFIG_REPO, "full_name": f"{GITEA_ORG}/{CONFIG_REPO}",
                    "owner": {"name": GITEA_ORG, "login": GITEA_ORG},
                    "clone_url": f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}.git",
                    "html_url": f"{gitea_url}/{GITEA_ORG}/{CONFIG_REPO}", "default_branch": "main",
                },
                "pusher": {"name": "e2e-runner", "email": "e2e@vartrack.local"},
                "commits": [{"id": sha, "message": "unsigned-call", "added": [CONFIG_FILE],
                             "removed": [], "modified": []}],
                "head_commit": {"id": sha, "message": "unsigned-call", "added": [CONFIG_FILE],
                                "removed": [], "modified": []},
            }

        GRANT_ONLY = "a signed grant is required"
        EITHER = "a signed grant or a signed-in session is required"   # /v1/tasks takes a grant or a session
        json_headers = {"Content-Type": "application/json", "X-Gitea-Event": "push"}

        # route: (label, method, path, body for a refused call, body for the control, grant datasource, message kind)
        routes = [
            ("webhook", "POST", f"/v1/webhooks/{datasource}", push(refused_sha), push(real_sha), datasource, GRANT_ONLY),
            ("dry-run", "POST", f"/v1/webhooks/{datasource}/dry-run", push(refused_sha), push(real_sha),
             datasource, GRANT_ONLY),
            ("schema warm-up", "POST", f"/v1/tenants/{DEMO_TENANT}/schemas/warm", None, None, "", GRANT_ONLY),
            ("task status", "GET", f"/v1/tasks/{task_probe}", None, None, "", EITHER),
            ("metrics", "GET", "/metrics", None, None, "", GRANT_ONLY),
        ]

        other_key = "another-secret-another-secret-0123456789"
        callers = [
            # name, headers for the call, the refusal's message for a grant-only route
            ("no credential", lambda ds: {}, GRANT_ONLY),
            ("a Basic header", lambda ds: {"Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()},
             GRANT_ONLY),
            ("a grant signed with another key",
             lambda ds: {"Authorization": f"Bearer {client.grant(datasource=ds, secret=other_key)}"},
             "invalid grant: invalid signature"),
            ("an expired grant",
             lambda ds: {"Authorization": f"Bearer {client.grant(datasource=ds, now=time.time() - 3600)}"},
             "invalid grant: token has expired"),
            ("a grant for another service (watch)",
             lambda ds: {"Authorization": f"Bearer {client.grant(datasource=ds, audience='watch')}"},
             "invalid grant: wrong audience"),
        ]

        def detail_of(resp) -> str:
            try:
                return str(resp.json().get("detail", ""))
            except Exception:
                return resp.text[:120]

        for label, method, path, bad_body, good_body, grant_ds, kind in routes:
            for caller, make_headers, message in callers:
                want = EITHER if kind == EITHER else message
                kw = {"json": bad_body} if bad_body is not None else {}
                resp = client.raw(method, path, headers={**json_headers, **make_headers(grant_ds)}, **kw)
                got = detail_of(resp)
                result.add_check(f"{label}: {caller} → 401",
                                 resp.status_code == 401 and want in got and "task_id" not in resp.text,
                                 f"status={resp.status_code} detail={got!r}")

        # a valid grant that is for another datasource / another tenant is refused with 403
        for label, method, path, body, grant_kw, want in [
            ("webhook", "POST", f"/v1/webhooks/{datasource}", push(refused_sha),
             {"datasource": other_datasource}, "the grant was issued for another datasource"),
            ("dry-run", "POST", f"/v1/webhooks/{datasource}/dry-run", push(refused_sha),
             {"datasource": other_datasource}, "the grant was issued for another datasource"),
            ("schema warm-up", "POST", f"/v1/tenants/{DEMO_TENANT}/schemas/warm", None,
             {"tenant": "payments"}, "the grant was issued for another tenant"),
        ]:
            headers = {**json_headers, "Authorization": f"Bearer {client.grant(**grant_kw)}"}
            kw = {"json": body} if body is not None else {}
            resp = client.raw(method, path, headers=headers, **kw)
            got = detail_of(resp)
            what = "datasource" if "datasource" in grant_kw else "tenant"
            result.add_check(f"{label}: a valid grant for another {what} → 403",
                             resp.status_code == 403 and want in got and "task_id" not in resp.text,
                             f"status={resp.status_code} detail={got!r}")

        # the control: the same calls, signed for exactly what they do, are accepted
        control_task = None
        for label, method, path, bad_body, good_body, grant_ds, kind in routes:
            kw = {"json": good_body} if good_body is not None else {}
            resp = client.raw(method, path, headers={**json_headers, **client.headers(grant_ds)}, **kw)
            accepted = resp.status_code == 200
            if label == "webhook" and accepted:
                control_task = resp.json().get("task_id")
                accepted = bool(control_task)
            result.add_check(f"{label}: a valid grant for this call → 200", accepted,
                             f"status={resp.status_code}")
        result.task_id = control_task

        # nothing a refused call carried was queued, and the task list can see what is queued
        # (the UI API takes a session, since the bundle has a ui block: sign in as the admin, as a person would)
        listing = ui_session(client, gitea_url).get(f"{client}/v1/ui/tasks", params={"limit": 500}, timeout=10)
        tasks = listing.json().get("tasks", []) if listing.ok else []
        seen = {t.get("revision") for t in tasks}
        result.add_check("The task list is readable", listing.ok, f"status={listing.status_code}")
        result.add_check("No refused call left a task behind", refused_sha not in seen,
                         f"{len(tasks)} task(s) listed")
        result.add_check("The accepted control call is in the task list",
                         control_task is not None and any(t.get("taskId") == control_task for t in tasks),
                         str(control_task))
        if control_task:
            # The control call queued a real sync: wait for it, so the next suite starts with nothing running.
            outcome = wait_for_task(client, control_task)
            result.add_check("The accepted control call's task finished SUCCEEDED",
                             outcome.state == "SUCCEEDED", outcome.said())

        result.finish("pass" if all(c["passed"] for c in result.checks) else "fail")
    except Exception as exc:
        result.error = traceback.format_exc()
        err(f"Suite 18 raised exception: {_exc_type(exc)}")
        result.finish("fail")

    colour_code = colour.GREEN if result.status == "pass" else colour.RED
    print(f"\n  {colour_code}→ Suite 18 {result.status.upper()} ({result.duration_ms:.0f} ms){colour.NC}")
    return result


def suite_19_redis_dry_run(orchestrator, gitea, gitea_url, rules):
    """Redis dry-run must not write any keys."""
    own = _variant(rules, destination_template="e2e-dry-run:{env}:config")

    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        if not HAS_REDIS:
            result.add_check("redis-py available", False, "not installed")
            return

        host = rule.get("redis_host", "redis")
        port = int(rule.get("redis_port", 6379))
        db   = int(rule.get("redis_db", 0))
        dest_tpl = rule.get("destination_template", "")
        prefix   = dest_tpl.format(tenant=tenant, env="main") if dest_tpl else f"{tenant}:default"

        # Keys under this suite's own prefix before the dry run: none.
        try:
            r = redis_lib.Redis(host=host, port=port, db=db, socket_timeout=5, decode_responses=True)
            before_keys = len(r.keys(f"{prefix}*"))
            r.close()
        except Exception as exc:
            result.add_check("Redis pre-check", False, _exc_type(exc))
            return

        # The dry run has finished when this returns (the endpoint waits for its task).
        check_dry_run(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)

        # Verify no keys were written.
        try:
            r = redis_lib.Redis(host=host, port=port, db=db, socket_timeout=5, decode_responses=True)
            after_keys = len(r.keys(f"{prefix}*"))
            r.close()
            result.add_check("Redis key count unchanged after dry-run",
                             after_keys == before_keys,
                             f"before={before_keys} after={after_keys}")
            result.add_check(f"No key under the dry run's own prefix '{prefix}'", after_keys == 0,
                             f"found {after_keys} key(s)")
        except Exception as exc:
            result.add_check("Redis post-check", False, _exc_type(exc))

    return run_suite(19, "Redis dry-run (no write)",
                     "05_redis_hash.json", own, orchestrator, gitea, gitea_url, extra_fn=_fn)


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
        resp = _client(orchestrator).post(
            f"/v1/webhooks/{datasource}",
            datasource=datasource,
            json=develop_payload,
            headers={"Content-Type": "application/json", "X-Gitea-Event": "push"},
        )
        task_id = resp.json().get("task_id") if resp.ok else None
        result.add_check("Develop-branch webhook accepted", task_id is not None, str(task_id))
        result.task_id = task_id
        if task_id:
            check_synced(result, wait_for_task(orchestrator, task_id))

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

    # The develop branch has to exist for the orchestrator to clone it, and the
    # rule has to map it to staging.
    gitea.ensure_branch(CONFIG_REPO, "develop")
    mapped = json.loads(json.dumps(rules))
    mapped[0]["branch_map"] = {"develop": "staging"}
    return run_suite(20, "branch_map: develop → staging collection",
                     "01_mongo_document.json", mapped, orchestrator, gitea, gitea_url, extra_fn=_fn,
                     envs=("staging",))


def suite_21_structured_prune(orchestrator, gitea, gitea_url, rules):
    """Structured prune config: prune: {enabled: true, dry_run: false}."""
    # Structured prune config, and a destination of its own, in the rules copy.
    modified = _variant(rules, destination_template="e2e-structured-prune-{env}",
                        prune={"enabled": True, "dry_run": False})

    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        # First write.
        _default_etl_check(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource)

        # Second write — confirms prune doesn't corrupt existing data.
        sha2 = gitea.get_head_sha(CONFIG_REPO)
        sync_and_wait(result, orchestrator, datasource, tenant, sha2, gitea_url,
                      label="Second push (structured prune): ")
        verify_mongo(rule, result)

    return run_suite(21, "Structured prune config",
                     "01_mongo_document.json", modified, orchestrator, gitea, gitea_url, extra_fn=_fn)


def suite_22_task_status_polling(orchestrator, gitea, gitea_url, rules):
    """Trigger a webhook then poll /v1/tasks/{task_id} until the task reports it has finished."""
    own = _variant(rules, destination_template="e2e-task-status-{env}")

    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        task_id = trigger_webhook(orchestrator, datasource, tenant, commit_sha, gitea_url)
        result.add_check("Webhook accepted (task_id returned)", task_id is not None, str(task_id))
        result.task_id = task_id
        if not task_id:
            return

        # Polling is the point of this suite: the endpoint answers PENDING / PROGRESS until the task has
        # a result, and the result's own state (not Celery's) says whether the sync succeeded.
        outcome = wait_for_task(orchestrator, task_id)
        result.add_check("Task reached a terminal state via polling", outcome.finished, outcome.said())
        check_synced(result, outcome)
        verify_mongo(rule, result)

    return run_suite(22, "Task status polling until terminal",
                     "01_mongo_document.json", own, orchestrator, gitea, gitea_url, extra_fn=_fn)


def _schema_map_documents(rule: dict) -> dict[str, dict]:
    """The documents the schema_map suite's rule wrote, by the file they hold (an empty dict when none)."""
    client = MongoClient(rule.get("mongo_uri", "mongodb://mongo:27017"), serverSelectionTimeoutMS=5000)
    try:
        col = client[rule.get("database", "vartrack")][rule["destination_template"]]
        return {d["_vt_file"]: d for d in col.find({}, {"_id": 0}) if "_vt_file" in d}
    finally:
        client.close()


def suite_23_schema_map(orchestrator, gitea, gitea_url, rules,
                        schemas_dir: str = "/demo/schemas/cue/teams"):
    """schema_map: one file name in two folders, each checked by its own schema; a mapping to a schema the
    schema repository does not have fails the file naming it, strict_validation or not, and writes nothing."""
    folders = {"team-a": "team-a/app-settings.yaml", "team-b": "team-b/app-settings.yaml"}
    schemas = {"team-a": "teams/team-a.cue", "team-b": "teams/team-b.cue"}
    own = _variant(rules, destination_template="e2e-schema-map", branch_as_env=False, strict_validation=True,
                   file_path_map=folders,
                   schema_map=[{"match": "team-a/**", "schema": schemas["team-a"]},
                               {"match": "team-b/**", "schema": schemas["team-b"]}])
    own[0].pop("file_name", None)       # two files, found by path: file_path_map

    def _fn(result, rule, orchestrator, gitea_url, commit_sha, tenant, datasource):
        # One name, two folders, two schemas: each file passes only the schema of its own folder.
        sha = gitea.get_head_sha(CONFIG_REPO)
        outcome = sync_and_wait(result, orchestrator, datasource, tenant, sha, gitea_url)
        if outcome is not None and outcome.finished:
            by_file = {f.get("file"): f for f in outcome.files}
            for team, path in folders.items():
                f = by_file.get(path, {})
                result.add_check(f"{path} was checked by {schemas[team]} (schema_map)",
                                 (f.get("validation"), f.get("schema"), f.get("schema_source"))
                                 == ("ok", schemas[team], f"schema_map[{list(folders).index(team)}]"),
                                 f"{f.get('validation')} {f.get('schema')} {f.get('schema_source')}")
        if HAS_MONGO:
            docs = _schema_map_documents(rule)
            result.add_check("Both files were written, one document each", sorted(docs) == sorted(folders.values()),
                             ", ".join(sorted(docs)) or "(none)")
            result.add_check("Each document holds its own folder's data",
                             all((docs.get(path) or {}).get("data", {}).get("owner") == team
                                 for team, path in folders.items()),
                             str({p: (d.get("data") or {}).get("owner") for p, d in docs.items()}))

        # A mapping to a schema nobody pushed fails that file, naming the entry, the schema and the commit.
        missing = _variant(own, schema_map=[{"match": "team-a/**", "schema": schemas["team-a"]},
                                            {"match": "team-b/**", "schema": "teams/nowhere.cue"}],
                           strict_validation=False)
        missing[0].pop("file_name", None)
        reset_destination(missing[0], ("team-a", "team-b"))
        result.schema_commit = gitea.push_bundle(missing)
        warmed = warm_schema(orchestrator, tenant)
        result.add_check("Schema registry warmed at the second bundle", warmed["schema_commit"] == result.schema_commit,
                         f"holds={warmed['schema_commit'][:12] or '(none)'} pushed={result.schema_commit[:12]}")
        task_id = trigger_webhook(orchestrator, datasource, tenant, sha, gitea_url)
        result.add_check("Webhook accepted for the missing schema", task_id is not None, str(task_id))
        if task_id:
            failed = wait_for_task(orchestrator, task_id)
            result.add_check("Task finished FAILED", failed.state == "FAILED", failed.said())
            if failed.finished:
                reasons = failed.message + " ".join(str(f.get("error", "")) for f in failed.files)
                result.add_check("The failure names the schema and the schema_map entry that sent the file there",
                                 "teams/nowhere.cue" in reasons and "schema_map[1]" in reasons, failed.message[:200])
                bad = [f for f in failed.files if f.get("file") == folders["team-b"]]
                result.add_check("The file with the missing schema was not synced",
                                 not any(f.get("status") == "ok" for f in bad), _files_summary(failed.files))
                check_schema_commit(result, failed)
        if HAS_MONGO:
            # The push's other file still syncs: one bad file fails that file, not its neighbours.
            docs = _schema_map_documents(rule)
            result.add_check("Only the file with a schema was written", sorted(docs) == [folders["team-a"]],
                             ", ".join(sorted(docs)) or "(none)")

    # The files and the schemas they are checked by are pushed before the suite's own bundle.
    try:
        for team, path in folders.items():
            gitea.push_schema_file(schemas[team], (Path(schemas_dir) / f"{team}.cue").read_bytes(),
                                   f"e2e: schema of {team}")
            gitea.push_file(CONFIG_REPO, path, f"owner: {team}\n".encode(), f"e2e: {path}")
    except Exception:
        failed = SuiteResult(23, "schema_map: a schema per path", "12_mongo_document_strategy.json",
                             rules[0]["datasource"])
        failed.error = traceback.format_exc()
        failed.add_check("The suite's files and schemas were pushed", False, "see the suite's error")
        failed.finish("fail")
        return failed

    return run_suite(23, "schema_map: a schema per path", "12_mongo_document_strategy.json", own,
                     orchestrator, gitea, gitea_url, extra_fn=_fn, envs=("team-a", "team-b"))


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
    # Every call the suites make to the orchestrator is signed with the demo's INTERNAL_AUTH_SECRET.
    args.orchestrator = OrchestratorClient(args.orchestrator, os.environ.get("INTERNAL_AUTH_SECRET", ""))

    Path(args.results_dir).mkdir(parents=True, exist_ok=True)

    gitea = GiteaClient(args.gitea, args.gitea_user, args.gitea_pass)

    banner("VarTrack E2E Test Runner — 23 Suites / 8 Sinks + CUE + Vault")
    run_started = datetime.now(timezone.utc).isoformat()
    global_t0 = time.monotonic()

    settings = load_settings(args.settings_dir)
    log(f"Loaded {len(settings)} setting files from {args.settings_dir}")

    s = settings
    mongo_doc   = s.get("01_mongo_document.json",  [{}])
    mongo_file  = s.get("02_mongo_file.json",       [{}])
    mongo_docs  = s.get("12_mongo_document_strategy.json", [{}])
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
    results.append(suite_01_mongo_doc_basic(args.orchestrator, gitea, args.gitea, mongo_docs))
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
    results.append(suite_18_unsigned_calls_refused(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_19_redis_dry_run(args.orchestrator, gitea, args.gitea, redis_hash))
    results.append(suite_20_branch_map(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_21_structured_prune(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_22_task_status_polling(args.orchestrator, gitea, args.gitea, mongo_doc))
    results.append(suite_23_schema_map(args.orchestrator, gitea, args.gitea, mongo_docs))

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
