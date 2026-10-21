# apiprobe

AI-assisted API testing for backend developers. An AI coding assistant (Claude Code or similar) reads the app's OpenAPI spec and source code, drafts a test plan, you approve it, the `apiprobe` CLI runs it deterministically, and the AI reports results and asks you to confirm DB state.

- The AI decides **what** to assert; the CLI decides **pass/fail**.
- Nothing runs before you approve the plan.
- Python standard library only. No DB access (plans carry SQL for you to run).

## Install

```powershell
cd C:\Projects\apiprobe
pip install -e .
```

This puts `apiprobe` in Python's `Scripts` folder. If `apiprobe` is not found, add that folder to PATH (`python -c "import sysconfig; print(sysconfig.get_path('scripts'))"` prints it) or use `python -m apiprobe` instead.

Install the Claude Code skill by copying the `skill/` folder:

```powershell
Copy-Item -Recurse -Force skill "$HOME\.claude\skills\apiprobe"
```

Tests: `python -m unittest discover -s tests -v`

## Quick start

```powershell
cd C:\path\to\your-app
apiprobe init my-app            # writes ~/.apiprobe/apps/my-app.json with CHANGE_ME values
# edit the file: baseUrl, specUrl, login, users
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
        "admin": { "username": "admin@dev.local", "password": "CHANGE_ME" },
        "bot": { "token": "eyJ..." }
      }
    }
  }
}
```

- `specUrl`: `/path` → joined to `baseUrl`; `http...` → as is; otherwise a local file (relative to `repoPath`). OpenAPI 3.x JSON only.
- Users are `{username, password}` (logged in via `login`) or `{token}` (sent as `Authorization: Bearer`).
- A `CHANGE_ME` user only errors when a command or plan actually uses that role.

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

Assertions: plain JSON value (deep-equal), `{"exists": true|false}`, `{"contains": x}` (substring or list element). Missing `expect.status` → any 2xx passes. Built-in variables: `{{$uuid}}`, `{{$now}}`. After the first failing step the rest are skipped; cleanup always runs. Full rules are in `skill/SKILL.md`.

Every run is saved to `~/.apiprobe/runs/<app>/<UTC timestamp>-<plan>.json` (full response bodies; no request headers, so no tokens).

## Commands

Global flags (after the command): `--app NAME`, `--env NAME`, `--json`.

| Command | Does | Exit |
|---|---|---|
| `apiprobe app` | Show detected app and all known apps | 0 |
| `apiprobe init NAME [--repo PATH]` | Write config template (`--repo` defaults to cwd) | 0 / 2 if exists |
| `apiprobe find TEXT` | Search spec | 0 (also with no results) |
| `apiprobe show "METHOD /path"` | Describe one endpoint | 0 / 2 |
| `apiprobe call METHOD PATH [--as ROLE] [--body JSON or @file] [--query k=v ...] [--header k=v ...]` | One request, no assertions | 0 if 2xx, 1 otherwise, 2 on error |
| `apiprobe run PLAN.json` | Execute plan | 0 passed / 1 failed / 2 error |

## Security notes

- Configs hold **plaintext credentials**. They live in your home folder, never in a repo. Use dev/test accounts only.
- Only put dev/test environments in configs. **Never prod.** Plans contain only paths, never full URLs, so a plan can only reach hosts that are in your config.
- Responses (which may contain dev data) are read by the AI assistant. If your dev DB holds copies of real personal data, keep that in mind.
- `verifyTls: false` disables certificate checks for that environment; use it only for local self-signed certs.
