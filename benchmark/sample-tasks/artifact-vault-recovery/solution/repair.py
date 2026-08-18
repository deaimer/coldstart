import os
from pathlib import Path


def replace_between(path: Path, start_marker: str, end_marker: str, replacement: str) -> None:
    text = path.read_text()
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    path.write_text(text[:start] + replacement.rstrip() + "\n\n" + text[end:])


root = Path(os.environ.get("ARTIFACT_VAULT_ROOT", "/app"))

store_path = root / "internal/store/store.go"
store_text = store_path.read_text().replace('    "time"\n', "")
store_path.write_text(store_text)

fixed_create = r'''
func (s *Store) CreateArtifact(
    id string,
    key string,
    name string,
    sha256 string,
    size int64,
    objectKey string,
) (string, bool, error) {
    query := fmt.Sprintf(`
        BEGIN;
        SELECT pg_advisory_xact_lock(hashtext(%s));
        WITH existing AS (
            SELECT artifact_id AS id, false AS created
            FROM upload_requests
            WHERE idempotency_key = %s
        ), inserted_artifact AS (
            INSERT INTO artifacts
                (id, name, sha256, size_bytes, status, legacy_path, object_key)
            SELECT %s, %s, %s, %d, 'pending', NULL, %s
            WHERE NOT EXISTS (SELECT 1 FROM existing)
            RETURNING id, true AS created
        ), chosen AS (
            SELECT id, created FROM existing
            UNION ALL
            SELECT id, created FROM inserted_artifact
            LIMIT 1
        ), request_row AS (
            INSERT INTO upload_requests(idempotency_key, artifact_id)
            SELECT %s, id FROM chosen
            ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
            RETURNING artifact_id
        )
        SELECT request_row.artifact_id::text, chosen.created::text
        FROM request_row JOIN chosen ON chosen.id = request_row.artifact_id;
        COMMIT;`,
        quote(key), quote(key), quote(id), quote(name), quote(sha256), size,
        quote(objectKey), quote(key),
    )
    output, err := runSQL(query)
    if err != nil {
        return "", false, err
    }
    fields := strings.Split(strings.TrimSpace(output), "\t")
    if len(fields) != 2 || fields[0] == "" {
        return "", false, fmt.Errorf("invalid idempotent insert result: %q", output)
    }
    return fields[0], fields[1] == "true" || fields[1] == "t", nil
}
'''
replace_between(
    store_path,
    "func (s *Store) CreateArtifact(",
    "func (s *Store) PendingArtifacts(",
    fixed_create,
)

api_path = root / "internal/api/api.go"
fixed_ready = r'''
func (s *Server) ready(w http.ResponseWriter, _ *http.Request) {
    if err := s.store.Ping(); err != nil {
        writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "not_ready", "error": err.Error()})
        return
    }
    info, err := os.Stat(s.cfg.StorageRoot)
    if err != nil || !info.IsDir() {
        writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "not_ready", "error": "storage root unavailable"})
        return
    }
    probe, err := os.CreateTemp(s.cfg.StorageRoot, ".readiness-")
    if err != nil {
        writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "not_ready", "error": "storage root is not writable"})
        return
    }
    probeName := probe.Name()
    _ = probe.Close()
    _ = os.Remove(probeName)
    writeJSON(w, http.StatusOK, map[string]any{"status": "ready"})
}
'''
replace_between(
    api_path,
    "func (s *Server) ready(",
    "func (s *Server) artifacts(",
    fixed_ready,
)

(root / "config/worker.json").write_text(
    '''{
  "storage_root": "/var/lib/artifact-vault/blobs",
  "staging_root": "/var/lib/artifact-vault/staging",
  "poll_interval_ms": 100
}
'''
)
