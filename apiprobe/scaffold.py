"""Authoring helpers: scaffold a plan step from the spec, and check a plan against it (PLAN.md 19.7)."""
import re

from . import runner, spec as specs
from .body import BODY_KEYS
from .errors import ApiProbeError

FILE_PLACEHOLDER = "files/CHANGE_ME"
_STRING = "{{$uuid}}"


# ---------- scaffold ----------

def _schema(spec: dict, schema, visited=frozenset()):
    """Follow $ref and merge allOf; a ref already being followed comes back empty (recursive schemas)."""
    schema = schema if isinstance(schema, dict) else {}
    ref = schema.get("$ref")
    if ref:
        if ref in visited:
            return {"type": "object"}, visited  # recursive: stop one level down with an empty object
        visited = visited | {ref}
        schema = {**(specs._resolve(spec, ref) or {}), **{k: v for k, v in schema.items() if k != "$ref"}}
    return specs._merge_all_of(spec, schema, visited), visited


def _is_file(spec, schema) -> bool:
    s, _ = _schema(spec, schema)
    return s.get("type") == "string" and (s.get("format") == "binary" or "contentMediaType" in s)


def _value(spec: dict, schema, visited=frozenset(), depth=0):
    """A value for a schema: example, default, first enum value, else a typed placeholder (required properties only)."""
    schema, visited = _schema(spec, schema, visited)
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("oneOf", "anyOf"):
        if schema.get(key):
            return _value(spec, schema[key][0], visited, depth)
    t = schema.get("type")
    t = next((x for x in t if x != "null"), None) if isinstance(t, list) else t
    if t == "object" or (t is None and schema.get("properties")):
        if depth >= specs.MAX_DEPTH:
            return {}
        required = set(schema.get("required", []))
        return {k: _value(spec, v, visited, depth + 1) for k, v in (schema.get("properties") or {}).items() if k in required}
    if t == "array":
        return []
    if t in ("integer", "number"):
        return 0
    if t == "boolean":
        return False
    if t == "string" and (schema.get("format") == "binary" or "contentMediaType" in schema):
        return {"file": FILE_PLACEHOLDER}
    return _STRING


def _multipart_value(spec: dict, schema) -> dict:
    s, visited = _schema(spec, schema)
    required = set(s.get("required", []))
    out = {}
    for name, prop in (s.get("properties") or {}).items():
        if name not in required:
            continue
        p, _ = _schema(spec, prop, visited)
        if _is_file(spec, prop):
            out[name] = {"file": FILE_PLACEHOLDER}
        elif p.get("type") == "array" and _is_file(spec, p.get("items")):
            out[name] = [{"file": FILE_PLACEHOLDER}]
        elif p.get("type") == "object" or p.get("properties"):
            out[name] = {"json": _value(spec, prop, visited)}
        else:
            out[name] = _value(spec, prop, visited)
    return out


def body_key(content_type: str) -> str:
    """The plan key that sends a request body of this content type."""
    ct = content_type.lower()
    if "json" in ct:
        return "body"
    if ct == "application/x-www-form-urlencoded":
        return "form"
    if ct.startswith("multipart/"):
        return "multipart"
    if ct.startswith("text/") or "xml" in ct:
        return "text"
    return "file"


def _step_id(op: dict, method: str, template: str) -> str:
    name = op.get("operationId") or f"{method}_{template}"
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "step"


def _expected_status(op: dict) -> int:
    for code in (op.get("responses") or {}):
        if re.fullmatch(r"2\d\d", str(code)):
            return int(code)
        if str(code).lower() == "2xx":
            return 200
    return 200


def scaffold(spec: dict, method: str, path: str, role: str | None = None) -> dict:
    """One ready plan step for an endpoint: path variables as {{name}}, required query/body filled with placeholders."""
    method = method.upper()
    template, item, op = specs.find_operation(spec, method, path)
    call = {"method": method, "path": re.sub(r"\{(\w+)\}", r"{{\1}}", template)}
    if role:
        call["as"] = role
    query = {}
    for raw in item.get("parameters", []) + op.get("parameters", []):
        p = specs._deref(spec, raw)
        if p.get("in") == "query" and p.get("required"):
            query[p["name"]] = _value(spec, p.get("schema"))
    if query:
        call["query"] = query
    rb = specs._deref(spec, op.get("requestBody"))
    content = rb.get("content") or {}
    if content:
        ct = "application/json" if "application/json" in content else next(iter(content))
        schema = content[ct].get("schema")
        key = body_key(ct)
        call[key] = {"body": lambda: _value(spec, schema), "form": lambda: _value(spec, schema),
                     "multipart": lambda: _multipart_value(spec, schema), "text": lambda: "CHANGE_ME",
                     "file": lambda: FILE_PLACEHOLDER}[key]()
    return {"id": _step_id(op, method, template), "call": call, "expect": {"status": _expected_status(op)}}


# ---------- check ----------

def _top_level(rows) -> dict:
    """Top-level property name -> required, from a flattened field list."""
    return {r["property"]: r["required"] for r in rows if not re.search(r"[.\[]", r["property"])}


def _check_call(spec: dict, call: dict, where: str, problems: list, warnings: list):
    path = call["path"]
    if path.startswith("{{"):
        warnings.append(f"{where}.path is a variable; endpoint not checked against the spec")
        return
    try:
        d = specs.describe(spec, call["method"], path.split("?")[0])
    except ApiProbeError as e:
        problems.append(f"{where}: {e.message}")
        return
    query = call.get("query") if isinstance(call.get("query"), dict) else {}
    for p in d["parameters"]:
        if p["in"] == "query" and p["required"] and p["name"] not in query:
            problems.append(f"{where}.query: required query parameter '{p['name']}' is missing")
    keys = [k for k in BODY_KEYS if k in call]
    rb = d["requestBody"]
    if not rb:
        if keys:
            problems.append(f"{where}.{keys[0]}: {d['method']} {d['path']} takes no request body")
        return
    types = rb["contentTypes"]
    if not keys:
        if rb["required"]:
            problems.append(f"{where}: request body is required; send one of " + ", ".join(f"{t['contentType']} ({body_key(t['contentType'])})" for t in types))
        return
    key = keys[0]
    match = next((t for t in types if body_key(t["contentType"]) == key), None)
    if not match:
        problems.append(f"{where}.{key}: the endpoint accepts " + ", ".join(f"{t['contentType']} ({body_key(t['contentType'])})" for t in types))
        return
    if key in ("body", "form", "multipart") and isinstance(call[key], dict):
        known = _top_level(match["fields"])
        for name, required in known.items():
            if required and name not in call[key]:
                problems.append(f"{where}.{key}: required field '{name}' is missing")
        for name in call[key]:
            if known and name not in known:
                warnings.append(f"{where}.{key}: field '{name}' is not in the spec")


def check_plan(spec: dict, plan: dict, env: dict, files) -> tuple[list[str], list[str]]:
    """(problems, warnings). Plan structure first, then each call against the spec; nothing is sent to the API."""
    problems = runner.validate_plan(plan, env)
    warnings = []
    if problems:
        return problems, warnings
    for i, step in enumerate(plan["steps"]):
        _check_call(spec, step["call"], f"steps[{i}].call", problems, warnings)
    for i, call in enumerate(plan.get("cleanup", [])):
        _check_call(spec, call, f"cleanup[{i}]", problems, warnings)
    # upload and saveTo files last, so a missing file does not hide the spec problems above
    problems += [p for p in runner.validate_plan(plan, env, files) if p not in problems]
    return problems, warnings
