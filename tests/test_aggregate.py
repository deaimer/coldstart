from __future__ import annotations

from datetime import datetime, timedelta

from coldctl.results.aggregate import compute_aggregate
from coldctl.results.ingest import ingest_jobs

from .helpers import write_job

# Mirrors the five real gpt-5.6-terra / terminus-2 trials against
# artifact-vault-recovery, sanitized (no real trajectory/API content) but
# numerically faithful, so the aggregate math is exercised exactly as it was
# validated against the real jobs/ directory.
TERRA_TRIALS = [
    {
        "runtime_sec": 264.203051,
        "cost_usd": 0.36537539999999996,
        "input_tokens": 530707,
        "output_tokens": 13868,
        "rewards": {
            "coldstart_pass": 0.0,
            "durability": 1.0,
            "evidence": 1.0,
            "functional": 1.0,
            "integrity": 1.0,
            "state_safety": 0.6666666666666666,
        },
        "checks": {
            "concurrent_idempotency": True,
            "evidence_report": True,
            "initial_readiness": True,
            "legacy_backfill": False,
            "new_upload_retrievable": True,
            "no_orphan_uploads": True,
            "restart_durability": True,
            "seed_state_preserved": True,
            "storage_aware_readiness": True,
        },
    },
    {
        "runtime_sec": 281.758622,
        "cost_usd": 0.3143266,
        "input_tokens": 398391,
        "output_tokens": 13018,
        "rewards": {
            "coldstart_pass": 0.0,
            "durability": 1.0,
            "evidence": 1.0,
            "functional": 1.0,
            "integrity": 0.5,
            "state_safety": 0.6666666666666666,
        },
        "checks": {
            "concurrent_idempotency": True,
            "evidence_report": True,
            "initial_readiness": True,
            "legacy_backfill": False,
            "new_upload_retrievable": True,
            "no_orphan_uploads": True,
            "restart_durability": True,
            "seed_state_preserved": True,
            "storage_aware_readiness": False,
        },
    },
    {
        "runtime_sec": 299.688234,
        "cost_usd": 0.34395070000000005,
        "input_tokens": 435207,
        "output_tokens": 14310,
        "rewards": {
            "coldstart_pass": 1.0,
            "durability": 1.0,
            "evidence": 1.0,
            "functional": 1.0,
            "integrity": 1.0,
            "state_safety": 1.0,
        },
        "checks": {
            "concurrent_idempotency": True,
            "evidence_report": True,
            "initial_readiness": True,
            "legacy_backfill": True,
            "new_upload_retrievable": True,
            "no_orphan_uploads": True,
            "restart_durability": True,
            "seed_state_preserved": True,
            "storage_aware_readiness": True,
        },
    },
    {
        "runtime_sec": 221.944552,
        "cost_usd": 0.22094049999999998,
        "input_tokens": 237950,
        "output_tokens": 9562,
        "rewards": {
            "coldstart_pass": 0.0,
            "durability": 0.0,
            "evidence": 0.0,
            "functional": 0.5,
            "integrity": 0.0,
            "state_safety": 0.6666666666666666,
        },
        "checks": {
            "concurrent_idempotency": False,
            "evidence_report": False,
            "initial_readiness": True,
            "legacy_backfill": True,
            "new_upload_retrievable": False,
            "no_orphan_uploads": False,
            "restart_durability": False,
            "seed_state_preserved": True,
            "storage_aware_readiness": False,
        },
    },
    {
        "runtime_sec": 198.562947,
        "cost_usd": 0.2327576,
        "input_tokens": 198429,
        "output_tokens": 10460,
        "rewards": {
            "coldstart_pass": 0.0,
            "durability": 1.0,
            "evidence": 1.0,
            "functional": 1.0,
            "integrity": 0.5,
            "state_safety": 0.6666666666666666,
        },
        "checks": {
            "concurrent_idempotency": True,
            "evidence_report": True,
            "initial_readiness": True,
            "legacy_backfill": False,
            "new_upload_retrievable": True,
            "no_orphan_uploads": True,
            "restart_durability": True,
            "seed_state_preserved": True,
            "storage_aware_readiness": False,
        },
    },
]


