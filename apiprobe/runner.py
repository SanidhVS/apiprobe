"""Plan validation, execution and assertions (PLAN.md 8.3-8.6, 10.1)."""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import http
from .config import get_user, resolve_env
from .errors import ApiProbeError
from .store import save_run
from .vars import lookup, substitute, valid_path

METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_ID_RE = re.compile(r"[a-z0-9_]+")


# ---------- assertions ----------

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


def _show(v) -> str:
    return json.dumps(v)


def check(expectation, found: bool, value) -> tuple[bool, str]:
    is_op = isinstance(expectation, dict) and len(expectation) == 1 and next(iter(expectation)) in ("exists", "contains")
    if is_op and "exists" in expectation:
        want = expectation["exists"]
        if found == bool(want):
            return True, ""
        return False, "path not found" if want else f"expected path to be absent, got {_show(value)}"
    if not found:
        return False, "path not found"
    if is_op:
        x = expectation["contains"]
        if isinstance(value, str):
            ok = isinstance(x, str) and x in value
        elif isinstance(value, list):
            ok = any(_equal(e, x) for e in value)
        else:
            return False, f"contains needs a string or list, got {type(value).__name__} {_show(value)}"
        return (True, "") if ok else (False, f"expected to contain {_show(x)}, got {_show(value)}")
    if _equal(value, expectation):
        return True, ""
    return False, f"expected {_show(expectation)}, got {_show(value)}"


# ---------- plan loading / validation ----------

def load_plan(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ApiProbeError("PLAN_INVALID", f"plan file not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ApiProbeError("PLAN_INVALID", f"{p}: not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})")


def _check_call(call, where: str, env: dict, problems: list):
    if not isinstance(call, dict):
        problems.append(f"{where} must be an object")
        return
    if not isinstance(call.get("method"), str) or call["method"].upper() not in METHODS:
        problems.append(f"{where}.method must be one of {', '.join(sorted(METHODS))}")
    path = call.get("path")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        problems.append(f"{where}.path must be a path starting with '/' (absolute URLs are not allowed), got {_show(path)}")
    role = call.get("as")
    if role is not None and role not in env["users"]:
        problems.append(f"{where}.as: role '{role}' not in config environment '{env['name']}' (known: {', '.join(env['users']) or 'none'})")
    for key in ("query", "headers"):
        if key in call and not isinstance(call[key], dict):
            problems.append(f"{where}.{key} must be an object")


def validate_plan(plan, env: dict) -> list[str]:
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    problems = []
    if not isinstance(plan.get("title"), str) or not plan["title"].strip():
        problems.append("title is required")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("steps must be a non-empty list")
        steps = []
    seen = set()
    for i, step in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(step, dict):
            problems.append(f"{where} must be an object")
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
            problems.append(f"{where}.id must match [a-z0-9_]+, got {_show(sid)}")
        elif sid in seen:
            problems.append(f"{where}.id '{sid}' is duplicated")
        seen.add(sid)
        _check_call(step.get("call"), f"{where}.call", env, problems)
        extract = step.get("extract", {})
        if not isinstance(extract, dict) or not all(isinstance(p, str) and valid_path(p) for p in extract.values()):
            problems.append(f"{where}.extract must map variable names to dotted paths")
        expect = step.get("expect", {})
        if not isinstance(expect, dict):
            problems.append(f"{where}.expect must be an object")
            continue
        status = expect.get("status")
        if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
            problems.append(f"{where}.expect.status must be an integer")
        json_expect = expect.get("json", {})
        if not isinstance(json_expect, dict):
            problems.append(f"{where}.expect.json must be an object")
        else:
            for p in json_expect:
                if not valid_path(p):
                    problems.append(f"{where}.expect.json: invalid path '{p}'")
    cleanup = plan.get("cleanup", [])
    if not isinstance(cleanup, list):
        problems.append("cleanup must be a list")
        cleanup = []
    for i, call in enumerate(cleanup):
        _check_call(call, f"cleanup[{i}]", env, problems)
    checks = plan.get("manualChecks", [])
    if not isinstance(checks, list) or not all(isinstance(c, dict) and isinstance(c.get("sql"), str) for c in checks):
        problems.append("manualChecks must be a list of objects with a 'sql' string")
    return problems


def _roles(plan: dict) -> set:
    calls = [s["call"] for s in plan["steps"]] + plan.get("cleanup", [])
    return {c["as"] for c in calls if c.get("as")}


# ---------- execution ----------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        return False


class _Session:
    """Per-run state: env, lazily-fetched tokens, variables."""

    def __init__(self, app, env):
        self.app, self.env, self.tokens, self.variables = app, env, {}, {}
        self.timeout = app.get("timeoutSeconds", 30)

    def token(self, role):
        if not role:
            return None
        if role not in self.tokens:
            self.tokens[role] = http.login(self.app, self.env, role)
        return self.tokens[role]

    def prepare(self, call) -> dict:
        """Substitute variables; raises VAR_UNDEFINED before anything is sent."""
        out = {
            "method": call["method"].upper(),
            "path": substitute(call["path"], self.variables),
            "query": substitute(call.get("query"), self.variables),
            "headers": substitute(call.get("headers"), self.variables),
            "body": substitute(call.get("body"), self.variables),
        }
        if not isinstance(out["path"], str) or not out["path"].startswith("/") or out["path"].startswith("//"):
            raise ApiProbeError("PLAN_INVALID", f"path became {_show(out['path'])} after substitution; it must start with '/'")
        return out

    def send(self, req, role) -> dict:
        return http.send(self.env, self.timeout, req["method"], req["path"], req["query"], req["headers"], req["body"], self.token(role))


