"""{{var}} substitution and dotted path lookup (PLAN.md 7.5, 8.4)."""
import json
import re
import uuid
from datetime import datetime, timezone

from .errors import ApiProbeError

_PATH_RE = re.compile(r"(?:[^.\[\]]+|\[\d+\])(?:\.[^.\[\]]+|\[\d+\])*")
_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
_VAR_RE = re.compile(r"\{\{\s*(\$?[A-Za-z0-9_]+)\s*\}\}")


def valid_path(path: str) -> bool:
    return path == "" or _PATH_RE.fullmatch(path) is not None


def lookup(data, path: str) -> tuple[bool, object]:
    """Return (found, value). Missing key / index out of range -> (False, None)."""
    if not valid_path(path):
        return False, None
    current = data
    for key, index in _TOKEN_RE.findall(path):
        if key:
            if not isinstance(current, dict) or key not in current:
                return False, None
            current = current[key]
        else:
            i = int(index)
            if not isinstance(current, list) or i >= len(current):
                return False, None
            current = current[i]
    return True, current


def _resolve(name: str, variables: dict):
    if name == "$uuid":
        return str(uuid.uuid4())
    if name == "$now":
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if name not in variables:
        raise ApiProbeError(
            "VAR_UNDEFINED",
            f"variable '{{{{{name}}}}}' is not defined; set it with 'extract' in an earlier step "
            f"or use a built-in ($uuid, $now)",
        )
    return variables[name]


def _as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def substitute(value, variables: dict):
    """Recursively replace {{name}} in all strings. Exact '{{name}}' keeps the value's type."""
    if isinstance(value, str):
        exact = _VAR_RE.fullmatch(value)
        if exact:
            return _resolve(exact.group(1), variables)
        return _VAR_RE.sub(lambda m: _as_text(_resolve(m.group(1), variables)), value)
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value
