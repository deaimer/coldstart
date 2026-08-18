package worker

import (
    "log"
    "os"
    "path/filepath"
    "time"

    "coldstart.dev/artifact-vault/internal/config"
    "coldstart.dev/artifact-vault/internal/store"
)

func Run(path string) error {
    cfg, err := config.LoadWorker(path)
    if err != nil {
        return err
    }
    database := store.New()
    ticker := time.NewTicker(time.Duration(cfg.PollInterval) * time.Millisecond)
    defer ticker.Stop()
    log.Printf("artifact worker started with storage root %s", cfg.StorageRoot)
    for {
        if err := process(database, cfg); err != nil {
            log.Printf("worker iteration failed: %v", err)
        }
        <-ticker.C
    }
}

func process(database *store.Store, cfg config.Worker) error {
    artifacts, err := database.PendingArtifacts()
    if err != nil {
        return err
    }
    for _, artifact := range artifacts {
        source := filepath.Join(cfg.StagingRoot, artifact.ID+".upload")
        if _, err := os.Stat(source); err != nil {
            if os.IsNotExist(err) {
                continue
            }
            return err
        }
        target := filepath.Join(cfg.StorageRoot, artifact.ObjectKey)
        if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
            return err
        }
        if err := os.Rename(source, target); err != nil {
            return err
        }
        if err := database.MarkReady(artifact.ID); err != nil {
            return err
        }
        log.Printf("stored artifact %s at %s", artifact.ID, target)
    }
    return nil
}
