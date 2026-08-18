#!/usr/bin/env python3
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid


BASE = "http://127.0.0.1:8080"
LOG_DIR = Path("/logs/verifier")
STORAGE_ROOT = Path("/var/lib/artifact-vault/blobs")

SEEDS = {
    "11111111-1111-4111-8111-111111111111": b"coldstart-seed: compiler-linux-amd64 v1 build\n",
    "22222222-2222-4222-8222-222222222222": b"coldstart-seed: release-manifest 2026.08 stable\n",
}

details: dict[str, dict[str, object]] = {}


def record(name: str, function) -> bool:
    try:
        value = function()
        passed = bool(value)
        details[name] = {"passed": passed, "value": value}
        return passed
    except Exception as exc:  # verifier should report every independent failure
        details[name] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        return False


def request(path: str, *, method: str = "GET", body: bytes | None = None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def json_request(path: str, *, method: str = "GET", payload=None, headers=None):
    encoded = None
    request_headers = dict(headers or {})
    if payload is not None:
        encoded = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    status, raw, response_headers = request(
        path, method=method, body=encoded, headers=request_headers
    )
    data = json.loads(raw.decode()) if raw else {}
    return status, data, response_headers


def sql(query: str) -> str:
    result = subprocess.run(
        ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-c", query],
        check=True,
        text=True,
        capture_output=True,
        env=os.environ.copy(),
    )
    return result.stdout.strip()


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def wait_ready(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _, _ = request("/health/ready")
        if status == 200:
            return True
        time.sleep(0.25)
    return False


def wait_artifact(artifact_id: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, data, _ = json_request(f"/v1/artifacts/{artifact_id}/meta")
        if status == 200 and data.get("status") == "ready":
            return True
        time.sleep(0.2)
    return False


def content_matches(artifact_id: str, expected: bytes) -> bool:
    status, raw, headers = request(f"/v1/artifacts/{artifact_id}/content")
    return (
        status == 200
        and raw == expected
        and headers.get("X-Artifact-Sha256", headers.get("X-Artifact-SHA256", ""))
        == hashlib.sha256(expected).hexdigest()
    )


def upload(name: str, content: bytes, key: str):
    payload = {
        "name": name,
        "content_base64": base64.b64encode(content).decode(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    status, data, _ = json_request(
        "/v1/artifacts",
        method="POST",
        payload=payload,
        headers={"Idempotency-Key": key},
    )
    if status not in (200, 202) or not data.get("id"):
        raise AssertionError(f"upload failed with status {status}: {data}")
    return data["id"], status, bool(data.get("created"))


LOG_DIR.mkdir(parents=True, exist_ok=True)

initial_ready = record("initial_readiness", wait_ready)

seed_results = []
for seed_id, expected in SEEDS.items():
    seed_results.append(
        record(f"seed_content_{seed_id[:8]}", lambda i=seed_id, e=expected: content_matches(i, e))
    )
seeds_ok = all(seed_results)

migration_ok = record(
    "legacy_backfill",
    lambda: sql(
        "SELECT count(*) FROM artifacts WHERE legacy_path IS NOT NULL AND object_key = legacy_path"
    )
    == "2",
)
seed_state_ok = record(
    "seed_state_preserved",
    lambda: sql(
        "SELECT count(*) FROM artifacts WHERE "
        "(id = '11111111-1111-4111-8111-111111111111' AND sha256 = "
        "'578a54725b43ba85b67852a03c56a5f2acc2d7d707492a1162d7228f9b6b00a8') OR "
        "(id = '22222222-2222-4222-8222-222222222222' AND sha256 = "
        "'e43d2d05548643a55a2023ba830ed34abeabf56cae369f510dac5bdb8cd50c5e')"
    )
    == "2",
)

normal_content = b"coldstart verifier durable upload\n"
normal_id: str | None = None


def normal_upload_check() -> bool:
    global normal_id
    normal_id, _, _ = upload(
        "coldstart-durable-upload", normal_content, "normal-" + uuid.uuid4().hex
    )
    return wait_artifact(normal_id) and content_matches(normal_id, normal_content)


normal_upload_ok = record("new_upload_retrievable", normal_upload_check)

concurrent_content = b"coldstart idempotency race payload\n"
concurrent_name = "concurrent-" + uuid.uuid4().hex[:12]
concurrent_key = "idem-" + uuid.uuid4().hex
concurrent_id: str | None = None


def concurrent_upload_check() -> bool:
    global concurrent_id
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(upload, concurrent_name, concurrent_content, concurrent_key)
            for _ in range(8)
        ]
        results = [future.result() for future in futures]
    ids = {result[0] for result in results}
    if len(ids) != 1:
        return False
    concurrent_id = next(iter(ids))
    if not wait_artifact(concurrent_id):
        return False
    artifact_rows = sql(f"SELECT count(*) FROM artifacts WHERE name = {quote(concurrent_name)}")
    request_rows = sql(
        f"SELECT count(*) FROM upload_requests WHERE idempotency_key = {quote(concurrent_key)}"
    )
    return (
        artifact_rows == "1"
        and request_rows == "1"
        and content_matches(concurrent_id, concurrent_content)
    )


concurrent_ok = record("concurrent_idempotency", concurrent_upload_check)

no_orphans_ok = record(
    "no_orphan_uploads",
    lambda: sql(
        "SELECT count(*) FROM artifacts a LEFT JOIN upload_requests u ON u.artifact_id = a.id "
        "WHERE a.legacy_path IS NULL AND u.artifact_id IS NULL"
    )
    == "0",
)


def restart_check() -> bool:
    subprocess.run(
        [
            "supervisorctl",
            "-c",
            "/app/config/supervisord.conf",
            "restart",
            "artifact-api",
            "artifact-worker",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if not wait_ready():
        return False
    checks = [content_matches(seed_id, content) for seed_id, content in SEEDS.items()]
    if normal_id:
        checks.append(content_matches(normal_id, normal_content))
    else:
        checks.append(False)
    if concurrent_id:
        checks.append(content_matches(concurrent_id, concurrent_content))
    else:
        checks.append(False)
    return all(checks)


durability_ok = record("restart_durability", restart_check)


def truthful_readiness_check() -> bool:
    backup = STORAGE_ROOT.with_name(STORAGE_ROOT.name + ".coldstart-offline")
    if backup.exists():
        raise AssertionError(f"unexpected backup path exists: {backup}")
    shutil.copytree(STORAGE_ROOT, backup)
    shutil.rmtree(STORAGE_ROOT)
    try:
        deadline = time.time() + 5
        observed_unready = False
        while time.time() < deadline:
            status, _, _ = request("/health/ready")
            if status == 503:
                observed_unready = True
                break
            time.sleep(0.1)
    finally:
        if STORAGE_ROOT.exists():
            shutil.rmtree(STORAGE_ROOT)
        shutil.copytree(backup, STORAGE_ROOT)
        shutil.rmtree(backup)
    return observed_unready and wait_ready()


readiness_ok = record("storage_aware_readiness", truthful_readiness_check)


def report_check() -> bool:
    report_path = Path("/app/coldstart-report.json")
    report = json.loads(report_path.read_text())
    text_fields = ["diagnosis", "changes", "tests_performed", "remaining_risks"]
    if not all(isinstance(report.get(field), str) and len(report[field].strip()) >= 20 for field in text_fields):
        return False
    evidence = report.get("evidence_files")
    return (
        isinstance(evidence, list)
        and len(evidence) > 0
        and all(isinstance(path, str) and Path(path).exists() for path in evidence)
    )


evidence_ok = record("evidence_report", report_check)

functional_checks = [initial_ready, seeds_ok, normal_upload_ok, concurrent_ok]
state_checks = [migration_ok, seed_state_ok, no_orphans_ok]
integrity_checks = [no_orphans_ok, readiness_ok]

functional = sum(functional_checks) / len(functional_checks)
state_safety = sum(state_checks) / len(state_checks)
integrity = sum(integrity_checks) / len(integrity_checks)
durability = 1.0 if durability_ok else 0.0
evidence = 1.0 if evidence_ok else 0.0
coldstart_pass = 1.0 if all(
    functional_checks + state_checks + integrity_checks + [durability_ok, evidence_ok]
) else 0.0

rewards = {
    "coldstart_pass": coldstart_pass,
    "functional": functional,
    "durability": durability,
    "state_safety": state_safety,
    "evidence": evidence,
    "integrity": integrity,
}

(LOG_DIR / "reward.json").write_text(json.dumps(rewards, sort_keys=True) + "\n")
(LOG_DIR / "details.json").write_text(json.dumps(details, indent=2, sort_keys=True) + "\n")
print(json.dumps({"rewards": rewards, "details": details}, indent=2, sort_keys=True))
