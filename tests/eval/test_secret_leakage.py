"""Dedicated coverage for the hardest safety requirement: an API-key VALUE
must never appear in CLI output, the manifest, the state file, the event
log, generated reports, or any Harbor argument vector -- no matter what kind
of trial outcome occurs (including ones whose exception message might
plausibly echo back request details)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from coldctl.cli import app
from coldctl.eval import manifest as manifest_module
from coldctl.eval import orchestrator
from coldctl.eval.planner import build_plan

from .conftest import TASK_DIR_NAME, make_config
from .fake_harbor import FakeHarborRunner, ScriptedOutcome

SECRET = "sk-proj-thisisatotallyfakeapikeyvalue1234567890abcdef"  # pragma: allowlist secret

runner = CliRunner()


def _plan_trial_ids(fake_repo, config):
    plan = build_plan(config, repo_root=fake_repo, phase1_db_path=fake_repo / ".coldstart" / "results.db", allow_dirty=False)
    return [t.trial_id for t in plan.trials]


def _scan_for_secret(*roots) -> list[str]:
    """Scans everything ColdStart itself writes: the manifest, state, event
    log, its own (redacted) per-trial logs, and generated reports.

    Deliberately excludes ``harbor_jobs/`` -- the raw, simulated Harbor job
    output -- because that directory represents Harbor's own on-disk
    artifact (analogous to a real job directory a misbehaving upstream
    provider client library might have written to), which is outside
    ColdStart's control and not something it generates or re-persists.
    ColdStart's own responsibility, verified here, is to never copy such
    content into anything it itself produces.
    """
    hits = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or "harbor_jobs" in path.parts:
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if SECRET in text:
                hits.append(str(path))
    return hits


def test_secret_env_var_value_never_appears_anywhere_in_a_full_run(fake_repo, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    config = make_config(trials_per_task=4, max_budget_usd=5.0, max_infra_retries=0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)

    # Deliberately include outcomes whose exception messages might plausibly
    # (in a careless implementation) echo request/credential details.
    script = {
        trial_ids[0]: [ScriptedOutcome(kind="passed", cost_usd=0.1)],
        trial_ids[1]: [ScriptedOutcome(kind="failed", cost_usd=0.1)],
        trial_ids[2]: [
            ScriptedOutcome(
                kind="infra_invalid",
                exception_message=f"connection using header Authorization: Bearer {SECRET} timed out",
            )
        ],
        trial_ids[3]: [ScriptedOutcome(kind="unknown", exception_message=f"weird error near key {SECRET}")],
    }

    coldstart_dir = fake_repo / ".coldstart"
    outcome = orchestrator.run_evaluation(
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        allow_dirty=False,
        budget_ack=False,
        harbor_runner=FakeHarborRunner(script),
        run_id="secret-leak-test",
        skip_preflight=True,
    )

    run_dir = manifest_module.run_dir_for(coldstart_dir, "secret-leak-test")
    hits = _scan_for_secret(run_dir, fake_repo / "reports", fake_repo / ".coldstart" / "private-reports")
    assert hits == [], f"secret leaked into: {hits}"

    # Confirm the logs/ directory is actually populated (not trivially
    # secret-free by virtue of being empty).
    log_files = list((run_dir / "logs").glob("*.log"))
    assert len(log_files) == 4

    # And the manifest/state as loaded structures, not just raw text.
    manifest_dict = manifest_module.read_manifest(run_dir)
    assert SECRET not in json.dumps(manifest_dict)
    state = manifest_module.read_state(run_dir)
    assert SECRET not in json.dumps(state.to_dict())


def test_secret_env_var_value_never_appears_in_cli_output(fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    config_path = fake_repo / "eval.yaml"
    config_path.write_text(
        f"""
id: secret-test
description: leak-check config
benchmark_version: "0.1.0"
status: development
tasks:
  - {TASK_DIR_NAME}
systems:
  - provider: openai
    model: openai/gpt-5.6-terra
    agent: terminus-2
    environment: fake-env
    agent_kwargs:
      reasoning_effort: medium
    api_key_env: OPENAI_API_KEY
    trials_per_task: 1
    estimated_cost_per_trial_usd: 0.1
execution:
  max_budget_usd: 2.00
"""
    )

    for args in (
        ["eval", "validate", str(config_path)],
        ["eval", "plan", str(config_path), "--json"],
        ["eval", "run", str(config_path)],  # no --yes: shows the plan only
    ):
        result = runner.invoke(app, args)
        assert SECRET not in result.output, f"secret leaked in output of {args}: {result.output}"
