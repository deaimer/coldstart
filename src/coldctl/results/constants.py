"""Shared constants for the ColdStart results system."""

from __future__ import annotations

#: The five diagnostic ColdStart dimensions. ``coldstart_pass`` is
#: deliberately excluded here: it is the strict pass signal and must never be
#: averaged in with these when computing "dimension" aggregates.
DIMENSIONS: tuple[str, ...] = (
    "functional",
    "durability",
    "state_safety",
    "integrity",
    "evidence",
)

#: The reward key that, and only that, determines strict pass/fail.
STRICT_PASS_KEY = "coldstart_pass"
