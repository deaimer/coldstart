"""Conservative retry classification for a completed (or missing) Harbor trial.

Distinguishes:
  - a genuine, scored task outcome (pass or valid failure) -- never retried
    beyond the independently scheduled trials
  - an infrastructure-invalid attempt -- retryable up to a configured limit
  - an authentication failure -- must stop immediately, never auto-retried
  - an unrecognized situation -- pauses the run for human review rather than
    being retried indefinitely (the conservative default)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from coldctl.eval.redact import redact_text


class TrialOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INFRA_INVALID = "infra_invalid"
    AUTH_ERROR = "auth_error"
    UNKNOWN = "unknown"


@dataclass
class Classification:
    outcome: TrialOutcome
    reason: str
    evidence: str


_AUTH_SIGNALS = (
    "authenticationerror",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "permissiondenied",
    "permission denied",
    "forbidden",
    "401",
    "invalid_request_error: incorrect api key",
    "api key not found",
    "no api key",
)

#: Conservative, explicit allowlist of exception *types* known to indicate an
#: infrastructure problem rather than a genuine agent/model outcome.
_INFRA_EXCEPTION_TYPES = {
    "RuntimeError",
    "TimeoutError",
    "ConnectionError",
    "ConnectionResetError",
    "DockerException",
    "EnvironmentSetupError",
    "HealthcheckError",
    "BuildError",
    "ContainerError",
    "OSError",
    "CalledProcessError",
}

_INFRA_MESSAGE_SIGNALS = (
    "docker compose",
    "healthcheck",
    "environment build",
    "environment setup",
    "rate limit",
    "rate_limit",
    "429",
    "service unavailable",
    "503",
    "connection reset",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "no such host",
    "could not build",
)


def _contains_any(text: str, signals: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for signal in signals:
        if signal in lowered:
            return signal
    return None


def classify_missing_result(*, harbor_returncode: int, job_dir: str, stderr: str = "") -> Classification:
    """The harbor subprocess ran but no usable result.json was produced (or
    the process itself failed before Harbor could write one)."""
    redacted_stderr = redact_text(stderr)[:500]
    auth_signal = _contains_any(redacted_stderr, _AUTH_SIGNALS)
    if auth_signal:
        return Classification(
            outcome=TrialOutcome.AUTH_ERROR,
            reason="authentication_failure",
            evidence=f"harbor exit {harbor_returncode}; matched {auth_signal!r} in stderr: {redacted_stderr}",
        )
    return Classification(
        outcome=TrialOutcome.INFRA_INVALID,
        reason="corrupt_or_missing_harbor_result",
        evidence=(
            f"harbor exited {harbor_returncode}; no usable result.json at {job_dir}; "
            f"stderr: {redacted_stderr}"
        ),
    )


def classify_trial_result(trial_result: dict[str, Any]) -> Classification:
    """Classify a trial from its parsed Harbor trial-level result.json."""
    exception_info = trial_result.get("exception_info")

    if exception_info is None:
        verifier_result = trial_result.get("verifier_result") or {}
        rewards = verifier_result.get("rewards")
        if not rewards or "coldstart_pass" not in rewards:
            return Classification(
                outcome=TrialOutcome.UNKNOWN,
                reason="no_exception_but_no_scored_reward",
                evidence="trial completed without exception_info, but no coldstart_pass reward was recorded",
            )
        coldstart_pass = rewards["coldstart_pass"]
        if float(coldstart_pass) >= 1.0:
            return Classification(outcome=TrialOutcome.PASSED, reason="verifier_pass", evidence="coldstart_pass=1.0")
        return Classification(
            outcome=TrialOutcome.FAILED,
            reason="verifier_reported_task_failure",
            evidence=f"coldstart_pass={coldstart_pass}",
        )

    exception_type = str(exception_info.get("exception_type", ""))
    exception_message = redact_text(str(exception_info.get("exception_message", "")))[:500]
    combined_text = f"{exception_type} {exception_message}"

    auth_signal = _contains_any(combined_text, _AUTH_SIGNALS)
    if auth_signal:
        return Classification(
            outcome=TrialOutcome.AUTH_ERROR,
            reason="authentication_failure",
            evidence=f"{exception_type}: {exception_message} (matched {auth_signal!r})",
        )

    if exception_type in _INFRA_EXCEPTION_TYPES:
        return Classification(
            outcome=TrialOutcome.INFRA_INVALID,
            reason=f"known_infra_exception:{exception_type}",
            evidence=f"{exception_type}: {exception_message}",
        )

    infra_signal = _contains_any(combined_text, _INFRA_MESSAGE_SIGNALS)
    if infra_signal:
        return Classification(
            outcome=TrialOutcome.INFRA_INVALID,
            reason=f"infra_message_signal:{infra_signal}",
            evidence=f"{exception_type}: {exception_message}",
        )

    return Classification(
        outcome=TrialOutcome.UNKNOWN,
        reason="unrecognized_exception_type",
        evidence=f"{exception_type}: {exception_message}",
    )
