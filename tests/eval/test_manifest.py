from __future__ import annotations

import json

import pytest

from coldctl.eval import manifest as manifest_module


def test_atomic_write_json_leaves_no_temp_file(tmp_path):
    path = tmp_path / "state.json"
    manifest_module.atomic_write_json(path, {"a": 1})
    assert path.is_file()
    leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []
    assert json.loads(path.read_text()) == {"a": 1}


def test_atomic_write_json_overwrites_cleanly(tmp_path):
    path = tmp_path / "state.json"
    manifest_module.atomic_write_json(path, {"a": 1})
    manifest_module.atomic_write_json(path, {"a": 2})
    assert json.loads(path.read_text()) == {"a": 2}


def test_write_manifest_refuses_to_overwrite(tmp_path):
    run_dir = tmp_path / "run"
    manifest_module.write_manifest(run_dir, {"run_id": "x"})
    with pytest.raises(manifest_module.ManifestExistsError):
        manifest_module.write_manifest(run_dir, {"run_id": "y"})
    # original content must be untouched
    assert manifest_module.read_manifest(run_dir) == {"run_id": "x"}


def test_write_manifest_creates_logs_and_artifacts_dirs(tmp_path):
    run_dir = tmp_path / "run"
    manifest_module.write_manifest(run_dir, {"run_id": "x"})
    assert (run_dir / "logs").is_dir()
    assert (run_dir / "artifacts").is_dir()


def test_state_round_trip(tmp_path):
    run_dir = tmp_path / "run"
    state = manifest_module.new_state("run-1", ["t1", "t2"])
    manifest_module.write_state(run_dir, state)
    loaded = manifest_module.read_state(run_dir)
    assert loaded.run_id == "run-1"
    assert set(loaded.trials.keys()) == {"t1", "t2"}
    assert loaded.pending_trial_ids == ["t1", "t2"]
    assert loaded.completed_trial_ids == []


def test_state_transitions_are_reflected_after_reload(tmp_path):
    run_dir = tmp_path / "run"
    state = manifest_module.new_state("run-1", ["t1", "t2"])
    state.trials["t1"].status = "passed"
    state.trials["t1"].attempts = 1
    state.actual_cost_usd = 0.5
    manifest_module.write_state(run_dir, state)

    loaded = manifest_module.read_state(run_dir)
    assert loaded.completed_trial_ids == ["t1"]
    assert loaded.pending_trial_ids == ["t2"]
    assert loaded.actual_cost_usd == 0.5


def test_append_event_appends_lines(tmp_path):
    events_path = tmp_path / "events.jsonl"
    manifest_module.append_event(events_path, {"event_type": "run_created", "run_id": "x", "timestamp": "t"})
    manifest_module.append_event(events_path, {"event_type": "trial_started", "run_id": "x", "timestamp": "t"})
    lines = events_path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "run_created"
    assert json.loads(lines[1])["event_type"] == "trial_started"


def test_read_manifest_missing_raises():
    with pytest.raises(FileNotFoundError):
        manifest_module.read_manifest(manifest_module.run_dir_for(".", "no-such-run"))


def test_read_state_missing_raises():
    with pytest.raises(FileNotFoundError):
        manifest_module.read_state(manifest_module.run_dir_for(".", "no-such-run"))
