"""Plan validation, execution and assertions (PLAN.md 8.3-8.6, 10.1)."""
import json
import re
import time
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path

from . import http
from .assertions import check, expectation_problems, show as _show, status_problems, status_ok
from .body import check_call_body, check_save_to, resolve_file, resolve_multipart, resolve_output, summarize
from .config import get_user, resolve_env
from .errors import ApiProbeError
from .store import save_run
from .vars import lookup, substitute, valid_path

METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_ID_RE = re.compile(r"[a-z0-9_]+")
_VAR_NAME_RE = re.compile(r"[A-Za-z0-9_]+")


# ---------- plan loading / validation ----------

def load_plan(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ApiProbeError("PLAN_INVALID", f"plan file not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ApiProbeError("PLAN_INVALID", f"{p}: not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})")


def _check_call(call, where: str, env: dict, problems: list, files=None):
    if not isinstance(call, dict):
        problems.append(f"{where} must be an object")
        return
    if not isinstance(call.get("method"), str) or call["method"].upper() not in METHODS:
        problems.append(f"{where}.method must be one of {', '.join(sorted(METHODS))}")
    path = call.get("path")
    if not isinstance(path, str) or not path.startswith(("/", "{{")) or path.startswith("//"):  # "{{var}}" is checked after substitution
        problems.append(f"{where}.path must be a path starting with '/' (absolute URLs are not allowed), got {_show(path)}")
    role = call.get("as")
    if role is not None and role not in env["users"]:
        problems.append(f"{where}.as: role '{role}' not in config environment '{env['name']}' (known: {', '.join(env['users']) or 'none'})")
    for key in ("query", "headers"):
        if key in call and not isinstance(call[key], dict):
            problems.append(f"{where}.{key} must be an object")
    check_call_body(call, where, problems, files)
    check_save_to(call, where, problems, files)


def _valid_source(source: str) -> bool:
    kind, _, name = source.partition(":")
    return bool(name.strip()) if kind in ("header", "cookie") and source.startswith(kind + ":") else valid_path(source)


def validate_plan(plan, env: dict, files=None) -> list[str]:
    """files = (base folder, allowed roots) turns on upload-file checks (D15)."""
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
        _check_call(step.get("call"), f"{where}.call", env, problems, files)
        extract = step.get("extract", {})
        if not isinstance(extract, dict) or not all(isinstance(p, str) and _valid_source(p) for p in extract.values()):
            problems.append(f"{where}.extract must map variable names to dotted paths, 'header:NAME' or 'cookie:NAME'")
        expect = step.get("expect", {})
        if not isinstance(expect, dict):
            problems.append(f"{where}.expect must be an object")
            continue
        if "status" in expect:
            problems += status_problems(expect["status"], f"{where}.expect.status")
        json_expect = expect.get("json", {})
        if not isinstance(json_expect, dict):
            problems.append(f"{where}.expect.json must be an object")
        else:
            for p, exp in json_expect.items():
                if not valid_path(p):
                    problems.append(f"{where}.expect.json: invalid path '{p}'")
                problems += expectation_problems(exp, f"{where}.expect.json.{p}")
        headers_expect = expect.get("headers", {})
        if not isinstance(headers_expect, dict):
            problems.append(f"{where}.expect.headers must be an object of header name -> expectation")
        else:
            for name, exp in headers_expect.items():
                problems += expectation_problems(exp, f"{where}.expect.headers.{name}")
        problems += expectation_problems(expect.get("text"), f"{where}.expect.text")
        max_ms = expect.get("maxMs")
        if "maxMs" in expect and (isinstance(max_ms, bool) or not isinstance(max_ms, int) or max_ms < 0):
            problems.append(f"{where}.expect.maxMs must be a whole number of milliseconds")
    seed = plan.get("vars", {})
    if not isinstance(seed, dict) or not all(_VAR_NAME_RE.fullmatch(k) for k in seed):
        problems.append("vars must be an object whose keys are variable names (letters, digits, '_')")
    if "cookies" in plan and not isinstance(plan["cookies"], bool):
        problems.append("cookies must be true or false")
    cleanup = plan.get("cleanup", [])
    if not isinstance(cleanup, list):
        problems.append("cleanup must be a list")
        cleanup = []
    for i, call in enumerate(cleanup):
        _check_call(call, f"cleanup[{i}]", env, problems, files)
    checks = plan.get("manualChecks", [])
    if not isinstance(checks, list) or not all(isinstance(c, dict) and isinstance(c.get("sql"), str) for c in checks):
        problems.append("manualChecks must be a list of objects with a 'sql' string")
    return problems


