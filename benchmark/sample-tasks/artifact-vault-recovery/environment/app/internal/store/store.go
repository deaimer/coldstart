package store

import (
    "fmt"
    "os"
    "os/exec"
    "strconv"
    "strings"
    "time"
)

type Artifact struct {
    ID         string `json:"id"`
    Name       string `json:"name"`
    SHA256     string `json:"sha256"`
    SizeBytes  int64  `json:"size_bytes"`
    Status     string `json:"status"`
    LegacyPath string `json:"legacy_path,omitempty"`
    ObjectKey  string `json:"object_key,omitempty"`
}

type Store struct{}

func New() *Store {
    return &Store{}
}

func (s *Store) Ping() error {
    value, err := runSQL("SELECT 1")
    if err != nil {
        return err
    }
    if strings.TrimSpace(value) != "1" {
        return fmt.Errorf("unexpected database probe: %q", value)
    }
    return nil
}

func (s *Store) GetArtifact(id string) (Artifact, error) {
    query := fmt.Sprintf(`SELECT id::text, name, sha256, size_bytes::text, status,
        COALESCE(legacy_path, ''), COALESCE(object_key, '')
        FROM artifacts WHERE id = %s`, quote(id))
    output, err := runSQL(query)
    if err != nil {
        return Artifact{}, err
    }
    if strings.TrimSpace(output) == "" {
        return Artifact{}, fmt.Errorf("artifact not found")
    }
    return parseArtifact(output)
}

func (s *Store) ListArtifacts() ([]Artifact, error) {
    output, err := runSQL(`SELECT id::text, name, sha256, size_bytes::text, status,
        COALESCE(legacy_path, ''), COALESCE(object_key, '')
        FROM artifacts ORDER BY created_at, id`)
    if err != nil {
        return nil, err
    }
    if strings.TrimSpace(output) == "" {
        return []Artifact{}, nil
    }
    lines := strings.Split(strings.TrimSpace(output), "\n")
    artifacts := make([]Artifact, 0, len(lines))
    for _, line := range lines {
        artifact, err := parseArtifact(line)
        if err != nil {
            return nil, err
        }
        artifacts = append(artifacts, artifact)
    }
    return artifacts, nil
}

func (s *Store) FindByIdempotencyKey(key string) (string, error) {
    query := fmt.Sprintf(`SELECT artifact_id::text FROM upload_requests
        WHERE idempotency_key = %s`, quote(key))
    output, err := runSQL(query)
    if err != nil {
        return "", err
    }
    return strings.TrimSpace(output), nil
}

// CreateArtifact intentionally performs a check-then-insert sequence. Under
// concurrent retries, more than one artifact can be inserted before the
// idempotency-key constraint is reached.
func (s *Store) CreateArtifact(
    id string,
    key string,
    name string,
    sha256 string,
    size int64,
    objectKey string,
) (string, bool, error) {
    existing, err := s.FindByIdempotencyKey(key)
    if err != nil {
        return "", false, err
    }
    if existing != "" {
        return existing, false, nil
    }

    insertArtifact := fmt.Sprintf(`INSERT INTO artifacts
        (id, name, sha256, size_bytes, status, legacy_path, object_key)
        VALUES (%s, %s, %s, %d, 'pending', NULL, %s)`,
        quote(id), quote(name), quote(sha256), size, quote(objectKey))
    if _, err := runSQL(insertArtifact); err != nil {
        return "", false, err
    }

    time.Sleep(150 * time.Millisecond)

    insertRequest := fmt.Sprintf(`INSERT INTO upload_requests
        (idempotency_key, artifact_id) VALUES (%s, %s)`, quote(key), quote(id))
    if _, err := runSQL(insertRequest); err != nil {
        winner, lookupErr := s.FindByIdempotencyKey(key)
        if lookupErr == nil && winner != "" {
            return winner, false, nil
        }
        return "", false, err
    }
    return id, true, nil
}

func (s *Store) PendingArtifacts() ([]Artifact, error) {
    output, err := runSQL(`SELECT id::text, name, sha256, size_bytes::text, status,
        COALESCE(legacy_path, ''), COALESCE(object_key, '')
        FROM artifacts WHERE status = 'pending' ORDER BY created_at, id`)
    if err != nil {
        return nil, err
    }
    if strings.TrimSpace(output) == "" {
        return []Artifact{}, nil
    }
    lines := strings.Split(strings.TrimSpace(output), "\n")
    artifacts := make([]Artifact, 0, len(lines))
    for _, line := range lines {
        artifact, err := parseArtifact(line)
        if err != nil {
            return nil, err
        }
        artifacts = append(artifacts, artifact)
    }
    return artifacts, nil
}

func (s *Store) MarkReady(id string) error {
    query := fmt.Sprintf(`UPDATE artifacts SET status = 'ready' WHERE id = %s`, quote(id))
    _, err := runSQL(query)
    return err
}

func parseArtifact(line string) (Artifact, error) {
    fields := strings.Split(strings.TrimSpace(line), "\t")
    if len(fields) != 7 {
        return Artifact{}, fmt.Errorf("invalid artifact row with %d fields: %q", len(fields), line)
    }
    size, err := strconv.ParseInt(fields[3], 10, 64)
    if err != nil {
        return Artifact{}, fmt.Errorf("invalid artifact size: %w", err)
    }
    return Artifact{
        ID: fields[0], Name: fields[1], SHA256: fields[2], SizeBytes: size,
        Status: fields[4], LegacyPath: fields[5], ObjectKey: fields[6],
    }, nil
}

func quote(value string) string {
    return "'" + strings.ReplaceAll(value, "'", "''") + "'"
}

func runSQL(query string) (string, error) {
    cmd := exec.Command(
        "psql", "-X", "-qAt", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", query,
    )
    cmd.Env = os.Environ()
    output, err := cmd.CombinedOutput()
    if err != nil {
        return "", fmt.Errorf("database command failed: %w: %s", err, strings.TrimSpace(string(output)))
    }
    return strings.TrimSpace(string(output)), nil
}
