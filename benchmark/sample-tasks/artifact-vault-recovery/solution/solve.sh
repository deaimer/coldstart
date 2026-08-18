#!/usr/bin/env bash
set -euo pipefail

cd /app
python3 /solution/repair.py

psql -X -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
UPDATE artifacts
SET object_key = legacy_path
WHERE status = 'ready'
  AND legacy_path IS NOT NULL
  AND (object_key IS NULL OR object_key = '');
COMMIT;
SQL

make restart

for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8080/health/ready >/dev/null; then
        break
    fi
    sleep 1
done
curl -fsS http://127.0.0.1:8080/health/ready >/dev/null

python3 - <<'PY'
import json
from pathlib import Path

report = {
    "diagnosis": "The v2 migration ledger was committed before legacy object keys were backfilled; the worker wrote new blobs to a different root than the API; readiness ignored storage; and idempotency used a race-prone check-then-insert sequence.",
    "changes": "Backfilled legacy object keys without replacing IDs or bytes, aligned API and worker storage roots, added storage-aware readiness, and serialized idempotent reservation with a PostgreSQL advisory transaction lock.",
    "tests_performed": "Rebuilt and restarted both processes, confirmed readiness, and retained the seeded database and blob directory for verifier durability checks.",
    "remaining_risks": "The sample service shells out to psql and is intended for benchmark evaluation rather than production deployment.",
    "evidence_files": [
        "/logs/artifacts/api.log",
        "/logs/artifacts/worker.log",
        "/app/internal/store/store.go",
        "/app/config/worker.json"
    ]
}
Path("/app/coldstart-report.json").write_text(json.dumps(report, indent=2) + "\n")
PY
