package config

import (
    "encoding/json"
    "fmt"
    "os"
)

type API struct {
    ListenAddress string `json:"listen_address"`
    StorageRoot   string `json:"storage_root"`
    StagingRoot   string `json:"staging_root"`
}

type Worker struct {
    StorageRoot   string `json:"storage_root"`
    StagingRoot   string `json:"staging_root"`
    PollInterval  int    `json:"poll_interval_ms"`
}

func LoadAPI(path string) (API, error) {
    var cfg API
    if err := load(path, &cfg); err != nil {
        return cfg, err
    }
    if cfg.ListenAddress == "" || cfg.StorageRoot == "" || cfg.StagingRoot == "" {
        return cfg, fmt.Errorf("api configuration is incomplete")
    }
    return cfg, nil
}

func LoadWorker(path string) (Worker, error) {
    var cfg Worker
    if err := load(path, &cfg); err != nil {
        return cfg, err
    }
    if cfg.StorageRoot == "" || cfg.StagingRoot == "" || cfg.PollInterval <= 0 {
        return cfg, fmt.Errorf("worker configuration is incomplete")
    }
    return cfg, nil
}

func load(path string, target any) error {
    raw, err := os.ReadFile(path)
    if err != nil {
        return fmt.Errorf("read config %s: %w", path, err)
    }
    if err := json.Unmarshal(raw, target); err != nil {
        return fmt.Errorf("decode config %s: %w", path, err)
    }
    return nil
}
