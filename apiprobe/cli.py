"""Thin CLI over the core: argparse, printing, exit codes (PLAN.md section 11)."""
import argparse
import json
import sys
import traceback
import urllib.parse
from pathlib import Path

from . import __version__, config, http, runner, spec, store
from .brief import brief_call, brief_run, pick, truncate as _truncate
from .body import BODY_KEYS, resolve_file
from .errors import ApiProbeError

BODY_LIMIT = 1500


def _body_text(body) -> str:
    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)


def _app(args) -> dict:
    return config.load_app(args.app) if args.app else config.detect_app(Path.cwd())


def _check_path(path: str, code: str):
    if path.startswith("/") and not path.startswith("//"):
        return
    hint = " Git Bash rewrote the path; rerun with MSYS_NO_PATHCONV=1 set." if len(path) > 1 and path[1] == ":" else ""
    raise ApiProbeError(code, f"path must start with '/', got '{path}' (absolute URLs are not allowed).{hint}")


def _kv(pairs, flag) -> dict:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ApiProbeError("PLAN_INVALID", f"{flag} expects k=v, got '{pair}'")
        k, v = pair.split("=", 1)
        out[k] = v
    return out


def _multi(pairs, flag) -> dict:
    """k=v pairs; a repeated key becomes a list."""
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise ApiProbeError("PLAN_INVALID", f"{flag} expects k=v, got '{pair}'")
        k, v = pair.split("=", 1)
        out[k] = [*out[k], v] if isinstance(out.get(k), list) else [out[k], v] if k in out else v
    return out


def _part(value: str, where: str, base, roots):
    """--part value: plain text, or @path[;type=mime][;filename=x] for a file part."""
    if not value.startswith("@"):
        return value
    path, *opts = value[1:].split(";")
    part = {"file": str(resolve_file(path, where, base, roots))}
    for opt in opts:
        key, _, val = opt.partition("=")
        if key not in ("type", "filename") or not val:
            raise ApiProbeError("PLAN_INVALID", f"{where}: expected ';type=MIME' or ';filename=NAME' after the path, got ';{opt}'")
        part["contentType" if key == "type" else "filename"] = val
    return part


def _body_args(args, app) -> dict:
    """Body flags -> http.send keywords; files must sit inside the app repo or the current folder (D15)."""
    given = {"body": args.body, "form": args.form, "multipart": args.part, "text": args.text, "file": args.file}
    used = [k for k in BODY_KEYS if given[k] is not None]
    if len(used) > 1:
        raise ApiProbeError("PLAN_INVALID", f"use only one body flag, got {', '.join('--' + ('part' if k == 'multipart' else k) for k in used)}")
    base, roots = Path.cwd(), [app.get("repoPath"), Path.cwd()]
    if args.form:
        return {"form": _multi(args.form, "--form")}
    if args.part:
        return {"multipart": {k: [_part(x, f"--part {k}", base, roots) for x in v] if isinstance(v, list) else _part(v, f"--part {k}", base, roots)
                              for k, v in _multi(args.part, "--part").items()}}
    if args.text is not None:
        if args.text.startswith("@"):
            return {"text": resolve_file(args.text[1:], "--text", base, roots).read_text(encoding="utf-8-sig")}
        return {"text": args.text}
    if args.file is not None:
        return {"file": str(resolve_file(args.file.removeprefix("@"), "--file", base, roots))}
    return {}


# ---------- commands: each returns (result, exit_code, human_printer) ----------

def _auth_label(s: dict) -> str:
    if s["type"] == "http":
        return f"http {s.get('scheme', '?')}"
    if s["type"] == "apiKey":
        return f"{s.get('param')} {s.get('in')} (apiKey)"
    return s["type"] or f"{s['id']} (not defined in components.securitySchemes)"


