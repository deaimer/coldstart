# Schemas

This directory contains machine-readable ColdStart metadata, evidence-report, proposal, and review schemas. Add schemas only after the corresponding human-readable specification has been reviewed and frozen.

## Evaluation-results schemas

These describe the shapes produced by `coldctl results` / `coldctl reports` (see [`docs/results.md`](../docs/results.md)):

- [`trial.schema.json`](trial.schema.json) — one normalized, ingested trial (provenance paths/hashes only, never trajectory contents).
- [`task_system_aggregate.schema.json`](task_system_aggregate.schema.json) — aggregate metrics for one (task, system) pair.
- [`public_report.schema.json`](public_report.schema.json) — redacted, shareable task report.
- [`private_report.schema.json`](private_report.schema.json) — full-detail task report, internal/reviewer use only.