def _ingest_terra_fixture(conn, tmp_path):
    base = datetime(2026, 1, 1, 10, 0, 0)
    job_dirs = []
    for index, trial in enumerate(TERRA_TRIALS):
        started = base + timedelta(hours=index)
        finished = started + timedelta(seconds=trial["runtime_sec"])
        job_dir = write_job(
            tmp_path,
            f"terra_job_{index}",
            started_at=started.isoformat(sep="T", timespec="microseconds"),
            finished_at=finished.isoformat(sep="T", timespec="microseconds"),
            trials=[
                {
                    "started_at": started.isoformat(sep="T", timespec="microseconds") + "Z",
                    "finished_at": finished.isoformat(sep="T", timespec="microseconds") + "Z",
                    "rewards": trial["rewards"],
                    "checks": trial["checks"],
                    "cost_usd": trial["cost_usd"],
                    "input_tokens": trial["input_tokens"],
                    "output_tokens": trial["output_tokens"],
                    "cached_tokens": 0,
                }
            ],
        )
        job_dirs.append(job_dir)

    results = ingest_jobs(conn, job_dirs)
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]


def test_five_terra_trials_match_expected_aggregate(conn, tmp_path):
    _ingest_terra_fixture(conn, tmp_path)

    aggregate = compute_aggregate(conn, task="artifact-vault-recovery", system="gpt-5.6-terra__terminus-2")

    assert aggregate.attempts == 5
    assert aggregate.scored_attempts == 5
    assert aggregate.passes == 1
    assert aggregate.failures == 4
    assert aggregate.strict_pass_rate == 0.20

    assert aggregate.total_cost_usd == 1.4773508
    assert round(aggregate.average_cost_usd, 8) == 0.29547016

    assert round(aggregate.median_runtime_sec) == 264

    assert round(aggregate.dimension_averages["functional"], 10) == 0.90
    assert round(aggregate.dimension_averages["durability"], 10) == 0.80
    assert round(aggregate.dimension_averages["evidence"], 10) == 0.80
    assert abs(aggregate.dimension_averages["state_safety"] - 0.7333333333) < 1e-9
    assert round(aggregate.dimension_averages["integrity"], 10) == 0.60

    assert aggregate.failed_check_counts["legacy_backfill"] == 3
    assert aggregate.failed_check_counts["storage_aware_readiness"] == 3

    assert aggregate.exception_count == 0

    # coldstart_pass must never appear as a "dimension" average.
    assert "coldstart_pass" not in aggregate.dimension_averages


def test_strict_pass_is_isolated_from_dimension_scores(conn, tmp_path):
    """A trial with strong dimension scores but coldstart_pass=0 must count
    as a strict failure; dimension averages must not compensate for it."""
    job_dir = write_job(
        tmp_path,
        "isolation_job",
        trials=[
            {
                "rewards": {
                    "coldstart_pass": 0.0,
                    "functional": 1.0,
                    "durability": 1.0,
                    "state_safety": 1.0,
                    "integrity": 1.0,
                    "evidence": 1.0,
                },
                "checks": {"some_hidden_gate": False},
            }
        ],
    )
    ingest_jobs(conn, [job_dir])

    aggregate = compute_aggregate(conn, task="artifact-vault-recovery", system="gpt-5.6-terra__terminus-2")
    assert aggregate.passes == 0
    assert aggregate.failures == 1
    assert aggregate.strict_pass_rate == 0.0
    # Every diagnostic dimension is a perfect 1.0, yet strict pass is still 0.
    assert all(value == 1.0 for value in aggregate.dimension_averages.values())


def test_exceptions_are_excluded_from_scored_attempts(conn, tmp_path):
    job_dir = write_job(
        tmp_path,
        "exception_job",
        n_total_trials=2,
        trials=[
            {"trial_dir_name": "ok", "rewards": {"coldstart_pass": 1.0}},
            {
                "trial_dir_name": "boom",
                "exception": {
                    "exception_type": "TimeoutError",
                    "exception_message": "environment build timed out",
                    "exception_traceback": "…",
                    "occurred_at": "2026-01-01T00:00:00Z",
                },
            },
        ],
    )
    ingest_jobs(conn, [job_dir])

    aggregate = compute_aggregate(conn, task="artifact-vault-recovery", system="gpt-5.6-terra__terminus-2")
    assert aggregate.attempts == 2
    assert aggregate.scored_attempts == 1
    assert aggregate.passes == 1
    assert aggregate.exception_count == 1
