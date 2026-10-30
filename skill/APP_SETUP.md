# apiprobe — App setup guide (for the agent)

Use this when `apiprobe app` finds no config for the current repo, when a config is incomplete (USER_NOT_CONFIGURED, CONFIG_INVALID), or when the user wants to add an environment or user. Goal: a valid `~/.apiprobe/apps/<name>.json` with as few questions as possible.

## Rules
- Discover first, ask second. Ask only for what the repo cannot tell you.
- Ask all remaining questions in ONE message, numbered, each with your discovered or suggested answer, so the user can reply "ok" or correct it.
- Never add a production URL. If a URL may be prod (no local/dev/test/qa/staging marker, or the user says prod), stop and ask.
- Auth is token-only (bearer, API key, cookie or pre-encoded Basic). Never ask for, accept or store usernames or passwords. Write `CHANGE_ME` for each role's token, then ask the user for the tokens: they paste them here (you write them into the file) or into the file themselves.
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
| auth | `[Authorize]`/auth middleware present → users need tokens; none → users can be omitted. Type (check in order): spec `components.securitySchemes` (`apiprobe app --json` lists them); then code: .NET `AddJwtBearer` (bearer), custom `AuthenticationHandler` reading `Request.Headers["X-Api-Key"]` (header), `AuthenticationHeaderValue("Basic"` (basic); FastAPI `HTTPBearer`/`APIKeyHeader(name=…)`/`APIKeyQuery`/`APIKeyCookie`/`HTTPBasic`; Express/Nest `req.headers['x-api-key']`, `passport-http-bearer`, `passport-headerapikey`, `@ApiSecurity`; Spring `oauth2ResourceServer().jwt()` (bearer), `httpBasic()` (basic), a filter reading a header. Unsure → ask, listing what you found. |
| how to get a token | so you can tell the user: the app's login endpoint (route named login/token/auth/signin — e.g. run it in Swagger UI and copy the token field from the response), or the browser devtools Network tab (`Authorization: Bearer ...` header of any API call) when the app uses an external identity provider. Token lifetime setting (e.g. `JwtValidFor`, `AccessTokenLifetime`) tells the user how often to refresh |
| upload limits | only if the app takes files: `MaxRequestBodySize`/`[RequestSizeLimit]`, multer `limits`, `spring.servlet.multipart.max-file-size` — needed for the 413 negative case |
| roles | role names in `[Authorize(Roles=...)]`, policies, role enums, seed data; data scoping (depot, tenant, branch) → suggest one user per scope for access tests |

## Step 2 — Ask the user (one message)
Template:
1. App name: `<suggested>` — ok?
2. Environments: local `<discovered URL>` (from `<file:line>`) — ok? Any hosted dev/test environment? Its URL?
3. Auth: `<bearer token | API key in header X-Api-Key | …>`. Get one from `<POST /api/users/login in Swagger UI → copy token>` (from `<file:line>`); tokens last `<lifetime>`. Correct?
4. Test users per environment: suggested roles `<admin, manager, staff-depotA>` — username for each?
5. Tokens: paste one per role here and I'll write them into the file, or I'll leave `CHANGE_ME` and you paste them into `<config path>` yourself — which?
6. Timeout 30 seconds — ok?

## Step 3 — Write the config
- New app: run `apiprobe init <name> --repo <repoPath>`, then edit the created file with the answers.
- Existing app: edit in place, after showing the change.
- The file must match the config schema below.

## Step 4 — Verify
1. `apiprobe app --json` → the app is detected from the repo.
2. `apiprobe find <a word from a known route> --json` → spec reachable. On SPEC_UNAVAILABLE, ask the user to start the API or recheck `specUrl`.
3. If tokens are filled: `apiprobe call GET <a simple authenticated GET endpoint> --as <role>` → 2xx. On TOKEN_EXPIRED or 401, ask for a fresh token for that role.
4. If `CHANGE_ME` remains: list each file + field the user must fill, and stop until they confirm.

On Windows Git Bash, prefix commands that pass a `/path` argument with `MSYS_NO_PATHCONV=1`. If `apiprobe` is not found, ask the user to install it from the apiprobe repo with `uv tool install --editable .` (then `uv tool update-shell` and a new terminal); do not install it yourself.

Report: what was discovered (with sources), what the user provided, what is still missing.

## Config schema (reference)

Location: `~/.apiprobe/apps/<app-name>.json` (file name = app name). Override the root folder with env var `APIPROBE_HOME`.

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
        "admin":   { "token": "eyJ..." },
        "manager": { "token": "CHANGE_ME" }
      }
    },
    "dev": {
      "baseUrl": "https://dev-myapp.example.com",
      "users": { "admin": { "token": "CHANGE_ME" } }
    }
  }
}
```

| Field | Required | Rule |
|---|---|---|
| `repoPath` | no | Absolute path. Used for app detection (current folder or any subfolder). Case and `/` vs `\` don't matter. |
| `specUrl` | yes | Starts with `/` → joined to the environment's `baseUrl`. Starts with `http` → used as is. Otherwise → local file path (relative to `repoPath`). OpenAPI 3.x, JSON or YAML (YAML needs the user to run `uv tool install --editable ".[yaml]" --force` from the apiprobe repo; the SPEC_UNAVAILABLE error says so). |
| `defaultEnvironment` | yes | Must be a key of `environments`. |
| `timeoutSeconds` | no | Default 30. |
| `environments.<env>.baseUrl` | yes | `http://` or `https://`; trailing slash is stripped. |
| `environments.<env>.verifyTls` | no | Default `true`. `false` for local self-signed dev certs. |
| `environments.<env>.users.<role>` | — | `{"token": "..."}` plus at most one of: `"scheme": "Basic"` → `Authorization: Basic <token>` (any scheme word); `"header": "X-Api-Key"` → that header; `"query": "api_key"` → `?api_key=<token>` (masked as `***` in output); `"cookie": "session"` → `Cookie: session=<token>`. No extra key → `Authorization: Bearer <token>`. Basic: the user pastes the pre-encoded base64 value into the file; never ask for the username/password. Username/password keys and a `login` section are rejected. |

A role whose token is `CHANGE_ME` or empty fails with USER_NOT_CONFIGURED, and a JWT past its `exp` fails with TOKEN_EXPIRED — both only when a command or plan uses that role, and before any request is sent.
