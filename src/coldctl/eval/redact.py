"""Secret-like value detection and redaction.

Used both to reject configs that accidentally embed credential material and
to sanitize anything (Harbor command arguments, exception messages) that
gets written into a manifest, state file, event log, or report.

This never reads real credential values from the environment; it only
pattern-matches strings that are already in front of us (config values,
command-line arguments, exception text) to decide whether they look like
a secret and should be masked before being persisted or printed.
"""

from __future__ import annotations

import re
from typing import Any

_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT-shaped
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack-style
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),  # Google API key
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
)

#: Key-name substrings that warrant extra scrutiny of their value. Fields
#: that legitimately hold an *environment variable name* (e.g. `api_key_env:
#: OPENAI_API_KEY`) are excluded by the `_ENV`/`env` suffix + shape check.
_SECRET_KEY_HINTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "private_key",
    "access_key",
)

_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def looks_like_env_var_name(value: str) -> bool:
    """True for values shaped like an environment variable NAME (not a secret)."""
    return bool(_ENV_VAR_NAME_RE.match(value)) and len(value) <= 128


def looks_like_secret_value(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def find_secret_like_values(node: Any, path: str = "") -> list[str]:
    """Recursively scan a parsed config/data tree for values that look like
    embedded credentials. Returns a list of dotted/indexed paths where a
    likely secret was found. Field names ending in ``_env`` whose value is
    shaped like an environment variable name (e.g. ``OPENAI_API_KEY``) are
    intentionally exempted, since that is the correct way to reference a
    credential in a ColdStart evaluation config.
    """
    findings: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            key_lower = str(key).lower()
            if isinstance(value, str):
                if key_lower.endswith("_env") and looks_like_env_var_name(value):
                    continue
                matched_pattern = looks_like_secret_value(value)
                looks_like_secret_key = any(hint in key_lower for hint in _SECRET_KEY_HINTS)
                if matched_pattern or (looks_like_secret_key and not looks_like_env_var_name(value)):
                    findings.append(child_path)
            else:
                findings.extend(find_secret_like_values(value, child_path))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            findings.extend(find_secret_like_values(item, f"{path}[{index}]"))
    return findings


def redact_text(text: str) -> str:
    """Mask any secret-shaped substring found in free text (e.g. exception
    messages, subprocess stderr) before it is stored or printed."""
    redacted = text
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_argv(argv: list[str]) -> list[str]:
    """Redact a subprocess argument vector for safe logging: any argument
    that looks like a secret value outright is masked, and the value
    following a flag whose name looks credential-related is masked too
    (covers ``--ae KEY=value`` style pass-throughs where value might be a
    literal secret rather than an env-var reference)."""
    redacted: list[str] = []
    previous = ""
    for arg in argv:
        lowered = previous.lower()
        if any(hint in lowered for hint in _SECRET_KEY_HINTS) and "=" not in arg:
            redacted.append("[REDACTED]")
        elif "=" in arg and any(hint in arg.split("=", 1)[0].lower() for hint in _SECRET_KEY_HINTS):
            key, _, value = arg.partition("=")
            redacted.append(arg if looks_like_env_var_name(value) else f"{key}=[REDACTED]")
        elif looks_like_secret_value(arg):
            redacted.append("[REDACTED]")
        else:
            redacted.append(arg)
        previous = arg
    return redacted
