"""Expectations: deep equality plus one-key operators, and status matching (PLAN.md 8.4, 19.6)."""
import json
import re

NUMERIC = ("gt", "gte", "lt", "lte")
OPERATORS = ("exists", "contains", "matches", "length", "type", "in", "equals", *NUMERIC)
JSON_TYPES = ("string", "number", "integer", "boolean", "array", "object", "null")
_CLASS_RE = re.compile(r"[1-5]xx")


def show(v) -> str:
    return json.dumps(v)


def _equal(a, b) -> bool:
    """JSON deep-equal: true != 1, 1 == 1.0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _json_type(value) -> set:
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int) or isinstance(value, float) and value.is_integer():
        return {"integer", "number"}
    if isinstance(value, float):
        return {"number"}
    return {"string" if isinstance(value, str) else "array" if isinstance(value, list) else "object"}


def _operator(expectation):
    """(name, argument) when expectation is an object whose only key is an operator, else None."""
    if isinstance(expectation, dict) and len(expectation) == 1 and next(iter(expectation)) in OPERATORS:
        return next(iter(expectation.items()))
    return None


# ---------- operators: (argument, value) -> (passed, message) ----------

def _contains(x, value):
    if isinstance(value, str):
        ok = isinstance(x, str) and x in value
    elif isinstance(value, list):
        ok = any(_equal(e, x) for e in value)
    else:
        return False, f"contains needs a string or list, got {type(value).__name__} {show(value)}"
    return (True, "") if ok else (False, f"expected to contain {show(x)}, got {show(value)}")


def _matches(pattern, value):
    if not isinstance(value, str):
        return False, f"matches needs a string, got {show(value)}"
    return (True, "") if re.search(pattern, value) else (False, f"expected to match /{pattern}/, got {show(value)}")


def _length(arg, value):
    if not isinstance(value, (str, list, dict)):
        return False, f"length needs a string, list or object, got {show(value)}"
    n = len(value)
    if isinstance(arg, dict):
        ok, msg = check(arg, True, n)
        return (True, "") if ok else (False, f"length is {n}: {msg}")
    return (True, "") if n == arg else (False, f"expected length {arg}, got {n} ({show(value)})")


def _type(arg, value):
    return (True, "") if arg in _json_type(value) else (False, f"expected type {arg}, got {show(value)}")


def _compare(name):
    ops = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}
    words = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

    def run(arg, value):
        if not _number(value):
            return False, f"{name} needs a number, got {show(value)}"
        return (True, "") if ops[name](value, arg) else (False, f"expected a number {words[name]} {show(arg)}, got {show(value)}")
    return run


def _in(options, value):
    return (True, "") if any(_equal(value, o) for o in options) else (False, f"expected one of {show(options)}, got {show(value)}")


def _equals(literal, value):
    return (True, "") if _equal(value, literal) else (False, f"expected {show(literal)}, got {show(value)}")


_RUN = {"contains": _contains, "matches": _matches, "length": _length, "type": _type, "in": _in, "equals": _equals,
        **{n: _compare(n) for n in NUMERIC}}


def check(expectation, found: bool, value) -> tuple[bool, str]:
    op = _operator(expectation)
    if op and op[0] == "exists":
        if found == bool(op[1]):
            return True, ""
        return False, "path not found" if op[1] else f"expected path to be absent, got {show(value)}"
    if not found:
        return False, "path not found"
    if op:
        return _RUN[op[0]](op[1], value)
    if _equal(value, expectation):
        return True, ""
    return False, f"expected {show(expectation)}, got {show(value)}"


# ---------- plan validation ----------

_HINT = " (an object that only has this key is read as an operator; to compare with such a literal object write {\"equals\": {...}})"


def expectation_problems(expectation, where: str) -> list[str]:
    """Shape errors of an operator expectation; a plain value or exists/contains/equals has none."""
    return [p + _HINT for p in _shape_problems(expectation, where)]


def _shape_problems(expectation, where: str) -> list[str]:
    op = _operator(expectation)
    if not op:
        return []
    name, arg = op
    if name == "matches":
        try:
            if not isinstance(arg, str):
                raise TypeError
            re.compile(arg)
        except (re.error, TypeError):
            return [f"{where}: matches needs a valid regular expression string, got {show(arg)}"]
    elif name == "length":
        nested = _operator(arg)
        ok = (isinstance(arg, int) and not isinstance(arg, bool) and arg >= 0) or (nested and nested[0] in NUMERIC and _number(nested[1]))
        if not ok:
            return [f"{where}: length needs a whole number or one of {{\"gte\": N}}, {{\"lt\": N}} etc., got {show(arg)}"]
    elif name == "type":
        if arg not in JSON_TYPES:
            return [f"{where}: type must be one of {', '.join(JSON_TYPES)}, got {show(arg)}"]
    elif name in NUMERIC:
        if not _number(arg):
            return [f"{where}: {name} needs a number, got {show(arg)}"]
    elif name == "in":
        if not isinstance(arg, list):
            return [f"{where}: in needs a list, got {show(arg)}"]
    return []


def status_problems(want, where: str) -> list[str]:
    def one(v):
        return isinstance(v, int) and not isinstance(v, bool)
    if one(want) or isinstance(want, str) and _CLASS_RE.fullmatch(want):
        return []
    if isinstance(want, list) and want and all(one(v) for v in want):
        return []
    return [f"{where} must be an integer, a non-empty list of integers, or a class like \"2xx\", got {show(want)}"]


def status_ok(want, status: int) -> bool:
    if isinstance(want, list):
        return status in want
    if isinstance(want, str):
        return str(status).startswith(want[0])
    return status == want
