# ColdStart Evaluation Runner (Phase 2)

`coldctl eval` is a safe, resumable, provider-agnostic automated evaluation
runner built on top of Harbor and the Phase 1 results system
([`docs/results.md`](results.md)). It plans and executes multi-trial
evaluations from a versioned YAML configuration, enforcing a budget,
classifying failures conservatively, and generating Phase 1 reports
automatically when a run finishes.

**ColdStart never reads, prints, or stores an API-key value.** Configuration
files reference credentials only by *environment-variable name*
(`api_key_env: OPENAI_API_KEY`); ColdStart checks only whether that variable
is set, never what it contains, and the real Harbor invocation inherits the
process environment directly rather than having credentials rebuilt or
logged anywhere. See "Credential handling" below.

## Evaluation configuration

Configs live under [`configs/evaluations/`](../configs/evaluations/) as
YAML, validated against
[`schemas/evaluation_config.schema.json`](../schemas/evaluation_config.schema.json).
See
[`configs/evaluations/artifact-vault-recovery.terra.yaml`](../configs/evaluations/artifact-vault-recovery.terra.yaml)
for a complete example. Shape:

```yaml
id: artifact-vault-recovery-terra-baseline
description: Five-trial baseline of gpt-5.6-terra via terminus-2.
benchmark_version: "0.1.0"
status: development        # or "official"

tasks:
  - benchmark/sample-tasks/artifact-vault-recovery

systems:
  - provider: openai
    model: openai/gpt-5.6-terra   # exact model identifier
    agent: terminus-2
    environment: docker
    agent_kwargs:                 # generic -- any agent keyword argument
      reasoning_effort: medium
      use_responses_api: true
      max_turns: 30
    api_key_env: OPENAI_API_KEY   # env-var NAME only, never a value
    trials_per_task: 5
    estimated_cost_per_trial_usd: 0.30   # optional fallback estimate

execution:
  max_concurrent_trials: 1     # keep at 1 unless you have verified higher concurrency
  max_infra_retries: 2
  max_budget_usd: 2.00

reports:
  private:
    enabled: true
    path: .coldstart/private-reports   # default; must not resolve under reports/
  public:
    enabled: true
    path: reports/generated            # default; must not resolve under .coldstart/ or results/private/
```

`status: official` additionally requires a clean, committed git worktree
(see "Run lifecycle" below).

## Commands

```bash
uv run coldctl eval validate <config>
uv run coldctl eval plan <config> [--allow-dirty] [--json]
uv run coldctl eval run <config> --yes [--allow-dirty] [--acknowledge-unestimated-cost]
uv run coldctl eval status <run-id> [--json]
uv run coldctl eval resume <run-id> --yes [--acknowledge-unestimated-cost]
```

### `eval validate`

Read-only and zero-cost. Never creates a run or invokes Harbor. Checks:

- the config parses as YAML and matches the schema (unknown fields and
  invalid values are rejected)
- no secret-shaped value is embedded in the config (only environment-variable
  *names* are permitted for credentials)
- every task path exists and passes ColdStart's existing task-structure
  validator (the same one behind `coldctl validate`)
- `git`, `harbor`, and (when any system uses `environment: docker`) `docker`
  are present on `PATH`
- every `api_key_env` variable is **set** -- reporting only presence/absence,
  never printing a value
- model/agent/trial-count/concurrency/budget/retry values are valid
- `reports.public.path` cannot resolve inside private storage
  (`.coldstart/`, `results/private/`), and `reports.private.path` cannot
  resolve inside the public reports root (`reports/`)

### `eval plan`

Deterministic and read-only; never invokes Harbor or spends money.

- Expands every (task × system × attempt) into a `TrialSpec` with a stable
  `trial_id` derived from a hash of the frozen config content, the task
  path, the system key, and the attempt number -- re-planning an unchanged
  config always yields identical trial IDs.
- Records the current git commit and whether the working tree is dirty.
- Recursively hashes each task directory's file contents (excluding VCS/cache
  directories) to detect drift later at resume time.
- Estimates total cost with this priority:
  1. **historical** -- Phase 1's SQLite store has prior trials for the same
     (task, system); their average cost × planned trial count.
  2. **configured_estimate** -- `estimated_cost_per_trial_usd` × planned
     trial count.
  3. **unavailable** -- no basis for an estimate.