def cmd_app(args):
    apps = config.list_apps()
    try:
        app = _app(args)
        detected = app["name"]
    except ApiProbeError as e:
        if e.code != "APP_NOT_FOUND" or args.app:
            raise
        app = detected = None
    result = {"detected": detected, "apps": apps}
    if app and not args.no_spec:
        try:  # best effort: the API may not be running
            result["securitySchemes"] = spec.security_schemes(spec.load_spec(app, config.resolve_env(app, args.env)))
        except ApiProbeError:
            pass

    def show():
        if not apps:
            print(f"No apps configured in {config.home() / 'apps'}. Create one with 'apiprobe init NAME'.")
            return
        print(f"  {'name':<20} {'environments':<20} repoPath")
        for a in apps:
            mark = "*" if a["name"] == detected else " "
            print(f"{mark} {a['name']:<20} {', '.join(a['environments']):<20} {a['repoPath'] or '-'}")
            if a.get("error"):
                print(f"    ! {a['error']}")
        if not detected:
            print("No app detected for this folder (pass --app NAME).")
        for s in result.get("securitySchemes", {}).values():
            print(f"spec auth: {_auth_label(s)}")
    return result, 0, show


def cmd_init(args):
    path = config.init_app(args.name, args.repo or str(Path.cwd()))
    return {"path": str(path)}, 0, lambda: print(f"Wrote {path}\nSet baseUrl/specUrl, then replace each CHANGE_ME with a token for that role.")


def cmd_find(args):
    app = _app(args)
    found = spec.find(spec.load_spec(app, config.resolve_env(app, args.env)), args.text)

    def show():
        for e in found:
            print(f"{e['method']:<7} {e['path']} — {e['summary'] or e['operationId'] or ''}")
        if not found:
            print(f"No endpoints match '{args.text}'.")
    return found, 0, show


def _print_fields(fields, indent="    "):
    for f in fields:
        if "$ref" in f:
            print(f"{indent}{f['property']}  {f['$ref']}")
            continue
        extra = [x for x in ("required" if f["required"] else "", "nullable" if f.get("nullable") else "") if x]
        if f.get("enum") is not None:
            extra.append(f"enum: {json.dumps(f['enum'])}")
        print(f"{indent}{f['property']}  {f['type']}" + (f"  [{', '.join(extra)}]" if extra else ""))


def cmd_show(args):
    target = " ".join(args.target).split(None, 1)
    if len(target) != 2:
        raise ApiProbeError("ENDPOINT_NOT_FOUND", 'show expects "METHOD /path", e.g. apiprobe show "GET /api/leave/{id}"')
    _check_path(target[1], "ENDPOINT_NOT_FOUND")
    app = _app(args)
    d = spec.describe(spec.load_spec(app, config.resolve_env(app, args.env)), target[0], target[1])

    def show():
        print(f"{d['method']} {d['path']}" + (f" — {d['summary']}" if d["summary"] else ""))
        if d["parameters"]:
            print("Parameters:")
            for p in d["parameters"]:
                enum = f"  enum: {json.dumps(p['enum'])}" if p.get("enum") else ""
                print(f"    {p['name']} ({p['in']}{', required' if p['required'] else ''})  {p['type']}{enum}")
        if d["security"]:
            print("Auth: " + " | ".join(" + ".join(_auth_label(s) for s in alt) for alt in d["security"]))
        if d["requestBody"]:
            rb = d["requestBody"]
            shown = []  # preferred type first; types with identical fields print once
            for ct in sorted(rb["contentTypes"] or [rb], key=lambda c: c["contentType"] != rb["contentType"]):
                if ct["fields"] not in shown:
                    shown.append(ct["fields"])
                    print(f"Request body ({ct['contentType']}{', required' if rb['required'] else ''}):")
                    _print_fields(ct["fields"])
        print("Responses:")
        for code, r in d["responses"].items():
            print(f"  {code} {r['description'] or ''}" + (f"  ({r['contentType']})" if r.get("contentType") else ""))
            _print_fields(r.get("fields", []), "      ")
    return d, 0, show


