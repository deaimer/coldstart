#!/usr/bin/env bash
set -euo pipefail

for _ in $(seq 1 60); do
    if pg_isready -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! pg_isready -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" >/dev/null 2>&1; then
    echo "PostgreSQL did not become ready" >&2
    exit 1
fi

rm -f /tmp/artifact-vault-supervisor.sock /tmp/artifact-vault-supervisord.pid
supervisord -c /app/config/supervisord.conf

exec "$@"