- An `official` evaluation from a dirty/unverifiable worktree raises unless
  `--allow-dirty` is passed, in which case the plan is produced but marked
  `unverified: true`. `development` evaluations never require `--allow-dirty`
  but are still marked unverified when the tree is dirty.
- `--json` emits the full machine-readable plan.

### `eval run`

- **Without `--yes`**: prints the plan and exits. No run directory is
  created; Harbor is never invoked; nothing is spent. This is the default.
- **With `--yes`**: rechecks credentials/commands, applies the budget gate,
  freezes the manifest, and executes trial-by-trial.
- If a cost estimate is available and exceeds `max_budget_usd`, the run is
  refused before anything is created.
- If no estimate is available at all, both `--yes` **and**
  `--acknowledge-unestimated-cost` are required.
- Before *each* trial launch, if `actual_cost_usd` has already reached
  `max_budget_usd`, the run stops (paused) rather than starting another.
- `Ctrl+C` is caught; the run is atomically marked `paused` and can be
  continued later with `eval resume`.
- A run's manifest is never overwritten, and a completed Harbor job
  directory is never deleted.

Harbor is invoked once per planned trial (see "Why one Harbor invocation per
trial" below), with an explicit argument array built from
`coldctl.eval.harbor_runner.build_harbor_invocation` -- never a shell string,
so nothing in a config value can be interpreted as shell syntax. The real
subprocess inherits the parent environment verbatim (`env=None`); a
credential is never placed on the command line and never rebuilt from parts.

### `eval status`

Shows run ID/status, model/agent, benchmark version, git commit,
planned/completed/pending trial counts, pass/failure/invalid-attempt counts,
accumulated cost and remaining budget, elapsed time, the last event, and
report-generation status. Supports `--json`.

### `eval resume`

- Reloads the *existing, frozen* manifest -- a resume never regenerates a plan.
- Re-verifies the config hash, git commit, and every task hash still match
  what the manifest recorded; any drift refuses to resume (task content
  changes are called out specifically).
- Only pending or infra-retry-scheduled trials are (re)run; already
  `passed`/`failed`/`infra_invalid_exhausted` trials are never rerun.
- Requires `--yes`; rechecks the budget and credentials exactly as a fresh
  run does.
- Preserves the original run ID.
- Safe to invoke twice: resuming an already-completed/failed run is a no-op.

## Why one Harbor invocation per trial

Rather than using Harbor's own `--n-attempts` (multiple trials inside one
job), ColdStart invokes `harbor run` once per planned trial, with an
explicit `--job-name`/`--jobs-dir` it chooses itself. This means the exact
job directory a trial will land in is always known in advance -- no
directory-discovery heuristics are needed -- and each trial can be budgeted,
classified, retried, and resumed completely independently. This trades a few
extra process launches for materially safer resumability and budgeting.

## Retry classification

Every completed (or failed-to-complete) trial is classified into exactly one
outcome (see [`src/coldctl/eval/classifier.py`](../src/coldctl/eval/classifier.py)):

| Outcome | Examples | Retried? |
|---|---|---|
| **passed** / **failed** | Verifier scored `coldstart_pass`; also model timeout, max-turn exhaustion, refusal, agent mistakes, missing required output, tool misuse -- anything the agent/verifier pipeline completed and scored | Never (each is its own independently-scheduled trial) |
| **infra_invalid** | Harbor setup/build failure, Docker startup failure, environment healthcheck failure before the agent began, a rate-limit/provider outage that produced no usable trajectory, a corrupt or missing Harbor result | Yes, up to `max_infra_retries` |
| **auth_error** | Authentication/invalid-API-key signals in the exception or process output | **Never** -- stops the run immediately for human correction |
| **unknown** | Any exception type/message the classifier doesn't recognize | **Never** -- pauses the run for human review (conservative default) |

The classifier is intentionally conservative: an unrecognized exception is
never assumed retry-safe. Every classification records a `reason` code and
redacted `evidence` string in the trial's state.

## Frozen run manifest and mutable state

```text
.coldstart/runs/<run-id>/
├── manifest.json   # written once; never overwritten (see schemas/run_manifest.schema.json)
├── state.json      # rewritten atomically on every change (see schemas/run_state.schema.json)
├── events.jsonl     # append-only event log (see schemas/event_entry.schema.json)
├── logs/            # one redacted stdout/stderr capture per Harbor attempt
├── artifacts/        # reserved
└── harbor_jobs/       # each trial's own Harbor job directory (never deleted)
```

