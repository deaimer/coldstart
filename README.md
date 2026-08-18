# ColdStart

ColdStart is a developing benchmark for evaluating whether AI agents can enter unfamiliar, stateful software systems, diagnose the real failure, implement a safe repair, preserve existing state, survive controlled perturbations, and accurately report what they accomplished.

The project uses [Harbor](https://github.com/harbor-framework/harbor) as its execution framework. ColdStart adds the benchmark specification, task-quality gates, scoring policy, authoring workflow, and reporting layer.

## Status

This repository is the public project scaffold for ColdStart v0.1. It will contain the CLI, methodology, authoring documentation, public sample tasks, and released reports. Held-out evaluation tasks and Oracle solutions should remain in the private `coldstart-evals` repository.

## What ColdStart measures

A ColdStart task should test several of the following:

- Operation in an unfamiliar system
- Root-cause diagnosis from incomplete or misleading evidence
- Safe repair across multiple components
- Preservation or migration of existing state
- Durability after restart, rebuild, or controlled perturbation
- Resistance to shortcuts and reward manipulation
- Evidence-grounded reporting

## Repository layout

```text
coldstart/
├── README.md
├── LICENSE
├── .gitignore
├── pyproject.toml
├── src/coldctl/
├── tests/
├── docs/
├── benchmark/sample-tasks/
├── schemas/
├── configs/
├── reports/
└── .github/workflows/
```

## Local setup

Requirements:

- Linux or macOS development environment
- Python 3.11 or newer
- Docker
- Git
- `uv`
- Harbor

Install Harbor and the ColdStart CLI:

```bash
uv tool install harbor
uv sync
uv run coldctl doctor
```

Create the first sample task:

```bash
uv run coldctl task init artifact-vault-recovery
```

Validate its structure:

```bash
uv run coldctl validate benchmark/sample-tasks/artifact-vault-recovery
```

After implementing the task's Oracle solution and verifier, run:

```bash
uv run coldctl oracle benchmark/sample-tasks/artifact-vault-recovery --runs 5
```

## Evaluation results

After running Harbor jobs, ingest them into a normalized local results store
and generate reports:

```bash
uv run coldctl results ingest jobs/2026-08-18__18-28-15
uv run coldctl results list-runs
uv run coldctl results list-trials --task artifact-vault-recovery
uv run coldctl reports task --task artifact-vault-recovery \
  --system gpt-5.6-terra__terminus-2 --visibility public --format json
```

See [`docs/results.md`](docs/results.md) for the data model, ingestion
workflow, public/private data policy, and reproducibility guarantees. API
keys are never read, printed, or stored by this system.

## Development rule

Do not commit model API keys, customer data, private tasks, private tests, or production Oracle solutions to this repository. No author may approve their own task.

## License

The public ColdStart code and documentation are licensed under Apache License 2.0. Individual datasets may include separate terms in their release manifests.
