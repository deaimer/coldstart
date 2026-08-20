from __future__ import annotations

from coldctl.eval.config import ConfigError, load_config, parse_config_text, validate_config_dict

VALID_YAML = """
id: my-eval
description: a valid evaluation
benchmark_version: "0.1.0"
status: development
tasks:
  - benchmark/sample-tasks/artifact-vault-recovery
systems:
  - provider: openai
    model: openai/gpt-5.6-terra
    agent: terminus-2
    environment: docker
    agent_kwargs:
      reasoning_effort: medium
      use_responses_api: true
      max_turns: 30
    api_key_env: OPENAI_API_KEY
    trials_per_task: 5
execution:
  max_concurrent_trials: 1
  max_infra_retries: 2
  max_budget_usd: 2.00
reports:
  private:
    enabled: true
  public:
    enabled: true
"""


def test_valid_config_has_no_errors():
    raw, errors = parse_config_text(VALID_YAML)
    assert errors == []
    assert raw["id"] == "my-eval"


def test_valid_config_loads_into_dataclass(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID_YAML)
    config = load_config(path)
    assert config.id == "my-eval"
    assert config.systems[0].system_key == "gpt-5.6-terra__terminus-2"
    assert config.execution.max_budget_usd == 2.00


def test_unknown_top_level_field_rejected():
    raw, errors = parse_config_text(VALID_YAML + "\nnot_a_real_field: true\n")
    assert any("unknown field: not_a_real_field" in e for e in errors)


def test_unknown_system_field_rejected():
    bad = VALID_YAML.replace("trials_per_task: 5", "trials_per_task: 5\n    bogus_field: 1")
    raw, errors = parse_config_text(bad)
    assert any("unknown field: systems[0].bogus_field" in e for e in errors)


def test_secret_like_api_key_value_rejected():
    bad = VALID_YAML.replace(
        "api_key_env: OPENAI_API_KEY",
        'api_key_env: OPENAI_API_KEY\n    api_key: "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"',
    )
    raw, errors = parse_config_text(bad)
    assert any("secret-like value" in e for e in errors)


def test_legitimate_env_var_name_field_is_not_flagged_as_secret():
    # api_key_env holding an env-var NAME (all caps identifier) must never
    # itself be treated as a secret value.
    raw, errors = parse_config_text(VALID_YAML)
    assert not any("secret-like value" in e for e in errors)


def test_secret_like_value_anywhere_in_agent_kwargs_rejected():
    bad = VALID_YAML.replace(
        "reasoning_effort: medium",
        "reasoning_effort: medium\n      token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",
    )
    raw, errors = parse_config_text(bad)
    assert any("secret-like value" in e for e in errors)


def test_invalid_status_value_rejected():
    bad = VALID_YAML.replace("status: development", "status: bogus")
    raw, errors = parse_config_text(bad)
    assert any("status" in e for e in errors)


def test_missing_required_field_rejected():
    bad = VALID_YAML.replace('description: a valid evaluation\n', "")
    raw, errors = parse_config_text(bad)
    assert any("description" in e for e in errors)


def test_non_positive_budget_rejected():
    bad = VALID_YAML.replace("max_budget_usd: 2.00", "max_budget_usd: 0")
    raw, errors = parse_config_text(bad)
    assert any("max_budget_usd" in e for e in errors)


def test_zero_trials_per_task_rejected():
    bad = VALID_YAML.replace("trials_per_task: 5", "trials_per_task: 0")
    raw, errors = parse_config_text(bad)
    assert any("trials_per_task" in e for e in errors)


def test_public_report_path_inside_private_storage_rejected():
    bad = VALID_YAML + "\n" if "reports:" in VALID_YAML else VALID_YAML
    bad = VALID_YAML.replace(
        "reports:\n  private:\n    enabled: true\n  public:\n    enabled: true\n",
        "reports:\n  private:\n    enabled: true\n  public:\n    enabled: true\n    path: .coldstart/oops\n",
    )
    raw, errors = parse_config_text(bad)
    assert any("must not point inside private storage" in e for e in errors)


def test_private_report_path_inside_public_root_rejected():
    bad = VALID_YAML.replace(
        "reports:\n  private:\n    enabled: true\n  public:\n    enabled: true\n",
        "reports:\n  private:\n    enabled: true\n    path: reports/oops\n  public:\n    enabled: true\n",
    )
    raw, errors = parse_config_text(bad)
    assert any("public reports root" in e for e in errors)


def test_invalid_yaml_raises_config_error():
    try:
        parse_config_text("not: valid: yaml: [")
        assert False, "expected ConfigError"
    except ConfigError:
        pass


def test_non_mapping_top_level_rejected():
    errors = validate_config_dict(["not", "a", "mapping"])
    assert errors and "mapping" in errors[0]
