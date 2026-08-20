from __future__ import annotations

from coldctl.eval.classifier import TrialOutcome, classify_missing_result, classify_trial_result


def test_verifier_pass_is_passed():
    result = {"exception_info": None, "verifier_result": {"rewards": {"coldstart_pass": 1.0}}}
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.PASSED


def test_verifier_scored_zero_is_a_valid_failure_not_infra():
    result = {"exception_info": None, "verifier_result": {"rewards": {"coldstart_pass": 0.0, "functional": 0.5}}}
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.FAILED
    assert c.reason == "verifier_reported_task_failure"


def test_max_turn_exhaustion_style_failure_is_still_a_valid_scored_failure():
    # Harbor recorded no exception_info at all -- the agent simply ran out of
    # turns/timed out/refused within its own execution and the verifier still
    # scored it. That must never be treated as infra-invalid.
    result = {"exception_info": None, "verifier_result": {"rewards": {"coldstart_pass": 0.0}}}
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.FAILED


def test_no_exception_but_no_rewards_is_unknown():
    result = {"exception_info": None, "verifier_result": None}
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.UNKNOWN


def test_healthcheck_error_is_infra_invalid():
    result = {
        "exception_info": {
            "exception_type": "HealthcheckError",
            "exception_message": "environment healthcheck failed before the agent began",
        }
    }
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.INFRA_INVALID


def test_docker_build_failure_message_is_infra_invalid():
    result = {
        "exception_info": {
            "exception_type": "SomeWrapperError",
            "exception_message": "docker compose command failed while building the environment",
        }
    }
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.INFRA_INVALID


def test_rate_limit_message_is_infra_invalid():
    result = {
        "exception_info": {
            "exception_type": "ProviderError",
            "exception_message": "received HTTP 429 rate limit exceeded, no trajectory produced",
        }
    }
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.INFRA_INVALID


def test_authentication_error_stops_not_retries():
    result = {
        "exception_info": {
            "exception_type": "AuthenticationError",
            "exception_message": "Incorrect API key provided",
        }
    }
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.AUTH_ERROR


def test_unauthorized_message_is_auth_error_even_with_generic_type():
    result = {
        "exception_info": {
            "exception_type": "RuntimeError",
            "exception_message": "401 Unauthorized: invalid api key",
        }
    }
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.AUTH_ERROR


def test_unrecognized_exception_type_is_unknown_and_conservative():
    result = {
        "exception_info": {
            "exception_type": "SomeBrandNewNeverSeenError",
            "exception_message": "completely novel failure mode",
        }
    }
    c = classify_trial_result(result)
    assert c.outcome == TrialOutcome.UNKNOWN


def test_missing_result_without_auth_signal_is_infra_invalid():
    c = classify_missing_result(harbor_returncode=1, job_dir="/tmp/nope", stderr="connection reset by peer")
    assert c.outcome == TrialOutcome.INFRA_INVALID


def test_missing_result_with_auth_signal_is_auth_error():
    c = classify_missing_result(harbor_returncode=1, job_dir="/tmp/nope", stderr="401 invalid api key provided")
    assert c.outcome == TrialOutcome.AUTH_ERROR


def test_secret_shaped_text_is_redacted_from_evidence():
    result = {
        "exception_info": {
            "exception_type": "RuntimeError",
            "exception_message": "failed using key sk-proj-abcdefghijklmnopqrstuvwxyz0123456789, docker compose broke",
        }
    }
    c = classify_trial_result(result)
    assert "sk-proj-" not in c.evidence
    assert "[REDACTED]" in c.evidence
