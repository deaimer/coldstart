# Artifact Vault Recovery

This public ColdStart sample evaluates durable repair of a stateful Go service backed by PostgreSQL. The environment is intentionally faulty. The instruction describes the required outcome; this file documents only the operator surface.

## Services

- Artifact API: `http://127.0.0.1:8080`
- PostgreSQL: host `postgres`, database/user/password `vault`
- API source: `/app/internal/api`
- Store source: `/app/internal/store`
- Worker source: `/app/internal/worker`
- Runtime configuration: `/app/config`
- Service logs: `/logs/artifacts`

## API

- `GET /health/live`
- `GET /health/ready`
- `GET /v1/artifacts`
- `GET /v1/artifacts/{id}/meta`
- `GET /v1/artifacts/{id}/content`
- `POST /v1/artifacts` with an `Idempotency-Key` header and JSON body:

```json
{
  "name": "release-bundle",
  "content_base64": "aGVsbG8K",
  "sha256": "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
}
```

## Operator commands

```bash
cd /app
make build
make restart
make status
make logs
```

The test harness may restart both processes. A repair that only changes in-memory state will not pass.
