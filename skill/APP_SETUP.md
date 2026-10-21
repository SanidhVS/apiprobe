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

On Windows Git Bash, prefix commands that pass a `/path` argument with `MSYS_NO_PATHCONV=1`. If `apiprobe` is not on PATH, use `python -m apiprobe`.

Report: what was discovered (with sources), what the user provided, what is still missing.

## Config schema (reference)

Location: `~/.apiprobe/apps/<app-name>.json` (file name = app name). Override the root folder with env var `APIPROBE_HOME`.

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
        "admin":   { "username": "admin@dev.local", "password": "CHANGE_ME" },
        "manager": { "username": "mgr@dev.local",   "password": "CHANGE_ME" },
        "tokenOnly": { "token": "eyJ..." }
      }
    },
    "dev": {
      "baseUrl": "https://dev-myapp.example.com",
      "users": { "admin": { "username": "...", "password": "..." } }
    }
  }
}
```

| Field | Required | Rule |
|---|---|---|
| `repoPath` | no | Absolute path. Used for app detection (current folder or any subfolder). Case and `/` vs `\` don't matter. |
| `specUrl` | yes | Starts with `/` → joined to the environment's `baseUrl`. Starts with `http` → used as is. Otherwise → local file path (relative to `repoPath`). OpenAPI 3.x JSON only. |
| `defaultEnvironment` | yes | Must be a key of `environments`. |
| `timeoutSeconds` | no | Default 30. |
| `login` | no | Omit if the app uses static tokens only or no auth. |
| `login.method` | no | Default `POST`. |
| `login.path` | yes if login | Starts with `/`. |
| `login.body` | yes if login | Any JSON; `{{username}}`/`{{password}}` are substituted from the user entry. |
| `login.tokenPath` | yes if login | Dotted path into the login response JSON (`data.token`, `accessToken`, `items[0].token`). |
| `environments.<env>.baseUrl` | yes | `http://` or `https://`; trailing slash is stripped. |
| `environments.<env>.verifyTls` | no | Default `true`. `false` for local self-signed dev certs. |
| `environments.<env>.users.<role>` | — | Either `{username, password}` or `{token}`. Token is sent as `Authorization: Bearer <token>`. |

A user whose `username`/`password` is `CHANGE_ME` or whose `token` is empty fails with USER_NOT_CONFIGURED only when a command or plan uses that role.
