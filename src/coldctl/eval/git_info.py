"""Git commit/dirty-state inspection via subprocess (argument arrays only)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitInfo:
    commit: str | None
    dirty: bool
    available: bool


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def get_git_info(repo_root: Path) -> GitInfo:
    """Return the current commit hash and whether the working tree is dirty.

    ``available=False`` when git is not installed or ``repo_root`` is not a
    git repository; callers should treat that conservatively (as unverified).
    """
    head = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if head.returncode != 0:
        return GitInfo(commit=None, dirty=True, available=False)
    commit = head.stdout.strip()

    status = _run_git(["status", "--porcelain"], cwd=repo_root)
    dirty = status.returncode != 0 or bool(status.stdout.strip())
    return GitInfo(commit=commit, dirty=dirty, available=True)
