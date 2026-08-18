# ColdStart Evaluation Results (Phase 1)

This describes `coldctl results` and `coldctl reports`: ingestion of completed
Harbor jobs into a normalized local SQLite store, aggregation, and
public/private report generation. Phase 1 only ingests and reports on
*already-completed* Harbor jobs; it does not run models, Harbor, or Docker.

## Data model

The store lives at `.coldstart/results.db` (gitignored; local and disposable —
delete it and re-ingest at any time). Schema is defined in
[`src/coldctl/results/db.py`](../src/coldctl/results/db.py).

| Table | Purpose |
|---|---|
| `benchmark_versions` | Distinct ColdStart tooling versions (`coldctl` package version) seen at ingestion time. |
| `tasks` / `task_versions` | Task identity (name) and versioned identity (version + content digest from `lock.json`), so results stay tied to an exact task revision. |
| `models` / `agents` / `systems` | The evaluated model, the agent harness, and the `<model>__<agent>` "system" pairing used throughout the CLI (e.g. `gpt-5.6-terra__terminus-2`). |
| `runs` | One row per ingested Harbor job directory. |
| `trials` | One row per trial directory inside a run: timing, strict pass, tokens, cost, exception info, source path + SHA-256. |
| `dimension_scores` | Every reward key returned by the verifier for a trial (the five diagnostic dimensions, `coldstart_pass`, and any future/unknown keys) — generic, so new metrics are never silently dropped. |
| `verifier_checks` | Per-check pass/fail results from `verifier/details.json`. |
| `artifact_references` | Provenance for every raw file touched during ingestion: `source_path` + `sha256` (+ size). **Never file contents.** The trajectory's full step-by-step transcript is not stored; only a small `metadata_json` summary (schema version, agent, token/cost totals, step count) plus its path and hash. |

### Strict pass vs. diagnostic dimensions

`trials.coldstart_pass` / `trials.strict_pass` are populated **only** from the
`coldstart_pass` reward key. The five diagnostic dimensions
(`functional`, `durability`, `state_safety`, `integrity`, `evidence`, see
[`constants.py`](../src/coldctl/results/constants.py)) are stored and averaged
separately and can never contribute partial credit to the strict pass rate —
this is enforced structurally: `aggregate.compute_aggregate` reads
`strict_pass_rate` exclusively from `trials.strict_pass`/`coldstart_pass`, and
`dimension_averages` iterates only over the fixed `DIMENSIONS` tuple.

### Trial failure vs. infrastructure exception

A trial that ran to completion and scored `coldstart_pass = 0` is a **task
failure** (`is_infra_exception = 0`). A trial where Harbor recorded a
structured `exception_info` (e.g. an environment/build crash) is an
**infrastructure exception** (`is_infra_exception = 1`); such trials have no
`coldstart_pass` value and are excluded from `scored_attempts` /
`strict_pass_rate`, matching the evaluation policy's guidance that
infrastructure failures should be documented and rerun rather than counted as
zero.

## Ingestion workflow

```bash
uv run coldctl results ingest jobs/2026-08-18__18-28-15
uv run coldctl results ingest jobs/2026-08-18__18-28-15 jobs/2026-08-18__18-46-48 ...
```

For each job directory: reads the job's `config.json`/`result.json`, discovers
trial subdirectories (any directory containing both `config.json` and
`result.json`), and for each trial reads `config.json`, `result.json`,
`lock.json` (task identity/digest), `verifier/details.json`, and
`agent/trajectory.json` metadata.

- **Idempotent:** every row is keyed by a stable natural key (run directory
  name, `<run_key>::<trial_name>`, `(task, digest)`, `(model, provider)`,
  `(agent, version)`, `<model>__<agent>`) and upserted with
  `INSERT ... ON CONFLICT ... DO UPDATE`. Re-running `ingest` on the same job
  updates existing rows rather than duplicating them.
- **Isolated per job:** each job is ingested inside its own SQLite
  `SAVEPOINT`. A malformed job (missing/invalid JSON, no trial directories)
  raises a clear error identifying the file and is rolled back without
  touching data already committed for other jobs in the same invocation.
- **Provenance:** every JSON file read is hashed (SHA-256) and its resolved
  path recorded in `artifact_references`; only that path and hash are stored
  for large artifacts (trajectories, logs) — never their contents.

### Runtime measurement

For a job containing exactly one trial, per-trial `runtime_sec` is derived
from the *job's* own `started_at`/`finished_at` envelope (`runtime_basis =
"run"`) — this matches what Harbor's own CLI reports as "Total runtime" and
captures dispatch/setup overhead the trial's internal clock does not. For
jobs with multiple trials (which may run concurrently), the trial's own
`started_at`/`finished_at` is used instead (`runtime_basis = "trial"`), since
the job envelope would not correspond to any single trial's duration.

## Inspection commands

```bash
uv run coldctl results list-runs [--json]
uv run coldctl results list-trials [--task NAME] [--system MODEL__AGENT] [--json]
uv run coldctl results show-run <run-key> [--json]
```

All three render Rich tables by default; pass `--json` for machine-readable output.

## Aggregation

`coldctl.results.aggregate.compute_aggregate(conn, task=..., system=...)`
computes, over all ingested trials for a `(task, system)` pair:

- strict pass rate (`coldstart_pass` only), pass/fail counts, and
  `scored_attempts` (attempts excluding infrastructure exceptions)
- the five dimension averages (diagnostic only)
- total/average cost, median runtime, token totals (when available)
- infrastructure exception count
- failed-check frequencies (how many trials each named verifier check failed in)
- task version(s) and benchmark version(s) covered

## Report generation

```bash
uv run coldctl reports task --task artifact-vault-recovery \
  --system gpt-5.6-terra__terminus-2 --visibility private --format markdown

uv run coldctl reports task --task artifact-vault-recovery \
  --system gpt-5.6-terra__terminus-2 --visibility public --format json
```

Public reports default to `reports/`; private reports default to
`results/private/` (already gitignored). Use `--output PATH` to override.

### Public/private data policy

**Private reports** may include: individual attempts, failed verifier-check
names, job/trial identifiers, raw artifact references (paths + hashes), and
detailed exception information. For reviewers/maintainers only.

**Public reports** may include: strict pass rate, dimension averages, number
of attempts, aggregated cost/runtime, model and agent configuration,
benchmark version, task content digest, evaluation date range, and high-level
failure/exception *totals*.

**Public reports must exclude**, and `build_public_report` does not have
access to: raw local filesystem paths, individual hidden verifier-check
names, oracle or verifier source, trajectory contents, API keys or other
credentials, private task contents, and any environment-variable values. This
is verified by tests in [`tests/test_reports.py`](../tests/test_reports.py).

## Reproducibility and provenance

Per the evaluation policy, every aggregate/report identifies: the benchmark
(`coldctl`) version, the exact task digest(s) (`sha256:...` from `lock.json`),
the agent name/version and model name/provider, agent settings (e.g.
`reasoning_effort`), trial count, and the evaluation date range. Raw source
files are never modified or deleted by ingestion; only their path and
SHA-256 hash are recorded, so any figure can be traced back to, and verified
against, the original Harbor job on disk.

## API keys are never stored

Ingestion never reads, prints, or persists API key or credential values.
Only `config.json`/`lock.json`/`result.json` structured fields are read
(model name, agent name, kwargs such as `reasoning_effort`, rewards, costs,
timings); none of these files contain credential material in Harbor's own
output. Environment variables are never read or stored by this system.
