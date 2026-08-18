from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coldctl.results import db as db_module  # noqa: E402


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / ".coldstart" / "results.db"


@pytest.fixture
def conn(db_path):
    connection = db_module.connect(db_path)
    yield connection
    connection.close()
