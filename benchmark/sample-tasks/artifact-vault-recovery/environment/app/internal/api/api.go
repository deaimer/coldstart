package api

import (
    "crypto/rand"
    "crypto/sha256"
    "encoding/base64"
    "encoding/hex"
    "encoding/json"
    "fmt"
    "io"
    "log"
    "net/http"
    "os"
    "path/filepath"
    "regexp"
    "strings"

    "coldstart.dev/artifact-vault/internal/config"
    "coldstart.dev/artifact-vault/internal/store"
)

var validName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

type Server struct {
    cfg   config.API
    store *store.Store
}

type uploadRequest struct {
    Name          string `json:"name"`
    ContentBase64 string `json:"content_base64"`
    SHA256        string `json:"sha256"`
}

func Run(path string) error {
    cfg, err := config.LoadAPI(path)
    if err != nil {
        return err
    }
    server := &Server{cfg: cfg, store: store.New()}
    mux := http.NewServeMux()
    mux.HandleFunc("/health/live", server.live)
    mux.HandleFunc("/health/ready", server.ready)
    mux.HandleFunc("/v1/artifacts", server.artifacts)
    mux.HandleFunc("/v1/artifacts/", server.artifact)
    log.Printf("artifact api listening on %s", cfg.ListenAddress)
    return http.ListenAndServe(cfg.ListenAddress, requestLog(mux))
}

func (s *Server) live(w http.ResponseWriter, _ *http.Request) {
    writeJSON(w, http.StatusOK, map[string]any{"status": "alive"})
}

// ready only probes PostgreSQL, so it reports healthy even when the configured
// storage root is absent or unusable.
func (s *Server) ready(w http.ResponseWriter, _ *http.Request) {
    if err := s.store.Ping(); err != nil {
        writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "not_ready", "error": err.Error()})
        return
    }
    writeJSON(w, http.StatusOK, map[string]any{"status": "ready"})
}

func (s *Server) artifacts(w http.ResponseWriter, r *http.Request) {
    switch r.Method {
    case http.MethodGet:
        items, err := s.store.ListArtifacts()
        if err != nil {
            writeError(w, http.StatusInternalServerError, err)
            return
        }
        writeJSON(w, http.StatusOK, map[string]any{"artifacts": items})
    case http.MethodPost:
        s.create(w, r)
    default:
        w.Header().Set("Allow", "GET, POST")
        writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"error": "method not allowed"})
    }
}

func (s *Server) create(w http.ResponseWriter, r *http.Request) {
    key := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
    if key == "" || len(key) > 128 {
        writeJSON(w, http.StatusBadRequest, map[string]any{"error": "valid Idempotency-Key required"})
        return
    }
    body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 2<<20))
    if err != nil {
        writeError(w, http.StatusBadRequest, err)
        return
    }
    var request uploadRequest
    if err := json.Unmarshal(body, &request); err != nil {
        writeError(w, http.StatusBadRequest, err)
        return
    }
    if !validName.MatchString(request.Name) {
        writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid artifact name"})
        return
    }
    content, err := base64.StdEncoding.DecodeString(request.ContentBase64)
    if err != nil {
        writeError(w, http.StatusBadRequest, err)
        return
    }
    digest := sha256.Sum256(content)
    actualSHA := hex.EncodeToString(digest[:])
    if request.SHA256 != actualSHA {
        writeJSON(w, http.StatusBadRequest, map[string]any{"error": "sha256 mismatch"})
        return
    }

    desiredID, err := newUUID()
    if err != nil {
        writeError(w, http.StatusInternalServerError, err)
        return
    }
    if err := os.MkdirAll(s.cfg.StagingRoot, 0o755); err != nil {
        writeError(w, http.StatusInternalServerError, err)
        return
    }
    stagedPath := filepath.Join(s.cfg.StagingRoot, desiredID+".upload")
    if err := os.WriteFile(stagedPath, content, 0o644); err != nil {
        writeError(w, http.StatusInternalServerError, err)
        return
    }
    objectKey := filepath.Join("objects", desiredID[:2], desiredID+".blob")
    actualID, created, err := s.store.CreateArtifact(
        desiredID, key, request.Name, actualSHA, int64(len(content)), objectKey,
    )
    if err != nil {
        _ = os.Remove(stagedPath)
        writeError(w, http.StatusInternalServerError, err)
        return
    }
    status := http.StatusAccepted
    if !created {
        _ = os.Remove(stagedPath)
        status = http.StatusOK
    }
    writeJSON(w, status, map[string]any{"id": actualID, "created": created})
}

func (s *Server) artifact(w http.ResponseWriter, r *http.Request) {
    path := strings.TrimPrefix(r.URL.Path, "/v1/artifacts/")
    parts := strings.Split(path, "/")
    if len(parts) != 2 || parts[0] == "" {
        writeJSON(w, http.StatusNotFound, map[string]any{"error": "not found"})
        return
    }
    item, err := s.store.GetArtifact(parts[0])
    if err != nil {
        writeJSON(w, http.StatusNotFound, map[string]any{"error": "artifact not found"})
        return
    }
    switch parts[1] {
    case "meta":
        writeJSON(w, http.StatusOK, item)
    case "content":
        if item.Status != "ready" {
            writeJSON(w, http.StatusConflict, map[string]any{"error": "artifact is not ready"})
            return
        }
        content, err := os.ReadFile(filepath.Join(s.cfg.StorageRoot, item.ObjectKey))
        if err != nil {
            writeJSON(w, http.StatusNotFound, map[string]any{"error": "artifact bytes unavailable"})
            return
        }
        w.Header().Set("Content-Type", "application/octet-stream")
        w.Header().Set("X-Artifact-SHA256", item.SHA256)
        w.WriteHeader(http.StatusOK)
        _, _ = w.Write(content)
    default:
        writeJSON(w, http.StatusNotFound, map[string]any{"error": "not found"})
    }
}

func newUUID() (string, error) {
    raw := make([]byte, 16)
    if _, err := rand.Read(raw); err != nil {
        return "", err
    }
    raw[6] = (raw[6] & 0x0f) | 0x40
    raw[8] = (raw[8] & 0x3f) | 0x80
    return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
        raw[0:4], raw[4:6], raw[6:8], raw[8:10], raw[10:16]), nil
}

func requestLog(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        log.Printf("%s %s", r.Method, r.URL.Path)
        next.ServeHTTP(w, r)
    })
}

func writeError(w http.ResponseWriter, status int, err error) {
    writeJSON(w, status, map[string]any{"error": err.Error()})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(status)
    _ = json.NewEncoder(w).Encode(value)
}
