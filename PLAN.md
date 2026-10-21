# apiprobe — Build Plan

AI-assisted API testing for backend developers. An AI coding assistant (Claude Code or similar) reads the app's Swagger spec and source code, drafts a test plan, the developer approves it, a small CLI executes it deterministically, and the AI reports results and asks the developer to confirm behaviour and DB state.

This document is the single source of truth for building v1. A fresh session should be able to build the whole thing from this file alone.

---

## 1. Problem

The AI writes the endpoint, but the developer still tests it by hand: open Swagger/Postman, get a token, create prerequisite data, call the endpoint, open SSMS, check rows. apiprobe hands that loop to the AI and keeps the developer only for judgement calls ("is this what you expected?").

## 2. Decisions already made (do not re-open)

| # | Decision | Reason |
|---|---|---|
| D1 | Generic tool in its own repo at `C:\Projects\apiprobe`, usable with any app | Works for any API, not just HR Portal |
| D2 | **CLI + skill**, no MCP server | CLI works in any AI harness with a shell and by hand; MCP can wrap the core later |
| D3 | Python, **standard library only** (no third-party runtime deps) | Simple install, nothing to break |
| D4 | One JSON config file per app in `~/.apiprobe/apps/`, credentials stored as **plain JSON** | Dev/test credentials only; simplicity over secrecy |
| D5 | Base URL is configurable per environment (local or hosted dev) | Some apps tested locally, some against hosted dev |
| D6 | **No DB access in v1.** The plan carries manual SQL checks; the CLI fills in real IDs and prints them for the developer to run | Keeps v1 small, no DB drivers |
| D7 | The AI decides *what* to assert; the CLI decides *pass/fail* | LLM can't misread a result into a pass |
| D8 | Developer approves the plan before anything runs | Expectations derived from code may encode a bug |
| D9 | Saved plans live in the tested app's repo under `.apiprobe/plans/` | Shareable with team; plans hold no secrets (users referenced by role name only) |
| D10 | Core is a library that returns data and never prints or exits; CLI is a thin layer on top | A UI (future) can call the same core |
| D11 | No PII masking in v1 | Dev data. Revisit if dev DBs contain copies of real staff data |
| D12 | Repo ships an agent-facing setup guide (`skill/APP_SETUP.md`): the agent discovers config values from the app's repo, asks the user only for the rest in one message, and writes the app JSON | Onboarding a new app needs no hand-written JSON beyond optional passwords |
| D13 | **Bearer tokens only** (supersedes the username/password + `login` parts of D4, 7.2–7.4, 13, 13.1). Users are `{token}`; a `login` section or username/password is `CONFIG_INVALID`. The agent asks the user for a fresh token when one is missing, expired (JWT `exp` checked before any request → `TOKEN_EXPIRED`) or rejected (401 → hint naming the config field) | Asking for credentials risks misuse; tokens are short-lived |

## 3. Non-goals for v1

Do not build these. They are listed so nobody adds them "for later".

- DB connections or queries of any kind
- MCP server
- UI / HTTP server
- Keyring / encrypted credentials
- Token caching across runs
- PII masking
- Swagger 2.0 (only OpenAPI 3.x, which Swashbuckle and most modern frameworks emit)
- Assertion operators beyond: equality, `exists`, `contains`
- Parallel steps, retries, loops, conditionals in plans
- Generating xUnit/integration tests
- Response diffing between runs

## 4. Architecture

```
          ┌──────────────────────────────┐
          │  SKILL (SKILL.md)            │  workflow + rules for the AI
          └──────────────┬───────────────┘
                         │ runs shell commands
          ┌──────────────▼───────────────┐      ┌──────────────────┐
          │  CLI  (apiprobe/cli.py)      │      │ future: UI / API │
          │  argparse, printing, exit    │      │ server, MCP      │
          └──────────────┬───────────────┘      └────────┬─────────┘
                         │ plain function calls           │
          ┌──────────────▼────────────────────────────────▼─────────┐
          │  CORE (no print, no sys.exit, returns dicts, raises     │
          │        ApiProbeError)                                    │
          │  config.py  spec.py  http.py  runner.py  store.py        │
          └──────────────────────────────────────────────────────────┘
                         │
          ~/.apiprobe/apps/*.json   (configs)
          ~/.apiprobe/runs/<app>/   (run results, JSON)
          <app repo>/.apiprobe/plans/*.json  (plans)
```

### Rules that keep the core UI-ready

1. Core functions never `print`, never call `sys.exit`, never read `sys.argv` or `input()`.
2. Every core result is a JSON-serialisable `dict`/`list` (no custom classes in return values). Schemas are in section 8.
3. Errors are raised as `ApiProbeError(code, message)`; `code` is a short stable string (section 9).
4. `run_plan` accepts an optional `on_event(event: dict)` callback so a UI can show live progress. The CLI uses it to print progress lines.
5. Every run result is saved to disk (`store.py`) so a UI can list run history later.

