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
5. Auth is token-only. Never ask for, accept or store usernames or passwords; if the user offers one, say a token is enough. For Basic auth the user pastes the pre-encoded value into the config file themselves. When a role has no token, or you get USER_NOT_CONFIGURED, TOKEN_EXPIRED or a 401 (or empty 403) with a "token ... may be expired" hint, ask the user to generate a fresh token for that role and either paste it here (you write it into the config field the error names) or paste it into that field themselves (keeps it out of the chat). Never write a token anywhere else: not in plans, the repo, or your replies.
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
- Endpoint in code but missing/different in spec (ENDPOINT_NOT_FOUND, old fields) → the running API is stale; ask the user to rebuild/restart it, then re-run `find`/`show`.
- `show` lists each accepted request content type with its fields (`file` = upload). Pick the plan body key: `application/json` → `body`, `application/x-www-form-urlencoded` → `form`, `multipart/form-data` → `multipart`, `text/*`/XML → `text`, `application/octet-stream` → `file`.
- `show` prints `Auth:` for the endpoint. The user's config entry must match it: bearer → `{"token"}`; basic → add `"scheme": "Basic"`; apiKey → add `"header"`, `"query"` or `"cookie"` with the scheme's name. No `Auth:` line → find the type in the code (`APP_SETUP.md` lists what to grep); unsure → ask.
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
- Include negative cases when relevant: validation (missing/partial fields), access (user without rights → 401/403/empty), not-found, wrong/missing API key (401/403) when the endpoint uses one; for uploads also wrong content type (415), missing file part, oversized file (413), disallowed extension.
- Ask: "Run this plan?" Wait.

### 5. Run
- `apiprobe run <plan> --json`.
- Exit 2 with USER_NOT_CONFIGURED / TOKEN_EXPIRED → ask for a fresh token for that role (rule 5); rerun once it is in.
- Exit 2 with CONFIG_INVALID → tell user exactly which file/field to fix; wait.
- A step failed with 401 and a token hint → the token was rejected (expired early, revoked, wrong environment). Ask for a fresh one; do not report it as an app bug.
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

Field rules:
- `title` required. `environment` optional (overrides config default; `--env` overrides both).
- `steps[].id` required, unique, `[a-z0-9_]+`.
- `call.method` GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS. `call.path` must start with `/` — absolute URLs are rejected. `call.as` is a role from the config (omit → no auth header). `query`, `headers` are objects.
- Body: at most one of `body` (any JSON), `form` (flat object; list value repeats the key), `multipart`, `text` (string; `headers` sets the type), `file` (path; raw bytes).
- `multipart`: field → string, `{"json": X}`, `{"file": "files/a.pdf", "contentType"?, "filename"?}`, or a list of these (repeats the field). Example: `"multipart": {"title": "T {{$uuid}}", "doc": {"file": "files/c.pdf"}}`.
- File paths are relative to the plan's folder. Keep fixtures in `<repo>/.apiprobe/files/`; ask before creating large files or using real documents. Paths outside the repo (or plan folder) fail validation, nothing is sent. Runs record file name, size and type, never bytes.
- `extract`: `{varName: dottedPath}`; path not found → step fails.
- `expect.status`: integer; omitted → any 2xx passes. `expect.json`: `{dottedPath: expectation}`.
- `source`: where the expectation came from (`File.cs:line`).
- `manualChecks[]`: `sql`, `expect`, `why`. Variables are filled in; never executed.
- `cleanup[]`: same shape as `call`. Always runs, even after failure; calls with undefined variables are skipped.

Variables: `{{name}}` in `path`, `query`/`headers` values, `body`/`form`/`multipart`/`text` values, file paths, `cleanup`, `manualChecks[].sql` (not in `expect`). A string that is exactly `{{name}}` keeps the value's type. Built-ins: `{{$uuid}}` (new per occurrence), `{{$now}}` (UTC ISO-8601). Variables are set by `extract`. Unknown variable → step fails before sending.

Dotted paths: `data.token`, `data.items[0].id`, `items[2]`, `[0].id` (root array), `""` (whole body). Case-sensitive.

Assertions (`expect.json` values):

| Expectation form | Passes when |
|---|---|
| any plain JSON value (`"Pending"`, `5`, `null`, `[1,2]`, `{"a":1}`) | path found and value deep-equals it |
| `{"exists": true}` | path found (value may be `null`) |
| `{"exists": false}` | path not found |
| `{"contains": x}` | value is a string containing `x`, or a list with an element deep-equal to `x` |

An object whose only key is `exists` or `contains` is an operator; any other object is a plain value. After the first failing step, remaining steps are skipped.

## Quick command reference

Global flags on every command (put them after the command): `--app NAME`, `--env NAME`, `--json`.

| Command | Does | Exit |
|---|---|---|
| `apiprobe app` | Show detected app and all known apps | 0 |
| `apiprobe init NAME [--repo PATH]` | Write config template (`--repo` defaults to cwd) | 0 / 2 if exists |
| `apiprobe find TEXT` | Search spec (every word must match method/path/summary/operationId/tags) | 0 (also with no results) |
| `apiprobe show "METHOD /path"` | Describe one endpoint: params, body fields, responses | 0 / 2 |
| `apiprobe call METHOD PATH [--as ROLE] [--body JSON or @file | --form k=v ... | --part k=v or k=@path[;type=MIME][;filename=N] ... | --text STR or @file | --file @path] [--query k=v ...] [--header k=v ...]` | One request (one body flag; files must be inside the repo or cwd), no assertions | 0 if 2xx, 1 otherwise, 2 on error |
| `apiprobe run PLAN.json` | Execute plan | 0 passed / 1 failed / 2 error |

- `--json` prints the result as JSON only; errors as `{"error": {"code", "message"}}`.
- Error messages name the file and field to fix — relay them to the user as is.
- If `apiprobe` is not found, ask the user to install it from the apiprobe repo with `uv tool install --editable .` (then `uv tool update-shell` and a new terminal); do not install it yourself.
- **Windows Git Bash:** it rewrites arguments that start with `/` into Windows paths (`/api/x` → `C:/Program Files/Git/api/x`). Prefix commands that pass a path argument with `MSYS_NO_PATHCONV=1`, e.g. `MSYS_NO_PATHCONV=1 apiprobe call GET /api/leave --as admin`. Plan files are not affected.
