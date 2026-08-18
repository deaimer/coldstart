package main

import (
    "fmt"
    "log"
    "os"

    "coldstart.dev/artifact-vault/internal/api"
    "coldstart.dev/artifact-vault/internal/worker"
)

func main() {
    if len(os.Args) != 2 {
        fmt.Fprintln(os.Stderr, "usage: artifact-vault {api|worker}")
        os.Exit(2)
    }
    var err error
    switch os.Args[1] {
    case "api":
        err = api.Run(envOr("VAULT_API_CONFIG", "/app/config/api.json"))
    case "worker":
        err = worker.Run(envOr("VAULT_WORKER_CONFIG", "/app/config/worker.json"))
    default:
        fmt.Fprintln(os.Stderr, "usage: artifact-vault {api|worker}")
        os.Exit(2)
    }
    if err != nil {
        log.Fatal(err)
    }
}

func envOr(name string, fallback string) string {
    if value := os.Getenv(name); value != "" {
        return value
    }
    return fallback
}
