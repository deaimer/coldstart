"""Evaluation configuration: YAML schema, safe loading, and validation.

Configuration files never contain credential values -- only the *name* of
the environment variable holding one (``api_key_env``). This module never
reads that environment variable's value; it only checks for presence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from coldctl.eval.redact import find_secret_like_values

VALID_STATUSES = ("official", "development")

_TOP_LEVEL_KEYS = {
    "id",
    "description",
    "benchmark_version",
    "status",
    "tasks",
    "systems",
    "execution",
    "reports",
}
_SYSTEM_KEYS = {
    "name",
    "provider",
    "model",
    "agent",
    "environment",
    "agent_kwargs",
    "api_key_env",
    "trials_per_task",
    "estimated_cost_per_trial_usd",
}
_EXECUTION_KEYS = {"max_concurrent_trials", "max_infra_retries", "max_budget_usd"}
_REPORT_TARGET_KEYS = {"enabled", "path"}
_REPORTS_KEYS = {"private", "public"}

DEFAULT_PRIVATE_REPORTS_PATH = ".coldstart/private-reports"
DEFAULT_PUBLIC_REPORTS_PATH = "reports/generated"

#: Locations reserved for private data; a public report path must never
#: resolve inside one of these.
_PRIVATE_STORAGE_ROOTS = (".coldstart", "results/private")
#: The public report root; a private report path must never resolve inside it
#: (that would accidentally stage private detail as a public/committable artifact).
_PUBLIC_STORAGE_ROOTS = ("reports",)


class ConfigError(Exception):
    """Raised for a single fatal config problem (e.g. the file can't be parsed)."""


@dataclass
class ReportTargetConfig:
    enabled: bool = True
    path: str = ""


@dataclass
class ReportsConfig:
    private: ReportTargetConfig = field(
        default_factory=lambda: ReportTargetConfig(enabled=True, path=DEFAULT_PRIVATE_REPORTS_PATH)
    )
    public: ReportTargetConfig = field(
        default_factory=lambda: ReportTargetConfig(enabled=True, path=DEFAULT_PUBLIC_REPORTS_PATH)
    )


@dataclass
class SystemConfig:
    provider: str
    model: str
    agent: str
    environment: str
    api_key_env: str
    trials_per_task: int
    agent_kwargs: dict[str, Any] = field(default_factory=dict)
    estimated_cost_per_trial_usd: float | None = None
    name: str | None = None

    @property
    def system_key(self) -> str:
        if self.name:
            return self.name
        model_short = self.model.split("/", 1)[-1]
        return f"{model_short}__{self.agent}"


@dataclass
class ExecutionConfig:
    max_concurrent_trials: int = 1
    max_infra_retries: int = 0
    max_budget_usd: float = 0.0


@dataclass
class EvaluationConfig:
    id: str
    description: str
    benchmark_version: str
    status: str
    tasks: list[str]
    systems: list[SystemConfig]
    execution: ExecutionConfig
    reports: ReportsConfig
    source_path: Path | None = None

    @property
    def is_official(self) -> bool:
        return self.status == "official"


def _require_dict(value: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _check_unknown_keys(data: dict[str, Any], allowed: set[str], *, where: str, errors: list[str]) -> None:
    unknown = sorted(set(data.keys()) - allowed)
    for key in unknown:
        field_path = f"{where}.{key}" if where else str(key)
        errors.append(f"unknown field: {field_path}")


def validate_config_dict(raw: Any) -> list[str]:
    """Validate a parsed (not-yet-dataclassed) config dict. Returns a list of
    human-readable error strings; empty means the config is structurally and
    semantically valid. Does not touch the filesystem or environment."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return [f"config must be a mapping at the top level, got {type(raw).__name__}"]

    _check_unknown_keys(raw, _TOP_LEVEL_KEYS, where="", errors=errors)

    for key in ("id", "description", "benchmark_version", "status"):
        if not isinstance(raw.get(key), str) or not raw.get(key, "").strip():
            errors.append(f"'{key}' is required and must be a non-empty string")

    if isinstance(raw.get("status"), str) and raw["status"] not in VALID_STATUSES:
        errors.append(f"'status' must be one of {VALID_STATUSES}, got {raw['status']!r}")

    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, str) and t.strip() for t in tasks):
        errors.append("'tasks' must be a non-empty list of non-empty path strings")

    systems = raw.get("systems")
    if not isinstance(systems, list) or not systems:
        errors.append("'systems' must be a non-empty list")
    else:
        for index, system in enumerate(systems):
            where = f"systems[{index}]"
            if not isinstance(system, dict):
                errors.append(f"{where} must be a mapping")
                continue
            _check_unknown_keys(system, _SYSTEM_KEYS, where=where, errors=errors)
            for key in ("provider", "model", "agent", "environment", "api_key_env"):
                if not isinstance(system.get(key), str) or not system.get(key, "").strip():
                    errors.append(f"{where}.{key} is required and must be a non-empty string")
            if isinstance(system.get("api_key_env"), str):
                env_name = system["api_key_env"]
                if not env_name.isupper() or not env_name.replace("_", "").isalnum():
                    errors.append(
                        f"{where}.api_key_env must look like an environment variable name "
                        f"(e.g. OPENAI_API_KEY), got {env_name!r}"
                    )
            trials = system.get("trials_per_task")
            if not isinstance(trials, int) or isinstance(trials, bool) or trials < 1:
                errors.append(f"{where}.trials_per_task must be a positive integer")
            agent_kwargs = system.get("agent_kwargs", {})
            if agent_kwargs is not None and not isinstance(agent_kwargs, dict):
                errors.append(f"{where}.agent_kwargs must be a mapping")
            estimated = system.get("estimated_cost_per_trial_usd")
            if estimated is not None and (not isinstance(estimated, (int, float)) or isinstance(estimated, bool) or estimated < 0):
                errors.append(f"{where}.estimated_cost_per_trial_usd must be a non-negative number")
            if system.get("name") is not None and not isinstance(system.get("name"), str):
                errors.append(f"{where}.name must be a string")

    execution = raw.get("execution")
    if execution is not None:
        if not isinstance(execution, dict):
            errors.append("'execution' must be a mapping")
        else:
            _check_unknown_keys(execution, _EXECUTION_KEYS, where="execution", errors=errors)
            concurrent = execution.get("max_concurrent_trials", 1)
            if not isinstance(concurrent, int) or isinstance(concurrent, bool) or concurrent < 1:
                errors.append("execution.max_concurrent_trials must be a positive integer")
            retries = execution.get("max_infra_retries", 0)
            if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
                errors.append("execution.max_infra_retries must be a non-negative integer")
            budget = execution.get("max_budget_usd")
            if budget is None or not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
                errors.append("execution.max_budget_usd is required and must be a positive number")
    else:
        errors.append("'execution' is required")

    reports = raw.get("reports")
    if reports is not None:
        if not isinstance(reports, dict):
            errors.append("'reports' must be a mapping")
        else:
            _check_unknown_keys(reports, _REPORTS_KEYS, where="reports", errors=errors)
            for visibility in ("private", "public"):
                target = reports.get(visibility)
                if target is None:
                    continue
                if not isinstance(target, dict):
                    errors.append(f"reports.{visibility} must be a mapping")
                    continue
                _check_unknown_keys(target, _REPORT_TARGET_KEYS, where=f"reports.{visibility}", errors=errors)
                if "enabled" in target and not isinstance(target["enabled"], bool):
                    errors.append(f"reports.{visibility}.enabled must be a boolean")
                if "path" in target and not isinstance(target["path"], str):
                    errors.append(f"reports.{visibility}.path must be a string")

    errors.extend(_validate_report_path_isolation(reports if isinstance(reports, dict) else {}))

    secret_paths = find_secret_like_values(raw)
    for path in secret_paths:
        errors.append(
            f"secret-like value detected at '{path}': configuration files must reference "
            "credentials only by environment-variable NAME (e.g. api_key_env: OPENAI_API_KEY), "
            "never by value"
        )

    return errors


def _is_within(candidate: str, root: str) -> bool:
    candidate_parts = Path(candidate.strip("/")).parts
    root_parts = Path(root.strip("/")).parts
    return candidate_parts[: len(root_parts)] == root_parts


def _validate_report_path_isolation(reports: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    public_path = (reports.get("public") or {}).get("path") or DEFAULT_PUBLIC_REPORTS_PATH
    private_path = (reports.get("private") or {}).get("path") or DEFAULT_PRIVATE_REPORTS_PATH

    if isinstance(public_path, str):
        for private_root in _PRIVATE_STORAGE_ROOTS:
            if _is_within(public_path, private_root):
                errors.append(
                    f"reports.public.path ({public_path!r}) must not point inside private "
                    f"storage ({private_root!r})"
                )
    if isinstance(private_path, str):
        for public_root in _PUBLIC_STORAGE_ROOTS:
            if _is_within(private_path, public_root):
                errors.append(
                    f"reports.private.path ({private_path!r}) must not point inside the "
                    f"public reports root ({public_root!r}); it would risk being staged/committed"
                )
    return errors


def _build_config(raw: dict[str, Any], *, source_path: Path | None) -> EvaluationConfig:
    systems = [
        SystemConfig(
            provider=s["provider"],
            model=s["model"],
            agent=s["agent"],
            environment=s["environment"],
            api_key_env=s["api_key_env"],
            trials_per_task=s["trials_per_task"],
            agent_kwargs=dict(s.get("agent_kwargs") or {}),
            estimated_cost_per_trial_usd=s.get("estimated_cost_per_trial_usd"),
            name=s.get("name"),
        )
        for s in raw["systems"]
    ]
    execution_raw = raw.get("execution") or {}
    execution = ExecutionConfig(
        max_concurrent_trials=execution_raw.get("max_concurrent_trials", 1),
        max_infra_retries=execution_raw.get("max_infra_retries", 0),
        max_budget_usd=execution_raw["max_budget_usd"],
    )
    reports_raw = raw.get("reports") or {}
    reports = ReportsConfig(
        private=ReportTargetConfig(
            enabled=(reports_raw.get("private") or {}).get("enabled", True),
            path=(reports_raw.get("private") or {}).get("path") or DEFAULT_PRIVATE_REPORTS_PATH,
        ),
        public=ReportTargetConfig(
            enabled=(reports_raw.get("public") or {}).get("enabled", True),
            path=(reports_raw.get("public") or {}).get("path") or DEFAULT_PUBLIC_REPORTS_PATH,
        ),
    )
    return EvaluationConfig(
        id=raw["id"],
        description=raw["description"],
        benchmark_version=raw["benchmark_version"],
        status=raw["status"],
        tasks=list(raw["tasks"]),
        systems=systems,
        execution=execution,
        reports=reports,
        source_path=source_path,
    )


def parse_config_text(text: str, *, source_path: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """Safely parse YAML text and validate it. Returns (raw_dict, errors)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    errors = validate_config_dict(raw)
    return raw, errors


def load_config(path: Path) -> EvaluationConfig:
    """Load and validate a config file, raising ConfigError on any problem."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    raw, errors = parse_config_text(path.read_text(), source_path=path)
    if errors:
        raise ConfigError("invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors))
    return _build_config(raw, source_path=path)


def config_to_dict(config: EvaluationConfig) -> dict[str, Any]:
    """Sanitized, canonical dict form of a config (safe to hash and persist --
    it never contains anything beyond what was in the source YAML, i.e. only
    an env-var *name*, never a credential value)."""
    return {
        "id": config.id,
        "description": config.description,
        "benchmark_version": config.benchmark_version,
        "status": config.status,
        "tasks": list(config.tasks),
        "systems": [
            {
                "name": s.name,
                "provider": s.provider,
                "model": s.model,
                "agent": s.agent,
                "environment": s.environment,
                "agent_kwargs": dict(s.agent_kwargs),
                "api_key_env": s.api_key_env,
                "trials_per_task": s.trials_per_task,
                "estimated_cost_per_trial_usd": s.estimated_cost_per_trial_usd,
            }
            for s in config.systems
        ],
        "execution": {
            "max_concurrent_trials": config.execution.max_concurrent_trials,
            "max_infra_retries": config.execution.max_infra_retries,
            "max_budget_usd": config.execution.max_budget_usd,
        },
        "reports": {
            "private": {"enabled": config.reports.private.enabled, "path": config.reports.private.path},
            "public": {"enabled": config.reports.public.enabled, "path": config.reports.public.path},
        },
    }
