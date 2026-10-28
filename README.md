# apiprobe

AI-assisted API testing for backend developers. An AI coding assistant (Claude Code or similar) reads the app's OpenAPI spec and source code, drafts a test plan, you approve it, the `apiprobe` CLI runs it deterministically, and the AI reports results and asks you to confirm DB state.

- The AI decides **what** to assert; the CLI decides **pass/fail**.
- Nothing runs before you approve the plan.
- Python standard library only. No DB access (plans carry SQL for you to run).

## Install

Uses [uv](https://docs.astral.sh/uv/). apiprobe gets its own isolated environment, so nothing touches your global Python.

```powershell
cd C:\Projects\apiprobe
uv tool install --editable .
```

This puts `apiprobe.exe` in `~/.local/bin`. If `apiprobe` is not found, run `uv tool update-shell` and open a new terminal. `--editable` means code changes apply without reinstalling; after changing `pyproject.toml` (e.g. the console script), run `uv tool install --editable . --force`.

Don't also `pip install` it into your global Python — two installs can shadow each other.

Install the Claude Code skill by linking the `skill/` folder (from the repo root), so `git pull` keeps it current:

```powershell
New-Item -ItemType Junction -Path "$HOME\.claude\skills\apiprobe" -Target "$PWD\skill"
```

On macOS/Linux: `ln -s "$PWD/skill" ~/.claude/skills/apiprobe`. If you copied the folder before, delete that copy first.

Tests (uv creates `.venv` from `uv.lock` on first run): `uv run python -m unittest discover -s tests -v`

## Quick start

```powershell
cd C:\path\to\your-app
apiprobe init my-app            # writes ~/.apiprobe/apps/my-app.json with CHANGE_ME values
# edit the file: baseUrl, specUrl, and a token per role
apiprobe app                    # my-app is detected from this folder (or any subfolder)
apiprobe find leave
apiprobe show "POST /api/leave"
apiprobe call GET /api/leave --as admin
apiprobe run .apiprobe/plans/approve-leave.json
```

Or just ask Claude Code: "test the create leave endpoint" — the skill sets up the config (asking you only what it can't find in the repo), drafts a plan, and waits for your approval.

**Windows Git Bash:** it rewrites `/api/x` arguments into Windows paths. Prefix with `MSYS_NO_PATHCONV=1` (PowerShell and cmd are not affected).

## App config

`~/.apiprobe/apps/<app-name>.json` (set `APIPROBE_HOME` to use another root folder).

```json
{
  "repoPath": "C:/Projects/MyApp",
  "specUrl": "/swagger/v1/swagger.json",
  "defaultEnvironment": "local",
  "timeoutSeconds": 30,
  "environments": {
    "local": {
      "baseUrl": "https://localhost:5001",
      "verifyTls": false,
      "users": {
        "admin": { "token": "eyJ..." },
        "manager": { "token": "CHANGE_ME" }
      }
    }
  }
}
```

- `specUrl`: `/path` → joined to `baseUrl`; `http...` → as is; otherwise a local file (relative to `repoPath`). OpenAPI 3.x as JSON, or as YAML with the optional extra: `uv tool install --editable ".[yaml]" --force` (adds PyYAML; without it a YAML spec fails with that command in the error, and everything else stays standard-library only).
- Auth is token-only; apiprobe never handles usernames or passwords. Default is `Authorization: Bearer <token>`. Add one of `"scheme": "Basic"` (paste the base64 `user:password` value yourself), `"header": "X-Api-Key"`, `"query": "api_key"` (masked as `***` in output) or `"cookie": "session"` to a user for other schemes. Get a token from your app's login endpoint (e.g. in Swagger UI) or the browser devtools, and paste it in.
- `apiprobe show` prints the endpoint's `Auth:` requirement; `apiprobe app --json` lists the spec's security schemes (`--no-spec` skips the fetch).
- A `CHANGE_ME` token only errors when a command or plan actually uses that role. A JWT past its `exp` stops the run with TOKEN_EXPIRED before any request; a 401 shows a "token may be expired" hint. Either way: paste a fresh token and rerun.

## Plan file

Saved in the app repo at `.apiprobe/plans/<slug>.json` so the team can share it. Plans hold no secrets — users are referenced by role.

```json
{
  "title": "Reporting manager approves leave",
  "steps": [
    {
      "id": "create_leave",
      "call": { "method": "POST", "path": "/api/leave", "as": "staff",
                "body": { "from": "2026-11-01", "to": "2026-11-02", "note": "test {{$uuid}}" } },
      "extract": { "leaveId": "data.id" },
      "expect": { "status": 201, "json": { "data.status": "Pending", "data.id": { "exists": true } } },
      "source": "CreateLeaveHandler.cs:58"
    },
    {
      "id": "approve",
      "call": { "method": "POST", "path": "/api/leave/{{leaveId}}/approve", "as": "manager" },
      "expect": { "status": 200 }
    }
  ],
  "manualChecks": [
    { "sql": "SELECT Status FROM LeaveRequests WHERE Id = '{{leaveId}}'", "expect": "Status = Approved", "why": "Approval must be persisted" }
  ],
  "cleanup": [ { "method": "DELETE", "path": "/api/leave/{{leaveId}}", "as": "admin" } ]
}
```

Assertions: plain JSON value (deep-equal) or one-key operators `exists`, `contains`, `matches` (regex), `length`, `type`, `gt`/`gte`/`lt`/`lte`, `in`, `equals` (for a literal object whose only key is an operator name). `expect.status` takes an integer, a list or a class like `"2xx"` (missing → any 2xx); `expect.headers`, `expect.text` and `expect.maxMs` check headers, raw body text and response time. `extract` reads a body path, `"header:Location"` or `"cookie:NAME"`; a plan with `"cookies": true` carries cookies between steps. Built-in variables: `{{$uuid}}`, `{{$now}}`. After the first failing step the rest are skipped; cleanup always runs. Full rules are in `skill/SKILL.md`.

Request bodies: a call carries at most one of `body` (JSON), `form` (urlencoded), `multipart` (text, `{"json": X}` and `{"file": path}` parts), `text` or `file` (raw). Upload files must live inside the app's `repoPath` or the plan's folder (put fixtures in `<repo>/.apiprobe/files/`); anything else is rejected before a request is sent. Runs record file name, size and type, never file bytes.

Plan-level `"vars": {"depotId": 12}` seeds variables; `extract` may overwrite them.

Binary responses (PDF, images, archives…) are never decoded: the body is reported as `{"binary": true, "bytes", "contentType", "sha256"}` and can be asserted on. `call.saveTo` (inside the repo or plan folder) also writes the bytes of a 2xx response to disk.

Every run is saved to `~/.apiprobe/runs/<app>/<UTC timestamp>-<plan>.json` (full response bodies; no request headers, so no tokens). The newest 50 runs per app are kept; older ones are deleted.

Redirects are not followed: a 3xx comes back as the step's result (assert on `status` and `headers.Location`), and the token is never sent to another host.

## Commands

Global flags (after the command): `--app NAME`, `--env NAME`, `--json`. `find`, `show`, `scaffold` and `check` cache the fetched spec for 10 minutes in `~/.apiprobe/cache/`; `--refresh` bypasses it, and an endpoint missing from a cached spec refetches once.

| Command | Does | Exit |
|---|---|---|
| `apiprobe app` | Show detected app and all known apps | 0 |
| `apiprobe init NAME [--repo PATH]` | Write config template (`--repo` defaults to cwd) | 0 / 2 if exists |
| `apiprobe find TEXT` | Search spec | 0 (also with no results) |
| `apiprobe show "METHOD /path"` | Describe one endpoint | 0 / 2 |
| `apiprobe scaffold "METHOD /path" [--as ROLE]` | Print one ready plan step (path variables, required fields, body type, expected status) | 0 / 2 |
| `apiprobe check PLAN.json` | Check a plan against the spec without calling the API: endpoints, required query params, body type, and JSON body values (type, enum, uuid/date formats, nested required fields; `{{vars}}` skipped) | 0 ok / 1 problems / 2 error |
| `apiprobe call METHOD PATH [--as ROLE] [--body JSON/@file / --form k=v / --part k=v or k=@path[;type=MIME][;filename=N] / --text STR/@file / --file @path] [--query k=v ...] [--header k=v ...] [--pick PATH] [--header-out NAME] [--max-chars N] [--full]` | One request, no assertions (one body flag). `--json` is compact; `--full` prints everything | 0 if 2xx, 1 otherwise, 2 on error |
| `apiprobe run PLAN.json ... [--only STEP_ID] [--max-chars N] [--full]` | Execute plans (several files or a folder of `*.json`: one saved run each plus a suite summary); `--only` stops after that step, cleanup still runs; `--json` is compact, `--full` prints everything | 0 passed / 1 failed / 2 error, including an unreachable API (worst of all) |
| `apiprobe last [--step ID [--pick PATH]] [--plan NAME]` | Newest saved run (compact), one step in full, or one value of its response body | 0 / 2 |
| `apiprobe runs [--limit N]` | List saved runs, newest first (default 10) | 0 |

## Security notes

- Configs hold **plaintext tokens** (no passwords). They live in your home folder, never in a repo. Use dev/test accounts only; short-lived tokens limit the damage if one leaks.
- A token you paste into the AI chat ends up in the conversation transcript. To keep it out, paste it straight into the config file instead.
- Only put dev/test environments in configs. **Never prod.** Plans contain only paths, never full URLs, so a plan can only reach hosts that are in your config.
- Responses (which may contain dev data) are read by the AI assistant and saved in plain text under `~/.apiprobe/runs/` (last 50 per app). If your dev DB holds copies of real personal data, keep that in mind.
- `verifyTls: false` disables certificate checks for that environment; use it only for local self-signed certs.