The manifest records: run ID, config hash, a sanitized config snapshot, git
commit + dirty status, benchmark version, per-task content hashes, every
expanded trial specification, the distinct system/model/agent
configurations used, creation timestamp (UTC), expected trial count,
configured budget, the cost-estimation method, and a schema version. It
never contains a credential value.

State tracks: overall status (`planned` / `running` / `completed` / `failed`
/ `paused` / `cancelled`), per-trial status/attempts/Harbor job
directories/outcome reason/evidence/cost, completed and pending trial ID
lists, total invalid-infrastructure-attempt count, per-trial retry counts,
accumulated actual cost, per-trial ingestion status, and report-generation
status. Every manifest/state write goes through
`coldctl.eval.manifest.atomic_write_json`: content is written to a sibling
temp file, `fsync`ed, then `os.replace`d onto the destination, so a reader
never observes a partial write.

## Interruption and resume

Pressing `Ctrl+C` (or a `KeyboardInterrupt` from any cause) during
`eval run`/`eval resume` is caught: the run is atomically marked `paused`
and a `run_paused` event is appended. A trial that was mid-flight when the
interrupt landed keeps whatever status it last had on disk (typically
`running`, since we cannot know its true outcome); the *next* execution
entry -- i.e. `eval resume` -- normalizes any stuck `running` trial back to
`pending` before continuing, so no trial is ever permanently stranded.

```bash
uv run coldctl eval run configs/evaluations/artifact-vault-recovery.terra.yaml --yes
# ...Ctrl+C...
uv run coldctl eval resume <run-id> --yes
```

## Public/private outputs

After every scheduled trial reaches a terminal state, ColdStart reuses
**Phase 1's own aggregation and reporting code directly** (`compute_aggregate`,
`build_report`, `render_json`) -- nothing is recalculated separately:

- Private reports: `.coldstart/private-reports/<run-id>/<task>__<system>.private.json`
  (gitignored; may include individual attempts, failed check names, job/trial
  IDs, raw artifact references, exception detail).
- Public reports: `reports/generated/<run-id>/<task>__<system>.public.json`
  (tracked; aggregate-only -- strict pass rate, dimension averages, attempt
  count, aggregated cost/runtime, model/agent configuration, benchmark
  version, evaluation date). Public reports pass the same redaction tests as
  Phase 1's (see [`tests/test_reports.py`](../tests/test_reports.py) and
  [`tests/eval/test_orchestrator.py`](../tests/eval/test_orchestrator.py)):
  no local filesystem paths, no individual hidden-check names, no
  trajectory/oracle/verifier content, no secrets.

Reports are never staged or committed automatically -- they land as plain
files for a human to review.

## Budget controls

- `max_budget_usd` is a hard ceiling checked at three points: before the run
  starts (against the cost estimate), before *each* trial launch (against
  accumulated actual cost), and again on resume.
- An estimate always uses real historical data over a guess when one exists;
  a config-supplied per-trial estimate is the fallback; otherwise the
  operator must explicitly acknowledge proceeding with no estimate at all.

## Credential handling

- Configuration files hold only an environment-variable *name*
  (`api_key_env`); a literal-looking secret value anywhere in a config
  fails `eval validate`/`eval plan`/`eval run` outright
  (`coldctl.eval.redact.find_secret_like_values`).
- Preflight checks report only whether a required variable is *set*, never
  its value.
- The real Harbor subprocess inherits the parent process environment
  unmodified (`subprocess.run(..., env=None)`); ColdStart never reconstructs
  or passes a credential as a command-line argument.
- Harbor command arguments and any captured stdout/stderr are redacted
  (`coldctl.eval.redact.redact_argv` / `redact_text`) before being written to
  `events.jsonl` or `logs/*.log`.
- **ColdStart never stores an API-key value** -- not in the manifest, the
  state file, the event log, per-trial logs, or generated reports. This is
  covered by a dedicated adversarial test in
  [`tests/eval/test_secret_leakage.py`](../tests/eval/test_secret_leakage.py).

## Reproducing the example evaluation

```bash
export OPENAI_API_KEY=...   # never pass this as a CLI argument
uv run coldctl eval validate configs/evaluations/artifact-vault-recovery.terra.yaml
uv run coldctl eval plan configs/evaluations/artifact-vault-recovery.terra.yaml
uv run coldctl eval run configs/evaluations/artifact-vault-recovery.terra.yaml --yes
uv run coldctl eval status <run-id>
# if interrupted:
uv run coldctl eval resume <run-id> --yes
```