def _roles(plan: dict) -> set:
    calls = [s["call"] for s in plan["steps"]] + plan.get("cleanup", [])
    return {c["as"] for c in calls if c.get("as")}


# ---------- execution ----------

def auth_rejected(resp: dict) -> bool:
    """401, or a 403 with no body at all (many APIs answer a bad token that way)."""
    return resp["status"] == 401 or (resp["status"] == 403 and not resp["bodyText"].strip())


def token_hint(app: dict, env: dict, role: str) -> str:
    return (f"token for role '{role}' may be expired or invalid: generate a new one and paste it into "
            f"field 'environments.{env['name']}.users.{role}.token' in {app['configPath']}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        return False


class _Session:
    """Per-run state: env, variables."""

    def __init__(self, app, env, files, cookies=False):
        self.app, self.env, self.variables = app, env, {}
        self.jar = CookieJar() if cookies else None  # per run, never saved
        self.base, self.roots = files
        self.timeout = app.get("timeoutSeconds", 30)

    def auth(self, role):
        return get_user(self.app, self.env, role) if role else None

    def prepare(self, call) -> dict:
        """Substitute variables; raises VAR_UNDEFINED before anything is sent."""
        out = {
            "method": call["method"].upper(),
            "path": substitute(call["path"], self.variables),
            "query": substitute(call.get("query"), self.variables),
            "headers": substitute(call.get("headers"), self.variables),
            "body": substitute(call.get("body"), self.variables),
            **{k: substitute(call.get(k), self.variables) for k in ("form", "multipart", "text", "file", "saveTo")},
        }
        if not isinstance(out["path"], str) or not out["path"].startswith("/") or out["path"].startswith("//"):
            raise ApiProbeError("PLAN_INVALID", f"path became {_show(out['path'])} after substitution; it must start with '/'")
        if out["saveTo"] is not None:
            out["saveTo"] = str(resolve_output(out["saveTo"], "call.saveTo", self.base, self.roots))
        if out["file"] is not None:
            out["file"] = str(resolve_file(out["file"], "call.file", self.base, self.roots))
        if out["multipart"] is not None:
            out["multipart"] = resolve_multipart(out["multipart"], self.base, self.roots)
        return out

    def send(self, req, role) -> dict:
        return http.send(self.env, self.timeout, req["method"], req["path"], req["query"], req["headers"], req["body"], self.auth(role),
                         form=req["form"], multipart=req["multipart"], text=req["text"], file=req["file"], save_to=req["saveTo"], jar=self.jar)


def _same_host_path(location: str, base_url: str) -> str:
    """A Location on this app's baseUrl becomes a path usable in call.path; anything else is kept as is."""
    if location.startswith(base_url) and location[len(base_url):len(base_url) + 1] in ("/", "?", "#"):
        return location[len(base_url):]
    return location


def _extract(source: str, resp: dict, headers: dict, is_json: bool, env: dict) -> tuple[bool, object]:
    kind, _, name = source.partition(":")
    if source.startswith("header:"):
        value = headers.get(name.strip().lower())
        return (False, None) if value is None else (True, _same_host_path(value, env["baseUrl"]))
    if source.startswith("cookie:"):
        value = resp["cookies"].get(name.strip())
        return (False, None) if value is None else (True, value)
    return lookup(resp["body"], source) if is_json else (False, None)


def _extract_missing(source: str) -> str:
    if source.startswith("header:"):
        return f"extract header '{source[7:]}' not in response"
    if source.startswith("cookie:"):
        return f"extract cookie '{source[7:]}' not set by this response"
    return f"extract path '{source}' not found in response"


def _run_step(step, s: _Session) -> dict:
    call = step["call"]
    result = {"id": step["id"], "status": "failed", "request": None, "response": None, "assertions": [],
              "extracted": {}, "error": None, "source": step.get("source")}
    try:
        req = s.prepare(call)
        result["request"] = {"method": req["method"], "url": http.build_url(s.env, req["path"], req["query"]), "body": req["body"],
                             **summarize(req)}
        resp = s.send(req, call.get("as"))
    except ApiProbeError as e:
        result["error"] = e.to_dict()
        return result

    is_json = resp["binary"] or _is_json(resp["bodyText"])  # a binary response's summary can be asserted on (bytes, sha256, contentType)
    result["response"] = {"status": resp["status"], "durationMs": resp["durationMs"],
                          "body": resp["body"] if is_json else resp["bodyText"]}
    if resp["saved"]:
        result["response"]["saved"] = resp["saved"]
    expect = step.get("expect", {})
    asserts = result["assertions"]
    want = expect.get("status")
    if want is None:
        ok = 200 <= resp["status"] < 300
        asserts.append({"target": "status", "expected": "2xx", "actual": resp["status"], "passed": ok,
                        "message": "" if ok else f"expected 2xx, got {resp['status']}"})
    else:
        ok = status_ok(want, resp["status"])
        asserts.append({"target": "status", "expected": want, "actual": resp["status"], "passed": ok,
                        "message": "" if ok else f"expected {_show(want)}, got {resp['status']}"})
    if auth_rejected(resp) and call.get("as") and asserts[0]["message"]:
        asserts[0]["message"] += f" ({token_hint(s.app, s.env, call['as'])})"
    for path, expectation in expect.get("json", {}).items():
        if is_json:
            found, value = lookup(resp["body"], path)
            ok, msg = check(expectation, found, value)
        else:
            value, ok, msg = None, False, "response is not JSON"
        asserts.append({"target": path, "expected": expectation, "actual": value, "passed": ok, "message": msg})
    headers = {k.lower(): v for k, v in resp["headers"].items()}
    for name, expectation in expect.get("headers", {}).items():
        found, value = name.lower() in headers, headers.get(name.lower())
        ok, msg = check(expectation, found, value)
        asserts.append({"target": f"header {name}", "expected": expectation, "actual": value, "passed": ok,
                        "message": "header not in response" if msg == "path not found" else msg})
    if "text" in expect:
        ok, msg = check(expect["text"], True, resp["bodyText"])
        asserts.append({"target": "text", "expected": expect["text"], "actual": resp["bodyText"], "passed": ok, "message": msg})
    if "maxMs" in expect:
        ok = resp["durationMs"] <= expect["maxMs"]
        asserts.append({"target": "maxMs", "expected": expect["maxMs"], "actual": resp["durationMs"], "passed": ok,
                        "message": "" if ok else f"took {resp['durationMs']}ms, limit {expect['maxMs']}ms"})
    for name, source in step.get("extract", {}).items():
        found, value = _extract(source, resp, headers, is_json, s.env)
        if found:
            result["extracted"][name] = value
            s.variables[name] = value
        else:
            asserts.append({"target": f"extract {name}", "expected": source, "actual": None, "passed": False,
                            "message": _extract_missing(source)})
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


def plan_files(app: dict, plan_path: str | None) -> tuple:
    """(folder relative file paths start at, folders files may live in): the plan's folder and the app repo (D15)."""
    base = Path(plan_path).parent if plan_path else Path.cwd()
    return base, [app.get("repoPath"), base]


def run_plan(plan: dict, app: dict, env_name: str | None = None, on_event=None, plan_path: str | None = None, only: str | None = None) -> dict:
    """only: run the steps up to and including this step id (cleanup still runs)."""
    emit = on_event or (lambda event: None)
    started, t0 = _now(), time.perf_counter()
    result = {"app": app["name"], "environment": None, "title": plan.get("title") if isinstance(plan, dict) else None,
              "planPath": plan_path, "startedAt": started, "durationMs": 0, "status": "error", "steps": [],
              "cleanup": [], "manualChecks": [], "variables": {}, "error": None, "savedTo": None}
    files = plan_files(app, plan_path)
    try:
        env = resolve_env(app, env_name or (plan.get("environment") if isinstance(plan, dict) else None))
        result["environment"] = env["name"]
        problems = validate_plan(plan, env, files)
        if problems:
            raise ApiProbeError("PLAN_INVALID", f"{plan_path or 'plan'}: " + "; ".join(problems))
        ids = [s["id"] for s in plan["steps"]]
        if only is not None and only not in ids:
            raise ApiProbeError("PLAN_INVALID", f"--only '{only}': no step with that id (steps: {', '.join(ids)})")
        for role in sorted(_roles(plan)):
            get_user(app, env, role)  # fail before sending anything if a used role is still CHANGE_ME
    except ApiProbeError as e:
        result["error"] = e.to_dict()
        result["durationMs"] = round((time.perf_counter() - t0) * 1000)
        save_run(app["name"], result)
        return result

    s = _Session(app, env, files, plan.get("cookies", False))
    s.variables.update(plan.get("vars", {}))
    failed = False
    steps = plan["steps"] if only is None else plan["steps"][:ids.index(only) + 1]
    for step in steps:
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
