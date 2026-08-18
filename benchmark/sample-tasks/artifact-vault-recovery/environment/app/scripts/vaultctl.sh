#!/usr/bin/env bash
set -euo pipefail

case "${1:-status}" in
    build)
        cd /app
        go build -o /app/bin/artifact-vault ./cmd/vault
        ;;
    restart)
        cd /app
        go build -o /app/bin/artifact-vault ./cmd/vault
        supervisorctl -c /app/config/supervisord.conf restart artifact-api artifact-worker
        ;;
    status)
        supervisorctl -c /app/config/supervisord.conf status
        ;;
    logs)
        tail -n 100 /logs/artifacts/api.log /logs/artifacts/worker.log
        ;;
    *)
        echo "usage: vaultctl {build|restart|status|logs}" >&2
        exit 2
        ;;
esac
