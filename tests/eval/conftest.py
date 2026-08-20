from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coldctl.eval.config import EvaluationConfig, ExecutionConfig, ReportsConfig, ReportTargetConfig, SystemConfig

TASK_DIR_NAME = "fake-task"


def _git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def write_fake_task(repo: Path, name: str = TASK_DIR_NAME) -> Path:
    task_dir = repo / name
    (task_dir / "solution").mkdir(parents=True, exist_ok=True)
    (task_dir / "tests").mkdir(parents=True, exist_ok=True)
    (task_dir / "environment").mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Fix the fake thing.\n")
    (task_dir / "task.toml").write_text('[task]\nname = "fake-task"\nversion = "0.1.0"\n')
    (task_dir / "solution" / "solve.sh").write_text("#!/bin/sh\necho solved\n")
    (task_dir / "tests" / "test.sh").write_text("#!/bin/sh\necho '{}'\n")
    (task_dir / "environment" / "Dockerfile").write_text("FROM scratch\n")
    return task_dir


@pytest.fixture
def fake_repo(tmp_path) -> Path:
    """A throwaway git repo with one minimal task, fully decoupled from the
    real ColdStart repository and its `benchmark/sample-tasks/` content."""
    repo = tmp_path / "repo"
    repo.mkdir()
    write_fake_task(repo)
    _git(["init", "-q"], cwd=repo)
    _git(["add", "-A"], cwd=repo)
    _git(["-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-q", "-m", "init"], cwd=repo)
    return repo


def make_config(
    *,
    id: str = "fake-eval",
    trials_per_task: int = 5,
    status: str = "development",
    max_budget_usd: float = 2.0,
    max_infra_retries: int = 2,
    estimated_cost_per_trial_usd: float | None = None,
    source_path: Path | None = None,
) -> EvaluationConfig:
    return EvaluationConfig(
        id=id,
        description="fake evaluation for tests",
        benchmark_version="0.1.0",
        status=status,
        tasks=[TASK_DIR_NAME],
        systems=[
            SystemConfig(
                provider="openai",
                model="openai/gpt-5.6-terra",
                agent="terminus-2",
                environment="fake-env",
                api_key_env="OPENAI_API_KEY",
                trials_per_task=trials_per_task,
                agent_kwargs={"reasoning_effort": "medium", "use_responses_api": True, "max_turns": 30},
                estimated_cost_per_trial_usd=estimated_cost_per_trial_usd,
            )
        ],
        execution=ExecutionConfig(
            max_concurrent_trials=1, max_infra_retries=max_infra_retries, max_budget_usd=max_budget_usd
        ),
        reports=ReportsConfig(
            private=ReportTargetConfig(enabled=True, path=".coldstart/private-reports"),
            public=ReportTargetConfig(enabled=True, path="reports/generated"),
        ),
        source_path=source_path,
    )


@pytest.fixture
def sample_config() -> EvaluationConfig:
    return make_config()
