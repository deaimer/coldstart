"""Shared, minimal ColdStart/Harbor task-structure validation.

Factored out so both `coldctl validate` and `coldctl eval validate` use the
exact same check, without either importing the other's CLI module.
"""

from __future__ import annotations

from pathlib import Path


def find_missing_task_files(task_path: Path) -> list[str]:
    required = [
        "instruction.md",
        "task.toml",
        "solution/solve.sh",
        "tests/test.sh",
    ]
    missing = [relative for relative in required if not (task_path / relative).is_file()]

    environment_options = [
        "environment/Dockerfile",
        "environment/docker-compose.yaml",
        "environment/docker-compose.yml",
    ]
    if not any((task_path / option).is_file() for option in environment_options):
        missing.append("environment/Dockerfile or docker-compose.yaml")
    return missing
