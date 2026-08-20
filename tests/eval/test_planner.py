from __future__ import annotations

from pathlib import Path

import pytest

from coldctl.eval.planner import (
    DirtyWorktreeError,
    build_plan,
    compute_config_hash,
    estimate_cost,
    expand_trials,
    hash_tasks,
)
from coldctl.results import db as db_module
from coldctl.results.ingest import ingest_job

from .conftest import make_config


def test_trial_expansion_is_deterministic(sample_config):
    config_hash = compute_config_hash(sample_config)
    trials_a = expand_trials(sample_config, config_hash)
    trials_b = expand_trials(sample_config, config_hash)
    assert [t.trial_id for t in trials_a] == [t.trial_id for t in trials_b]
    assert len(trials_a) == 5
    assert len(set(t.trial_id for t in trials_a)) == 5  # all unique


def test_trial_ids_change_when_config_changes(sample_config):
    hash_a = compute_config_hash(sample_config)
    trials_a = expand_trials(sample_config, hash_a)

    changed = make_config(trials_per_task=5, max_budget_usd=999.0)  # different budget -> different hash
    hash_b = compute_config_hash(changed)
    trials_b = expand_trials(changed, hash_b)

    assert hash_a != hash_b
    assert [t.trial_id for t in trials_a] != [t.trial_id for t in trials_b]


def test_task_hash_changes_when_task_content_changes(fake_repo):
    hashes_before = hash_tasks(fake_repo, ["fake-task"])
    (fake_repo / "fake-task" / "instruction.md").write_text("A completely different instruction.\n")
    hashes_after = hash_tasks(fake_repo, ["fake-task"])
    assert hashes_before["fake-task"] != hashes_after["fake-task"]


def test_task_hash_stable_when_nothing_changes(fake_repo):
    a = hash_tasks(fake_repo, ["fake-task"])
    b = hash_tasks(fake_repo, ["fake-task"])
    assert a == b


def test_dirty_official_run_is_refused(fake_repo, sample_config):
    (fake_repo / "fake-task" / "extra.txt").write_text("uncommitted\n")
    official = make_config(status="official")
    with pytest.raises(DirtyWorktreeError):
        build_plan(official, repo_root=fake_repo, phase1_db_path=fake_repo / "nope.db", allow_dirty=False)


def test_official_run_allow_dirty_is_marked_unverified(fake_repo):
    (fake_repo / "fake-task" / "extra.txt").write_text("uncommitted\n")
    official = make_config(status="official")
    plan = build_plan(official, repo_root=fake_repo, phase1_db_path=fake_repo / "nope.db", allow_dirty=True)
    assert plan.unverified is True
    assert plan.git_dirty is True


def test_development_dirty_run_does_not_require_allow_dirty(fake_repo, sample_config):
    (fake_repo / "fake-task" / "extra.txt").write_text("uncommitted\n")
    plan = build_plan(sample_config, repo_root=fake_repo, phase1_db_path=fake_repo / "nope.db", allow_dirty=False)
    assert plan.unverified is True


def test_clean_worktree_plan_is_verified(fake_repo, sample_config):
    plan = build_plan(sample_config, repo_root=fake_repo, phase1_db_path=fake_repo / "nope.db", allow_dirty=False)
    assert plan.unverified is False
    assert plan.git_dirty is False
    assert plan.git_commit is not None


def _seed_phase1_db(db_path: Path, *, task_name: str, system_key: str, cost_per_trial: float, n: int) -> None:
    conn = db_module.connect(db_path)
    try:
        for i in range(n):
            job_dir = db_path.parent / f"seed_job_{i}"
            from tests.helpers import write_job

            write_job(
                db_path.parent,
                f"seed_job_{i}",
                model_name=system_key.split("__")[0],
                agent_name=system_key.split("__")[1],
                trials=[
                    {
                        "task_name": task_name,
                        "rewards": {"coldstart_pass": 1.0},
                        "checks": {"a_check": True},
                        "cost_usd": cost_per_trial,
                    }
                ],
            )
            ingest_job(conn, job_dir)
        conn.commit()
    finally:
        conn.close()


def test_historical_cost_estimate_used_when_available(fake_repo):
    db_path = fake_repo / ".coldstart" / "results.db"
    _seed_phase1_db(db_path, task_name="fake-task", system_key="gpt-5.6-terra__terminus-2", cost_per_trial=0.30, n=4)

    config = make_config(trials_per_task=5)
    estimate = estimate_cost(config, phase1_db_path=db_path)
    assert estimate.source == "historical"
    assert estimate.total_usd == pytest.approx(0.30 * 5)


def test_configured_estimate_used_as_fallback(fake_repo):
    config = make_config(trials_per_task=5, estimated_cost_per_trial_usd=0.4)
    estimate = estimate_cost(config, phase1_db_path=fake_repo / "does-not-exist.db")
    assert estimate.source == "configured_estimate"
    assert estimate.total_usd == pytest.approx(0.4 * 5)


def test_estimate_unavailable_without_history_or_configured_value(fake_repo):
    config = make_config(trials_per_task=5, estimated_cost_per_trial_usd=None)
    estimate = estimate_cost(config, phase1_db_path=fake_repo / "does-not-exist.db")
    assert estimate.source == "unavailable"
    assert estimate.total_usd is None


def test_historical_estimate_five_trials_matches_terra_baseline(fake_repo):
    """Mirrors the real acceptance criterion: 5 historical trials averaging
    $0.29547016 must estimate ~$1.4773508 for 5 planned trials."""
    db_path = fake_repo / ".coldstart" / "results.db"
    costs = [0.36537539999999996, 0.3143266, 0.34395070000000005, 0.22094049999999998, 0.2327576]
    conn = db_module.connect(db_path)
    try:
        from tests.helpers import write_job

        for i, cost in enumerate(costs):
            write_job(
                db_path.parent,
                f"seed_job_{i}",
                model_name="gpt-5.6-terra",
                agent_name="terminus-2",
                trials=[{"task_name": "fake-task", "rewards": {"coldstart_pass": 1.0}, "cost_usd": cost}],
            )
            ingest_job(conn, db_path.parent / f"seed_job_{i}")
        conn.commit()
    finally:
        conn.close()

    config = make_config(trials_per_task=5)
    estimate = estimate_cost(config, phase1_db_path=db_path)
    assert estimate.source == "historical"
    assert estimate.total_usd == pytest.approx(1.4773508, abs=1e-6)
