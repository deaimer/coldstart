# Schemas

This directory contains machine-readable ColdStart metadata, evidence-report, proposal, and review schemas. Add schemas only after the corresponding human-readable specification has been reviewed and frozen.

## Evaluation-results schemas

These describe the shapes produced by `coldctl results` / `coldctl reports` (see [`docs/results.md`](../docs/results.md)):

- [`trial.schema.json`](trial.schema.json) — one normalized, ingested trial (provenance paths/hashes only, never trajectory contents).
- [`task_system_aggregate.schema.json`](task_system_aggregate.schema.json) — aggregate metrics for one (task, system) pair.
- [`public_report.schema.json`](public_report.schema.json) — redacted, shareable task report.
- [`private_report.schema.json`](private_report.schema.json) — full-detail task report, internal/reviewer use only.

## Evaluation-runner schemas

These describe the shapes produced by `coldctl eval` (see [`docs/evaluation-runner.md`](../docs/evaluation-runner.md)); none of them ever contain a credential value:

- [`evaluation_config.schema.json`](evaluation_config.schema.json) — a `coldctl eval` YAML configuration file.
- [`run_manifest.schema.json`](run_manifest.schema.json) — the immutable manifest frozen when a run is created.
- [`run_state.schema.json`](run_state.schema.json) — the mutable, atomically-rewritten run state.
- [`event_entry.schema.json`](event_entry.schema.json) — one line of a run's append-only event log.