## 5. Repo layout

```
C:\Projects\apiprobe\
  PLAN.md                  this file
  README.md                install + usage (written in step 10)
  pyproject.toml           package metadata, console script `apiprobe`
  .gitignore               __pycache__/, *.egg-info/, .venv/
  apiprobe/
    __init__.py            __version__ = "0.1.0"
    errors.py              ApiProbeError
    config.py              app config load/list/detect/init
    spec.py                OpenAPI load, find, describe
    http.py                single HTTP call + login
    vars.py                {{var}} substitution + dotted path lookup
    runner.py              run_plan + assertions
    store.py               save/list run results
    cli.py                 argparse entry point
  skill/
    SKILL.md               the Claude Code skill (section 13)
    APP_SETUP.md           agent guide to create/update an app config (section 13.1)
  tests/
    stub_server.py         tiny local HTTP server used by tests
    test_vars.py
    test_assertions.py
    test_config.py
    test_spec.py
    test_runner.py
    fixtures/
      openapi.json         small OpenAPI 3 spec for tests
```

Keep each module small. If a module passes ~250 lines, check for over-engineering before splitting.

## 6. Environment

- Python 3.14 is installed on the dev machine (minimum supported: 3.10, for `match` and `X | Y` types).
- Install with uv: `uv tool install --editable .` from the repo root (isolated env, `apiprobe.exe` in `~/.local/bin`, which is on PATH). Dev/tests: `uv run python -m unittest discover -s tests -v` (`.venv` from `uv.lock`). The earlier `pip install -e .` put the exe in Python's `Scripts` folder, which was not on PATH.
- Tests use `unittest` (stdlib): `python -m unittest discover -s tests -v`.

## 7. App config

### 7.1 Location