def cmd_call(args):
    _check_path(args.path, "PLAN_INVALID")
    app = _app(args)
    sent = _body_args(args, app)
    if args.body is not None:
        try:
            raw = Path(args.body[1:]).read_text(encoding="utf-8-sig") if args.body.startswith("@") else args.body
            sent = {"body": json.loads(raw)}
        except OSError as e:
            raise ApiProbeError("PLAN_INVALID", f"--body file could not be read: {e}")
        except json.JSONDecodeError as e:
            raise ApiProbeError("PLAN_INVALID", f"--body is not valid JSON: {e.msg}")
    env = config.resolve_env(app, args.env)
    auth = config.get_user(app, env, args.as_) if args.as_ else None
    resp = http.send(env, app.get("timeoutSeconds", 30), args.method, args.path,
                     _kv(args.query, "--query"), _kv(args.header, "--header"), auth=auth, **sent)
    code = 0 if 200 <= resp["status"] < 300 else 1
    shown = resp if args.full else brief_call(resp, args.header_out or (), args.pick)

    def show():
        print(f"{args.method.upper()} {resp['url']}  →  {resp['status']}  {resp['durationMs']}ms")
        if args.pick is not None:
            picked = pick(resp["body"], args.pick)
            print(_truncate(_body_text(picked["value"]), args.max_chars) if picked["found"] else f"path '{args.pick}' not found in the response body")
        elif resp["bodyText"] or resp["binary"]:
            print(_truncate(_body_text(resp["body"]) if resp["body"] is not None else resp["bodyText"], args.max_chars))
        if runner.auth_rejected(resp) and args.as_:
            print(f"hint: {runner.token_hint(app, env, args.as_)}", file=sys.stderr)
    return shown, code, show