def _run_step(step, s: _Session) -> dict:
    call = step["call"]
    result = {"id": step["id"], "status": "failed", "request": None, "response": None, "assertions": [],
              "extracted": {}, "error": None, "source": step.get("source")}
    try:
        req = s.prepare(call)
        result["request"] = {"method": req["method"], "url": http.build_url(s.env, req["path"], req["query"]), "body": req["body"]}
        resp = s.send(req, call.get("as"))
    except ApiProbeError as e:
        result["error"] = e.to_dict()
        return result

    is_json = _is_json(resp["bodyText"])
    result["response"] = {"status": resp["status"], "durationMs": resp["durationMs"],
                          "body": resp["body"] if is_json else resp["bodyText"]}
    expect = step.get("expect", {})
    asserts = result["assertions"]
    want = expect.get("status")
    if want is None:
        ok = 200 <= resp["status"] < 300
        asserts.append({"target": "status", "expected": "2xx", "actual": resp["status"], "passed": ok,
                        "message": "" if ok else f"expected 2xx, got {resp['status']}"})
    else:
        ok = resp["status"] == want
        asserts.append({"target": "status", "expected": want, "actual": resp["status"], "passed": ok,
                        "message": "" if ok else f"expected {want}, got {resp['status']}"})
    for path, expectation in expect.get("json", {}).items():
        if is_json:
            found, value = lookup(resp["body"], path)
            ok, msg = check(expectation, found, value)
        else:
            value, ok, msg = None, False, "response is not JSON"
        asserts.append({"target": path, "expected": expectation, "actual": value, "passed": ok, "message": msg})
    for name, path in step.get("extract", {}).items():
        found, value = lookup(resp["body"], path) if is_json else (False, None)
        if found:
            result["extracted"][name] = value
            s.variables[name] = value
        else:
            asserts.append({"target": f"extract {name}", "expected": path, "actual": None, "passed": False,
                            "message": f"extract path '{path}' not found in response"})
    if all(a["passed"] for a in asserts):
        result["status"] = "passed"
    return result


def _run_cleanup(call, s: _Session) -> dict:
    entry = {"method": call["method"].upper(), "path": call["path"], "status": "skipped", "responseStatus": None, "error": None}
    try:
        req = s.prepare(call)
    except ApiProbeError as e:
        entry["error"] = e.to_dict()
        return entry
    entry["path"] = req["path"]
    try:
        resp = s.send(req, call.get("as"))
    except ApiProbeError as e:
        entry.update(status="failed", error=e.to_dict())
        return entry
    entry["responseStatus"] = resp["status"]
    entry["status"] = "passed" if 200 <= resp["status"] < 300 else "failed"
    return entry


def _manual_checks(plan, variables) -> list:
    out = []
    for c in plan.get("manualChecks", []):
        entry = {"sql": c["sql"], "expect": c.get("expect"), "why": c.get("why")}
        try:
            entry["sql"] = substitute(c["sql"], variables)
        except ApiProbeError as e:
            entry["note"] = f"not all variables are set ({e.message})"
        out.append(entry)
    return out


def run_plan(plan: dict, app: dict, env_name: str | None = None, on_event=None, plan_path: str | None = None) -> dict:
    emit = on_event or (lambda event: None)
    started, t0 = _now(), time.perf_counter()
    result = {"app": app["name"], "environment": None, "title": plan.get("title") if isinstance(plan, dict) else None,
              "planPath": plan_path, "startedAt": started, "durationMs": 0, "status": "error", "steps": [],
              "cleanup": [], "manualChecks": [], "variables": {}, "error": None, "savedTo": None}
    try:
        env = resolve_env(app, env_name or (plan.get("environment") if isinstance(plan, dict) else None))
        result["environment"] = env["name"]
        problems = validate_plan(plan, env)
        if problems:
            raise ApiProbeError("PLAN_INVALID", f"{plan_path or 'plan'}: " + "; ".join(problems))
        for role in sorted(_roles(plan)):
            get_user(app, env, role)  # fail before sending anything if a used role is still CHANGE_ME
    except ApiProbeError as e:
        result["error"] = e.to_dict()
        result["durationMs"] = round((time.perf_counter() - t0) * 1000)
        save_run(app["name"], result)
        return result

    s = _Session(app, env)
    failed = False
    for step in plan["steps"]:
        if failed:
            result["steps"].append({"id": step["id"], "status": "skipped", "request": None, "response": None,
                                    "assertions": [], "extracted": {}, "error": None, "source": step.get("source")})
            emit({"type": "step_finished", "id": step["id"], "status": "skipped", "durationMs": 0})
            continue
        emit({"type": "step_started", "id": step["id"], "method": step["call"]["method"].upper(), "path": step["call"]["path"]})
        r = _run_step(step, s)
        result["steps"].append(r)
        emit({"type": "step_finished", "id": r["id"], "status": r["status"],
              "durationMs": (r["response"] or {}).get("durationMs", 0)})
        failed = r["status"] != "passed"

    for call in plan.get("cleanup", []):
        c = _run_cleanup(call, s)
        result["cleanup"].append(c)
        emit({"type": "cleanup_finished", "path": c["path"], "status": c["status"]})

    result["manualChecks"] = _manual_checks(plan, s.variables)
    result["variables"] = s.variables
    result["status"] = "failed" if failed else "passed"
    result["durationMs"] = round((time.perf_counter() - t0) * 1000)
    save_run(app["name"], result)
    return result
