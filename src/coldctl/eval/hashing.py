"""Deterministic hashing: task directory contents and canonical config."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

#: Directories never relevant to task content identity (VCS metadata, caches,
#: bytecode) and safe to exclude from the recursive task hash.
_EXCLUDED_DIR_NAMES = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


def canonical_json_hash(value: Any) -> str:
    """Stable sha256 over a canonical (sorted-key, no-whitespace) JSON
    encoding, so semantically identical configs always hash identically."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def hash_task_directory(task_path: Path) -> str:
    """Recursively hash every file under ``task_path``: a single sha256 over
    the sorted sequence of (relative_path, file_sha256) pairs. Used to detect
    task drift between plan/run time and a later resume, independent of
    Harbor's own per-run lock digest (which does not exist until Harbor has
    actually built/locked the task)."""
    task_path = Path(task_path)
    entries: list[tuple[str, str]] = []
    for path in sorted(task_path.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _EXCLUDED_DIR_NAMES for part in path.relative_to(task_path).parts):
            continue
        relative = path.relative_to(task_path).as_posix()
        file_digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                file_digest.update(chunk)
        entries.append((relative, file_digest.hexdigest()))

    combined = hashlib.sha256()
    for relative, digest in entries:
        combined.update(relative.encode())
        combined.update(b"\0")
        combined.update(digest.encode())
        combined.update(b"\0")
    return combined.hexdigest()