def _short(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return p.path + (f"?{p.query}" if p.query else "")


def _print_report(r: dict, limit: int = BODY_LIMIT):
    print(f"{r['title']}  [{r['app']} / {r['environment']}]")
    width = max((len(s["id"]) for s in r["steps"]), default=0) + 2
    for s in r["steps"]:
        if s["status"] == "skipped":
            print(f"  - {s['id']:<{width}} skipped")
            continue
        mark = "✓" if s["status"] == "passed" else "✗"
        req, resp = s["request"], s["response"]
        call = f"{req['method']} {_short(req['url'])}" if req else ""
        tail = f"{resp['status']}  {resp['durationMs']:>4}ms" if resp else ""
        print(f"  {mark} {s['id']:<{width}} {call:<40} {tail}")
        if s["status"] == "passed":
            continue
        src = f"   ({s['source']})" if s.get("source") else ""
        for a in s["assertions"]:
            if not a["passed"]:
                print(f"      {a['target']}: {a['message']}{src}")
        if s["error"]:
            print(f"      error [{s['error']['code']}]: {s['error']['message']}")
        if resp and resp["body"] not in (None, ""):
            print(f"      body: {_truncate(_body_text(resp['body']), limit)}")
    if r["cleanup"]:
        print("Cleanup")
        for c in r["cleanup"]:
            mark = {"passed": "✓", "failed": "✗", "skipped": "-"}[c["status"]]
            detail = c["responseStatus"] if c["responseStatus"] is not None else (c["error"] or {}).get("message", "")
            print(f"  {mark} {c['method']} {c['path']}  {detail}")
    if r["manualChecks"]:
        print("Manual checks (run these yourself)")
        for i, m in enumerate(r["manualChecks"], 1):
            print(f"  {i}. {m['sql']}")
            why = f"   ({m['why']})" if m.get("why") else ""
            print(f"     expect: {m.get('expect') or '-'}{why}")
            if m.get("note"):
                print(f"     note: {m['note']}")
    counts = {k: sum(s["status"] == k for s in r["steps"]) for k in ("passed", "failed", "skipped")}
    print(f"{r['status'].upper()}  {counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped"
          f"  ·  saved: {r['savedTo']}")


def cmd_run(args):
    plan = runner.load_plan(args.plan)
    app = _app(args)

    def progress(event):
        if event["type"] == "step_started":
            print(f"  … {event['id']}  {event['method']} {event['path']}", file=sys.stderr, flush=True)

    result = runner.run_plan(plan, app, args.env, None if args.json else progress, str(Path(args.plan).resolve()))
    if result["status"] == "error":
        def show():
            print(f"error [{result['error']['code']}]: {result['error']['message']}", file=sys.stderr)
            print(f"saved: {result['savedTo']}", file=sys.stderr)
        return (result if args.full else brief_run(result, args.max_chars)), 2, show
    shown = result if args.full else brief_run(result, args.max_chars)
    return shown, 0 if result["status"] == "passed" else 1, lambda: _print_report(result, args.max_chars)


def cmd_last(args):
    run = store.latest_run(_app(args)["name"], args.plan)
    if args.pick is not None and not args.step:
        raise ApiProbeError("PLAN_INVALID", "--pick needs --step ID: it picks a path from that step's response body")
    if not args.step:
        shown = run if args.full else brief_run(run, args.max_chars)
        return shown, 0, lambda: _print_report(run, args.max_chars)
    step = next((s for s in run["steps"] if s["id"] == args.step), None)
    if step is None:
        raise ApiProbeError("STEP_NOT_FOUND", f"no step '{args.step}' in the last run ({run['savedTo']}); steps: {', '.join(s['id'] for s in run['steps']) or 'none'}")
    if args.pick is None:
        return step, 0, lambda: print(json.dumps(step, indent=2, ensure_ascii=False))
    picked = {"step": step["id"], **pick((step["response"] or {}).get("body"), args.pick)}
    return picked, 0, lambda: print(_body_text(picked["value"]) if picked["found"] else f"path '{args.pick}' not found in step '{args.step}' response body")


def cmd_runs(args):
    runs = store.list_runs(_app(args)["name"], args.limit)

    def show():
        for r in runs:
            print(f"{r['startedAt']}  {str(r['status']):<7} {r['title']}  [{r['environment']}]  {r['path']}")
        if not runs:
            print("No saved runs.")
    return runs, 0, show


# ---------- entry point ----------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--app", metavar="NAME", help="app config name (default: detect from current folder)")
    common.add_argument("--env", metavar="NAME", help="environment (default: plan or config default)")
    common.add_argument("--json", action="store_true", help="print the result as JSON only")

    parser = argparse.ArgumentParser(prog="apiprobe", description="AI-assisted API testing: run approved test plans against OpenAPI apps.")
    parser.add_argument("--version", action="version", version=f"apiprobe {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("app", parents=[common], help="show detected app and all known apps")
    p.add_argument("--no-spec", action="store_true", help="skip fetching the spec for its auth schemes")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("init", parents=[common], help="write a config template for a new app")
    p.add_argument("name")
    p.add_argument("--repo", metavar="PATH", help="app repo root (default: current folder)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("find", parents=[common], help="search the OpenAPI spec")
    p.add_argument("text")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("show", parents=[common], help='describe one endpoint: show "METHOD /path"')
    p.add_argument("target", nargs="+")
    p.set_defaults(func=cmd_show)

    shaping = argparse.ArgumentParser(add_help=False)
    shaping.add_argument("--full", action="store_true", help="print the complete result (default: compact)")
    shaping.add_argument("--max-chars", type=int, default=BODY_LIMIT, metavar="N", help=f"cut response bodies at N characters (default {BODY_LIMIT})")

    p = sub.add_parser("call", parents=[common, shaping], help="send one request, no assertions")
    p.add_argument("method", type=str.upper, choices=sorted(runner.METHODS))
    p.add_argument("path")
    p.add_argument("--as", dest="as_", metavar="ROLE", help="user role from the config")
    p.add_argument("--body", metavar="JSON|@file")
    p.add_argument("--form", action="append", metavar="k=v", help="urlencoded form field (repeat a key for a list)")
    p.add_argument("--part", action="append", metavar="k=v|k=@path[;type=MIME][;filename=NAME]", help="multipart field or file")
    p.add_argument("--text", metavar="STR|@file", help="raw text body")
    p.add_argument("--file", metavar="@path", help="raw file body")
    p.add_argument("--query", action="append", metavar="k=v")
    p.add_argument("--header", action="append", metavar="k=v")
    p.add_argument("--pick", metavar="PATH", help="print only this path of the response body")
    p.add_argument("--header-out", action="append", metavar="NAME", help="also show this response header (repeatable)")
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("run", parents=[common, shaping], help="execute a plan file")
    p.add_argument("plan")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("last", parents=[common, shaping], help="show the newest saved run, or one step of it in full")
    p.add_argument("--step", metavar="ID", help="show this step in full")
    p.add_argument("--pick", metavar="PATH", help="with --step: only this path of its response body")
    p.add_argument("--plan", metavar="NAME", help="newest run of this plan (file name or slug)")
    p.set_defaults(func=cmd_last)

    p = sub.add_parser("runs", parents=[common], help="list saved runs, newest first")
    p.add_argument("--limit", type=int, default=10, metavar="N")
    p.set_defaults(func=cmd_runs)
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        result, code, show = args.func(args)
    except ApiProbeError as e:
        if args.json:
            print(json.dumps({"error": e.to_dict()}, indent=2, ensure_ascii=False))
        else:
            print(f"error [{e.code}]: {e.message}", file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        return 2
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        show()
    return code


if __name__ == "__main__":
    sys.exit(main())