`~/.apiprobe/apps/<app-name>.json` — file name (without `.json`) is the app name. `~` resolves via `Path.home()`. Override root with env var `APIPROBE_HOME` (needed so tests don't touch the real home folder).

### 7.2 Schema

```json
{
  "repoPath": "C:/Projects/Dhamecha/HR Portal/HR-Portal",
  "specUrl": "/swagger/v1/swagger.json",
  "defaultEnvironment": "local",
  "timeoutSeconds": 30,
  "login": {
    "method": "POST",
    "path": "/api/auth/login",
    "body": { "username": "{{username}}", "password": "{{password}}" },
    "tokenPath": "data.token"
  },
  "environments": {
    "local": {
      "baseUrl": "https://localhost:5001",
      "verifyTls": false,
      "users": {
        "admin":   { "username": "admin@dev.local", "password": "CHANGE_ME" },
        "manager": { "username": "mgr@dev.local",   "password": "CHANGE_ME" },
        "tokenOnly": { "token": "eyJ..." }
      }
    },
    "dev": {
      "baseUrl": "https://dev-hrportal.example.com",
      "users": { "admin": { "username": "...", "password": "..." } }
    }
  }
}
```

### 7.3 Field rules

| Field | Required | Rule |
|---|---|---|
| `repoPath` | no | Absolute path. Used for app detection. Compare normalised (resolve, case-insensitive on Windows, `/` vs `\` irrelevant). |
| `specUrl` | yes | Starts with `/` → joined to the environment's `baseUrl`. Starts with `http` → used as is. Otherwise → local file path. |
| `defaultEnvironment` | yes | Must be a key of `environments`. |
| `timeoutSeconds` | no | Default 30. |
| `login` | no | Omit if the app uses static tokens only or no auth. |
| `login.body` | yes if login | Any JSON; `{{username}}`/`{{password}}` are substituted from the user entry. |
| `login.tokenPath` | yes if login | Dotted path (section 7.5) into the login response JSON. |
| `environments.<env>.baseUrl` | yes | `http://` or `https://`, no trailing slash needed (strip it). |
| `environments.<env>.verifyTls` | no | Default `true`. `false` for local self-signed dev certs. |
| `environments.<env>.users.<role>` | — | Either `{username, password}` or `{token}`. |

### 7.4 Validation (on load, raise `CONFIG_INVALID` with a message naming the file and field)

- File is valid JSON.
- Required fields present, `defaultEnvironment` exists in `environments`.
- Each user has either `token` or both `username` + `password`.
- A user whose `password` is `CHANGE_ME` or whose `token` is empty → `USER_NOT_CONFIGURED` **only when that user is actually used** (so unused placeholders don't block other roles).

### 7.5 Dotted path syntax (shared by `tokenPath`, `extract`, `expect.json`)

- `data.token`, `data.items[0].id`, `items[2]`, `[0].id` (root array).
- Empty string `""` means the whole body.
- Keys are case-sensitive.
- Missing key / index out of range → "not found" (distinct from a `null` value).

### 7.6 App detection

1. `--app <name>` flag wins.
2. Otherwise, find configs whose `repoPath` equals the current directory or is a parent of it. Pick the longest (most specific) match.
3. None → `APP_NOT_FOUND` with the list of known apps.

### 7.7 Security notes (documented in README)

- Configs hold plaintext credentials. They live in the home folder, never in a repo.
- Only put dev/test environments in configs. **Never prod.** Plans contain only paths, never full URLs, so a plan can only reach hosts that are in the config.

## 8. Plan file

### 8.1 Location

`<app repo>/.apiprobe/plans/<slug>.json`. The CLI accepts any path.

### 8.2 Schema

```json
{
  "title": "Reporting manager approves leave",
  "environment": "local",
  "steps": [
    {
      "id": "create_leave",
      "call": {
        "method": "POST",
        "path": "/api/leave",
        "as": "staff",
        "query": { "notify": "false" },
        "headers": { "X-Correlation-Id": "{{$uuid}}" },
        "body": { "from": "2026-11-01", "to": "2026-11-02", "type": "Annual" }
      },
      "extract": { "leaveId": "data.id" },
      "expect": {
        "status": 201,
        "json": {
          "data.status": "Pending",
          "data.id": { "exists": true },
          "data.warnings": { "contains": "overlap" }
        }
      },
      "source": "CreateLeaveHandler.cs:58"
    }
  ],
  "manualChecks": [
    {
      "sql": "SELECT Status FROM LeaveRequests WHERE Id = '{{leaveId}}'",
      "expect": "Status = Approved",
      "why": "Approval must be persisted"
    }
  ],
  "cleanup": [
    { "method": "DELETE", "path": "/api/leave/{{leaveId}}", "as": "admin" }
  ]
}
```

### 8.3 Field rules

| Field | Required | Rule |
|---|---|---|
| `title` | yes | Free text. |
| `environment` | no | Overrides config `defaultEnvironment`. CLI `--env` overrides both. |
| `steps[].id` | yes | Unique within the plan, `[a-z0-9_]+`. |
| `call.method` | yes | GET/POST/PUT/PATCH/DELETE (case-insensitive, normalised upper). |
| `call.path` | yes | Must start with `/`. Absolute URLs rejected (`PLAN_INVALID`). |
| `call.as` | no | User role from config. Omitted → no `Authorization` header. |
| `call.query` | no | Object; values stringified; URL-encoded. |
| `call.headers` | no | Object. |
| `call.body` | no | Any JSON → sent with `Content-Type: application/json`. |
| `extract` | no | `{varName: dottedPath}`. Not found → step fails. |
| `expect.status` | no | Integer. If `expect` or `status` omitted, any 2xx passes. |
| `expect.json` | no | `{dottedPath: expectation}` (section 8.5). |
| `source` | no | Where the expectation came from (`File.cs:line`). Shown in report. |
| `manualChecks[]` | no | `sql`, `expect`, `why`. Variables substituted; printed in report; never executed. |
| `cleanup[]` | no | Same shape as `call`. |

### 8.4 Variables

- Syntax `{{name}}`. Substituted in `path`, `query` values, `headers` values, `body` (recursively, all strings), `cleanup` calls, `manualChecks[].sql`.
- If a string is **exactly** `{{name}}`, the raw value replaces it, preserving type (number stays a number). Otherwise the value is stringified into the string.
- Built-ins: `{{$uuid}}` (new uuid4 per occurrence), `{{$now}}` (UTC ISO-8601, e.g. `2026-10-21T10:15:00Z`). Use these for unique test data on shared dev DBs.
- Unknown variable → that step fails with `VAR_UNDEFINED` before the request is sent.
- Variables are set by `extract` and live for the rest of the run.

### 8.5 Assertions (`expect.json` values)

| Expectation form | Passes when |
|---|---|
| any plain JSON value (`"Pending"`, `5`, `null`, `[1,2]`, `{"a":1}`) | path found and value deep-equals it |
| `{"exists": true}` | path found (value may be `null`) |
| `{"exists": false}` | path not found |
| `{"contains": x}` | value is a string containing `x`, or a list with an element deep-equal to `x` |

An expectation object whose only key is `exists` or `contains` is an operator. Any other object is a plain value. Response body not JSON while `expect.json` present → each json assertion fails with "response is not JSON".

### 8.6 Execution rules

1. Load config, resolve environment, validate whole plan **before** sending anything (`PLAN_INVALID` lists every problem).
2. Steps run in order. Login per user happens lazily on first use; token kept in memory for the rest of the run. Header: `Authorization: Bearer <token>`.
3. A step **fails** if: request error (connection refused, timeout, TLS), status mismatch, any json assertion fails, extract path not found, or variable undefined.
4. After the first failing step, remaining steps are marked `skipped`.
5. `cleanup` always runs after steps (even after failure). Cleanup calls whose variables are undefined are `skipped`. Cleanup failures are reported but don't change the run's pass/fail.
6. Login failure → the step that needed it fails with `LOGIN_FAILED` and the login response status/body snippet.
7. Run status: `passed` (all steps passed), `failed` (an assertion/step failed), `error` (could not start: bad config/plan).

## 9. Error codes (`ApiProbeError.code`)

| Code | Meaning |
|---|---|
| `CONFIG_INVALID` | App config file malformed or missing fields |
| `APP_NOT_FOUND` | No config matches `--app` or the current folder |
| `APP_EXISTS` | `init` target config already exists |
| `ENV_NOT_FOUND` | Environment name not in config |
| `USER_NOT_FOUND` | Role in plan/call not in config |
| `USER_NOT_CONFIGURED` | User still has `CHANGE_ME` / empty token |
| `TOKEN_EXPIRED` | Role's JWT is past its `exp` (D13). `LOGIN_FAILED` was removed with login support |
| `SPEC_UNAVAILABLE` | Could not fetch/parse the OpenAPI spec |
| `ENDPOINT_NOT_FOUND` | `show` target not in spec |
| `PLAN_INVALID` | Plan JSON malformed or violates rules |
| `VAR_UNDEFINED` | `{{name}}` used before being set |
| `REQUEST_FAILED` | Connection/timeout/TLS error |

Every message must say **what** is wrong and **where to fix it** (file path + field), because the AI reads it and relays it.

## 10. Core API (signatures)

```python
# errors.py
class ApiProbeError(Exception):
    def __init__(self, code: str, message: str): ...

# config.py
def home() -> Path                                   # APIPROBE_HOME or ~/.apiprobe
def list_apps() -> list[dict]                        # [{name, repoPath, environments: [..]}]
def load_app(name: str) -> dict                      # validated config + "name"
def detect_app(cwd: Path) -> dict                    # per 7.6
def init_app(name: str, repo_path: str | None) -> Path   # writes template (7.2) with CHANGE_ME; refuses to overwrite
def resolve_env(app: dict, env: str | None) -> dict  # {name, baseUrl, verifyTls, users}

# vars.py
def lookup(data, path: str) -> tuple[bool, object]  # (found, value)
def substitute(value, variables: dict)               # recursive; raises VAR_UNDEFINED

# http.py
def send(env: dict, timeout: int, method, path, query=None, headers=None, body=None, token=None) -> dict
    # -> {"status", "headers", "body", "bodyText", "durationMs"}; body = parsed JSON or None
def login(app: dict, env: dict, role: str) -> str     # returns token

# spec.py
def load_spec(app: dict, env: dict) -> dict
def find(spec: dict, text: str) -> list[dict]        # [{method, path, summary, operationId, tags}]
def describe(spec: dict, method: str, path: str) -> dict   # params, requestBody schema, responses (refs resolved)

# runner.py
def run_plan(plan: dict, app: dict, env_name: str | None = None, on_event=None) -> dict   # RunResult 10.1
def check(expectation, found: bool, value) -> tuple[bool, str]   # assertion, returns (passed, message)

# store.py
def save_run(app_name: str, result: dict) -> Path    # runs/<app>/<UTC yyyyMMdd-HHmmss>-<plan-slug>.json
def list_runs(app_name: str) -> list[dict]           # future UI; small, include now
```

### 10.1 RunResult schema (also the `--json` output of `run`)

```json
{
  "app": "hr-portal",
  "environment": "local",
  "title": "Reporting manager approves leave",
  "planPath": "C:/.../.apiprobe/plans/approve-leave.json",
  "startedAt": "2026-10-21T10:15:00Z",
  "durationMs": 812,
  "status": "passed | failed | error",
  "steps": [
    {
      "id": "create_leave",
      "status": "passed | failed | skipped",
      "request": { "method": "POST", "url": "https://localhost:5001/api/leave", "body": {} },
      "response": { "status": 201, "durationMs": 120, "body": {} },
      "assertions": [
        { "target": "status", "expected": 201, "actual": 201, "passed": true, "message": "" },
        { "target": "data.status", "expected": "Pending", "actual": "Pending", "passed": true, "message": "" }
      ],
      "extracted": { "leaveId": "3f2a..." },
      "error": null,
      "source": "CreateLeaveHandler.cs:58"
    }
  ],
  "cleanup": [ { "method": "DELETE", "path": "/api/leave/3f2a...", "status": "passed | failed | skipped", "responseStatus": 204 } ],
  "manualChecks": [ { "sql": "SELECT ... '3f2a...'", "expect": "Status = Approved", "why": "..." } ],
  "variables": { "leaveId": "3f2a..." },
  "error": null,
  "savedTo": "C:/Users/.../.apiprobe/runs/hr-portal/20261021-101500-approve-leave.json"
}
```

- Request `headers` are **not** stored (they contain the token).
- Saved file holds full response bodies. Console (non-`--json`) output truncates bodies to 1500 chars.

### 10.2 Events passed to `on_event`

```json
{"type": "step_started",  "id": "create_leave", "method": "POST", "path": "/api/leave"}
{"type": "step_finished", "id": "create_leave", "status": "passed", "durationMs": 120}
{"type": "cleanup_finished", "path": "...", "status": "passed"}
```

## 11. CLI

Entry point `apiprobe` → `apiprobe.cli:main`. Global flags on every command: `--app NAME`, `--env NAME`, `--json`.

| Command | Does | Human output | Exit |
|---|---|---|---|
| `apiprobe app` | Show detected app and all known apps | Table: name, repoPath, environments, `*` on detected | 0 |
| `apiprobe init NAME [--repo PATH]` | Write config template (`--repo` defaults to cwd) | Path written + "fill in CHANGE_ME values" | 0 / 2 if exists |
| `apiprobe find TEXT` | Search spec | `METHOD path — summary` per line | 0 (also 0 with no results) |
| `apiprobe show "METHOD /path"` | Describe one endpoint | Params, body fields (name, type, required, enum), response codes + shapes | 0 / 2 |
| `apiprobe call METHOD PATH [--as ROLE] [--body JSON or @file] [--query k=v ...] [--header k=v ...]` | One request, no assertions | Status, duration, body (truncated) | 0 if 2xx, 1 otherwise, 2 on error |
| `apiprobe run PLAN.json` | Execute plan | Progress lines, then report (11.1) | 0 passed / 1 failed / 2 error |

- `--json` prints the core result dict as JSON and nothing else (errors as `{"error": {"code", "message"}}`).
- `ApiProbeError` → stderr `error [CODE]: message`, exit 2. Unexpected exceptions → full traceback, exit 2.
- Set stdout to UTF-8 on Windows (`sys.stdout.reconfigure(encoding="utf-8")`) because the report uses ✓/✗.

### 11.1 `run` human report format

```
Reporting manager approves leave  [hr-portal / local]
  ✓ create_leave   POST /api/leave                       201  120ms
  ✗ approve        POST /api/leave/3f2a.../approve       403   45ms
      status: expected 200, got 403   (ApproveLeaveHandler.cs:42)
      body: {"message":"User has no access to depot"}
  - notify         skipped
Cleanup
  ✓ DELETE /api/leave/3f2a...  204
Manual checks (run these yourself)
  1. SELECT Status FROM LeaveRequests WHERE Id = '3f2a...'
     expect: Status = Approved   (Approval must be persisted)
FAILED  1 passed, 1 failed, 1 skipped  ·  saved: C:\Users\...\runs\hr-portal\20261021-101500-approve-leave.json
```

## 12. `spec.describe` details

- OpenAPI 3.x only. If `swagger: "2.0"` → `SPEC_UNAVAILABLE` "Swagger 2.0 not supported".
- Match path template exactly (`/api/leave/{id}/approve`). Also accept a concrete path (`/api/leave/123/approve`) by matching segments against templates.
- Resolve `$ref` (`#/components/schemas/X`) recursively. Guard against cycles: track visited refs; on cycle output `{"$ref": "X (recursive)"}`. Max depth 6.
- Output fields: `parameters` (name, in, required, type), `requestBody` (content type → flattened schema: property, type, required, enum, nullable), `responses` (code → description + flattened schema).
- `find`: case-insensitive; split text on spaces; every word must appear in `method + path + summary + operationId + tags` joined.

## 13. Skill (`skill/SKILL.md`)

Installed by copying the whole `skill/` folder to `~/.claude/skills/apiprobe/` (SKILL.md + APP_SETUP.md). Full content to write:

```markdown
---
name: apiprobe
description: Test an API endpoint end-to-end using the apiprobe CLI. Use when the user asks to test, try, hit, call or verify an endpoint/API, reproduce an API bug, or check an API flow. Reads Swagger and source code, drafts a plan with expectations, gets approval, runs it, reports, and asks the user to confirm DB state.
---

# apiprobe — AI-assisted API testing

You drive the `apiprobe` CLI. The CLI decides pass/fail. You decide what to test and what to expect, and the user approves.

## Hard rules
1. Never report a pass unless `apiprobe run` exited 0. Quote the CLI result.
2. Never change an expectation after a run to make it pass without asking the user.
3. Every expectation has a `source` (File:line). If you could not find one, label it "assumption" in the plan table.
4. Never run a plan before the user approves it.
5. Passwords/tokens: by default write `CHANGE_ME` and tell the user which config file and field to fill. Write a real value only if the user chooses to give it (dev/test accounts only).
6. Never add a production URL to a config. If the user asks, refuse and explain.
7. Write endpoints (POST/PUT/PATCH/DELETE) are not "done" without manualChecks or an explicit note that DB state was not checked.
8. Always use `--json` when you need to read output; show the user the human summary.

## Workflow

### 1. Identify the app
- Run `apiprobe app --json`.
- Detected → confirm in one line: "Testing <app> on <env>."
- Not detected → list known apps and ask which one; if it is a new app, follow `APP_SETUP.md` (in this skill's folder) to create the config, then continue.
- Config incomplete (USER_NOT_CONFIGURED, CONFIG_INVALID), or the user wants to add an environment/user → follow `APP_SETUP.md`.

### 2. Understand the target
- `apiprobe find "<words>" --json`, then `apiprobe show "<METHOD /path>" --json`.
- Read the controller action, the request handler, any validator, and the entities it writes.
- More than one plausible endpoint, or unclear intent → stop and ask with the candidates listed.

### 3. Prerequisites
- Identify every ID/record the request needs. Find the endpoints that create them (search spec + code).
- Ask: "Use existing records (give IDs) or create new ones?"
- For shared dev environments, use `{{$uuid}}` for unique names/emails and add cleanup calls for what you create.

### 4. Draft the plan and get approval
- Write `<repo>/.apiprobe/plans/<slug>.json` following the plan format below.
- Show the user a table: step | call | as | expectations | source.
- List separately: calls with side effects outside the DB (emails, payroll, jobs), deletes, and assumptions.
- Include negative cases when relevant: validation (missing/partial fields), access (user without rights → 401/403/empty), not-found.
- Ask: "Run this plan?" Wait.

### 5. Run
- `apiprobe run <plan> --json`.
- Exit 2 with USER_NOT_CONFIGURED / CONFIG_INVALID → tell user exactly which file/field to fix; wait.
- Exit 2 with REQUEST_FAILED → ask the user to start the API (do not start it yourself).

### 6. Report
Group findings:
- ✅ Verified — passed assertions (by the CLI).
- ❌ Failed — expected vs actual, response snippet, your diagnosis from the code (file:line). Do not fix code unless asked.
- ⚠️ Inferred — behaviour you believe from code but did not assert.
- ❓ Needs you — print each manual check SQL (already has real IDs) and ask "Do you see <expect>?"

### 7. Close
- Ask: "Is this the behaviour you expected?"
- Yes → keep the plan file (offer to rename it). No → discuss, edit the plan, rerun.
- If the user does not want to keep the plan, delete it.

## Plan format (reference)
<copy section 8.2 example and 8.5 assertion table here>

## Quick command reference
<copy section 11 table here>
```

The final SKILL.md must inline the plan example and command table (not "see PLAN.md"), because the skill runs in other repos where PLAN.md does not exist.

### 13.1 Setup guide (`skill/APP_SETUP.md`)

Agent-facing guide for creating or updating an app config. The agent discovers what it can from the app's repo, asks the user only for the rest, writes the JSON, and verifies it. Full content to write:

```markdown
# apiprobe — App setup guide (for the agent)

Use this when `apiprobe app` finds no config for the current repo, when a config is incomplete (USER_NOT_CONFIGURED, CONFIG_INVALID), or when the user wants to add an environment or user. Goal: a valid `~/.apiprobe/apps/<name>.json` with as few questions as possible.

## Rules
- Discover first, ask second. Ask only for what the repo cannot tell you.
- Ask all remaining questions in ONE message, numbered, each with your discovered or suggested answer, so the user can reply "ok" or correct it.
- Never add a production URL. If a URL may be prod (no local/dev/test/qa/staging marker, or the user says prod), stop and ask.
- Passwords/tokens: by default write `CHANGE_ME` and tell the user the file and field. Write a real value only if the user chooses to give it.
- Never overwrite an existing config silently. When editing, show the change (secrets as `***`) and get a yes.

## Step 1 — Discover from the repo
Record each discovered value with its source (file:line).

| Field | Where to look |
| --- | --- |
| app name | repo folder name → kebab-case |
| repoPath | repo root (folder containing `.git`) |
| local baseUrl | .NET: `Properties/launchSettings.json` `applicationUrl` (prefer the https profile); Node: `.env` / `package.json` scripts / PORT; Python: uvicorn/flask run args; Spring: `application.yml` `server.port`; docker-compose port mappings |
| specUrl | .NET Swashbuckle/NSwag: `AddSwaggerGen` / `UseSwagger` route (default `/swagger/v1/swagger.json`); FastAPI: `/openapi.json`; springdoc: `/v3/api-docs`; NestJS: `SwaggerModule.setup` path + `-json` |
| verifyTls | https on localhost → `false` |
| auth type | JWT issued by the app itself → `login`; external identity provider (Azure AD, Auth0, IdentityServer authority in config) → static `token` users; no `[Authorize]`/auth middleware → none |
| login | controller/route named login/token/auth/signin; request DTO fields → `login.body`; response type → `tokenPath` (e.g. `data.token`, `accessToken`) |
| roles | role names in `[Authorize(Roles=...)]`, policies, role enums, seed data; data scoping (depot, tenant, branch) → suggest one user per scope for access tests |

## Step 2 — Ask the user (one message)
Template:
1. App name: `<suggested>` — ok?
2. Environments: local `<discovered URL>` (from `<file:line>`) — ok? Any hosted dev/test environment? Its URL?
3. Auth: `<POST /api/auth/login, token at data.token>` (from `<file:line>`) — correct? Or paste-a-token / no auth?
4. Test users per environment: suggested roles `<admin, manager, staff-depotA>` — username for each?
5. Passwords/tokens: I'll put `CHANGE_ME` for you to fill in the file, or you can give dev passwords now — which?
6. Timeout 30 seconds — ok?

## Step 3 — Write the config
- New app: run `apiprobe init <name> --repo <repoPath>`, then edit the created file with the answers.
- Existing app: edit in place, after showing the change.
- The file must match the config schema below.

## Step 4 — Verify
1. `apiprobe app --json` → the app is detected from the repo.
2. `apiprobe find <a word from a known route> --json` → spec reachable. On SPEC_UNAVAILABLE, ask the user to start the API or recheck `specUrl`.
3. If credentials are filled: `apiprobe call GET <a simple authenticated GET endpoint> --as <role>` → 2xx. On LOGIN_FAILED, show the login response and recheck `login.path`, `login.body`, `tokenPath`.
4. If `CHANGE_ME` remains: list each file + field the user must fill, and stop until they confirm.

Report: what was discovered (with sources), what the user provided, what is still missing.

## Config schema (reference)
<copy section 7.2 example and 7.3 field table here>
```

Like SKILL.md, APP_SETUP.md must inline the config schema and field table, because it is used from other repos.

## 14. Implementation steps (in order)

Each step ends with a check. Do not start the next step until the check passes.

1. **Scaffold repo**
   - `git init`, create layout from section 5, `pyproject.toml` (setuptools, `requires-python = ">=3.10"`, `[project.scripts] apiprobe = "apiprobe.cli:main"`, no dependencies), `.gitignore`, `apiprobe/__init__.py`, `errors.py`.
   - Check: `uv tool install --editable .` succeeds; `apiprobe --help` prints usage.

2. **`vars.py`** — `lookup`, `substitute` (incl. `$uuid`, `$now`, exact-match type preservation).
   - Tests (`test_vars.py`): nested path, array index, root array, missing vs null, out of range, exact-match keeps int, embedded stringifies, undefined raises `VAR_UNDEFINED`, `$uuid` differs per occurrence.

3. **Assertions** — `runner.check`.
   - Tests (`test_assertions.py`): every row of table 8.5 pass and fail; deep-equal object/list; `contains` on string, list, and on non-string/non-list (fails with message); object with extra keys is treated as plain value.

4. **`config.py`** — home (with `APIPROBE_HOME`), list, load+validate, detect (longest parent match, Windows-normalised), init template, resolve_env.
   - Tests (`test_config.py`, temp `APIPROBE_HOME`): valid load; each validation error names file+field; detection from subfolder; two nested repoPaths pick longest; `--app` wins; init refuses overwrite; CHANGE_ME only errors when that user is used.

5. **`http.py`** — `send` with `urllib.request` (timeout, query encoding, JSON body, `verifyTls=False` via unverified SSL context, return non-2xx as data not exception, catch `URLError`/timeout → `REQUEST_FAILED`), `login`.
   - Tests use `tests/stub_server.py`: a `http.server.ThreadingHTTPServer` on port 0 in a thread, with routes for login, echo, create (returns id), protected (checks Bearer), slow (for timeout).
   - Check: 2xx and 4xx both return dicts; connection refused → `REQUEST_FAILED`; login extracts token; bad tokenPath → `LOGIN_FAILED`.

6. **`runner.py` + `store.py`** — `run_plan` per section 8.6, events, RunResult per 10.1, save to runs folder.
   - Tests (`test_runner.py` with stub server): pass flow with extract→reuse; failing status marks later steps skipped; cleanup runs after failure; cleanup with undefined var skipped; manualChecks substituted; invalid plan (absolute URL, duplicate id, unknown role) → `PLAN_INVALID` listing all issues before any request (assert stub received zero requests); token not present anywhere in saved JSON; events emitted in order.

7. **`spec.py`** — load (path/URL/file), Swagger 2 rejection, find, describe with `$ref` resolution, cycle guard, concrete-path matching.
   - Tests (`test_spec.py`) against `tests/fixtures/openapi.json` (include a recursive schema, an enum, a path param).

8. **`cli.py`** — all commands of section 11, `--json`, exit codes, UTF-8 stdout, human report 11.1.
   - Check manually against the stub server or a real app: each command once with and without `--json`; exit codes match the table.

9. **Skill** — write `skill/SKILL.md` (section 13) and `skill/APP_SETUP.md` (section 13.1), both with inlined reference sections. Copy the `skill/` folder to `~/.claude/skills/apiprobe/`.
   - Check: in a new Claude Code session inside another repo, the skill is listed and triggers on "test the X endpoint".
   - Check: in a repo with no config, the agent follows APP_SETUP.md, asks one batch of questions, and writes a config that `apiprobe app` detects.

10. **README.md** — what it is, install (`uv tool install --editable .`, copy skill), config example, plan example, security notes (7.7), command table.

11. **End-to-end on HR Portal** (acceptance, section 15).

12. **Commit** — one commit per step above is fine; first commit on `main`.

## 15. Acceptance criteria

v1 is done when all of these hold:

- [ ] `python -m unittest discover -s tests -v` passes, with no third-party packages installed.
- [ ] `apiprobe init hr-portal` from the HR Portal repo writes a template; after filling it, `apiprobe app` detects `hr-portal` from any subfolder of that repo.
- [ ] `apiprobe find leave` and `apiprobe show "<a real endpoint>"` return correct info from HR Portal's real Swagger.
- [ ] `apiprobe call GET <some endpoint> --as <role>` works with real login.
- [ ] Via the skill, "test <a real HR Portal endpoint>" produces a plan with sources, waits for approval, runs, reports with ✅/❌/❓ and prints manual SQL with real IDs.
- [ ] A deliberately wrong expectation produces exit 1 and the AI reports it as failed (not passed).
- [ ] A plan using a role with `CHANGE_ME` stops with a message naming the config file and role.
- [ ] Saved run JSON contains no token or password.
- [ ] The same tool works for a second, unrelated app with only a new config file.
- [ ] In a repo with no config, the agent discovers values from the repo, asks the user only the remaining questions in one message, writes the config, and verifies it with `apiprobe app` and `apiprobe find`.

## 16. Things to confirm while building (not blockers)

- HR Portal's login endpoint path, request body and token location in the response — read the auth controller and `JwtConfigurationExtensions.cs` in HR Portal when doing step 11. If tokens come from an external identity provider rather than an app endpoint, use `{token}` users for v1 and note it.
- Whether HR Portal's local URL is http or https and the port (`Properties/launchSettings.json`).
- Swagger JSON path (`SwaggerInstaller.cs`; default `/swagger/v1/swagger.json`).

## 17. Future scope (do not build now)

Ordered by likely value:

1. **Read-only DB checks** — `db` section in env config (connection string, read-only login), `dbChecks` in plans asserted by the CLI. Start with SQL Server via `pyodbc` (first third-party dependency; make it optional extra).
2. **UI** — a small local web server (`apiprobe serve`) exposing the core functions as HTTP endpoints: list apps, list/run plans with live progress (via `on_event` → server-sent events), view run history (`store.list_runs`). No core changes needed if section 4 rules were followed.
3. **Replay and diff** — rerun saved plans, compare responses with the previous run.
4. **Generate integration tests** from a passed plan (e.g. xUnit + WebApplicationFactory for .NET apps).
5. **MCP server** wrapping the core, for AI clients without a shell.
6. **PII masking** (`maskFields` in config) if dev DBs contain real personal data.
7. **Spec diff** between branches to flag breaking changes.
8. **More auth types** (OAuth client credentials, custom header names).

## 18. Risks

- **Expectations derived from buggy code.** Mitigated by plan approval (D8) and `source` citations; still the main risk.
- **Shared dev DB pollution.** Mitigated by `{{$uuid}}` and `cleanup`; cleanup depends on the app having delete endpoints.
- **Real personal data in dev DBs** reaches the AI provider via responses (D11). Revisit masking if so.
- **Plaintext credentials** in `~/.apiprobe`. Acceptable only for dev/test accounts.
