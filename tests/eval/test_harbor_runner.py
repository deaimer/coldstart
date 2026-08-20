from __future__ import annotations

from pathlib import Path

from coldctl.eval.harbor_runner import build_harbor_invocation
from coldctl.eval.planner import TrialSpec


def _trial(**overrides) -> TrialSpec:
    defaults = dict(
        trial_id="fake-task__gpt-5.6-terra__terminus-2__01_abc123",
        task_path="benchmark/sample-tasks/artifact-vault-recovery",
        task_name="artifact-vault-recovery",
        system_key="gpt-5.6-terra__terminus-2",
        provider="openai",
        model="openai/gpt-5.6-terra",
        agent="terminus-2",
        environment="docker",
        agent_kwargs={"reasoning_effort": "medium", "use_responses_api": True, "max_turns": 30},
        api_key_env="OPENAI_API_KEY",
        attempt=1,
    )
    defaults.update(overrides)
    return TrialSpec(**defaults)


def test_harbor_invocation_uses_argument_array_not_shell_string():
    invocation = build_harbor_invocation(_trial(), job_name="my-job", jobs_dir=Path("/tmp/jobs"))
    assert isinstance(invocation.argv, list)
    assert all(isinstance(part, str) for part in invocation.argv)


def test_harbor_invocation_core_flags():
    trial = _trial()
    invocation = build_harbor_invocation(trial, job_name="my-job", jobs_dir=Path("/tmp/jobs"))
    argv = invocation.argv
    assert argv[0:2] == ["harbor", "run"]
    assert "-p" in argv and argv[argv.index("-p") + 1] == trial.task_path
    assert "-a" in argv and argv[argv.index("-a") + 1] == trial.agent
    assert "-m" in argv and argv[argv.index("-m") + 1] == trial.model
    assert "-e" in argv and argv[argv.index("-e") + 1] == trial.environment
    assert "--job-name" in argv and argv[argv.index("--job-name") + 1] == "my-job"
    assert "--jobs-dir" in argv and argv[argv.index("--jobs-dir") + 1] == "/tmp/jobs"
    assert "-y" in argv


def test_harbor_invocation_job_dir_is_deterministic():
    invocation = build_harbor_invocation(_trial(), job_name="my-job", jobs_dir=Path("/tmp/jobs"))
    assert invocation.job_dir == Path("/tmp/jobs/my-job")


def test_harbor_invocation_agent_kwargs_are_json_encoded_for_harbors_parser():
    trial = _trial(agent_kwargs={"reasoning_effort": "medium", "use_responses_api": True, "max_turns": 30})
    invocation = build_harbor_invocation(trial, job_name="j", jobs_dir=Path("/tmp/jobs"))
    argv = invocation.argv
    ak_values = [argv[i + 1] for i, a in enumerate(argv) if a == "--ak"]
    assert "max_turns=30" in ak_values
    assert "reasoning_effort=\"medium\"" in ak_values
    assert "use_responses_api=true" in ak_values


def test_harbor_invocation_never_includes_a_credential_value():
    trial = _trial()
    invocation = build_harbor_invocation(trial, job_name="j", jobs_dir=Path("/tmp/jobs"))
    # api_key_env only ever appears as a spec field, never passed as a CLI arg.
    assert trial.api_key_env not in invocation.argv
    joined = " ".join(invocation.argv)
    assert "OPENAI_API_KEY" not in joined
