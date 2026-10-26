"""App config: ~/.apiprobe/apps/<name>.json (PLAN.md section 7)."""
import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .errors import ApiProbeError

PLACEHOLDER = "CHANGE_ME"
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_AUTH_PLACES = ("scheme", "header", "query", "cookie")
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def home() -> Path:
    return Path(os.environ.get("APIPROBE_HOME") or Path.home() / ".apiprobe")


def _apps_dir() -> Path:
    return home() / "apps"


def _config_path(name: str) -> Path:
    if not _NAME_RE.fullmatch(name):
        raise ApiProbeError("APP_NOT_FOUND", f"invalid app name '{name}' (use letters, digits, '-', '_', '.')")
    return _apps_dir() / f"{name}.json"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ApiProbeError("CONFIG_INVALID", f"{path}: not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})")


def list_apps() -> list[dict]:
    apps = []
    for path in sorted(_apps_dir().glob("*.json")):
        entry = {"name": path.stem, "configPath": str(path), "repoPath": None, "environments": []}
        try:
            raw = _read_json(path)
            entry["repoPath"] = raw.get("repoPath")
            entry["environments"] = list((raw.get("environments") or {}).keys())
        except (ApiProbeError, AttributeError) as e:
            entry["error"] = getattr(e, "message", f"{path}: top level must be a JSON object")
        apps.append(entry)
    return apps


def _validate(cfg, path: Path) -> list[str]:
    if not isinstance(cfg, dict):
        return ["top level must be a JSON object"]
    problems = []

    def need_str(obj, key, where):
        if not isinstance(obj.get(key), str) or not obj[key]:
            problems.append(f"field '{where}' is required and must be a non-empty string")

    need_str(cfg, "specUrl", "specUrl")
    if "repoPath" in cfg and not isinstance(cfg["repoPath"], str):
        problems.append("field 'repoPath' must be a string")
    timeout = cfg.get("timeoutSeconds", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        problems.append("field 'timeoutSeconds' must be a positive number")

    if "login" in cfg:
        problems.append("field 'login' is no longer supported; remove it and give each user a 'token'")

    envs = cfg.get("environments")
    if not isinstance(envs, dict) or not envs:
        problems.append("field 'environments' is required and must be a non-empty object")
        envs = {}
    if not isinstance(cfg.get("defaultEnvironment"), str):
        problems.append("field 'defaultEnvironment' is required and must be a string")
    elif envs and cfg["defaultEnvironment"] not in envs:
        problems.append(f"field 'defaultEnvironment' is '{cfg['defaultEnvironment']}' but environments has only: {', '.join(envs)}")

    for env_name, env in envs.items():
        where = f"environments.{env_name}"
        if not isinstance(env, dict):
            problems.append(f"field '{where}' must be an object")
            continue
        base = env.get("baseUrl")
        if not isinstance(base, str) or not base.startswith(("http://", "https://")):
            problems.append(f"field '{where}.baseUrl' is required and must start with http:// or https://")
        if "verifyTls" in env and not isinstance(env["verifyTls"], bool):
            problems.append(f"field '{where}.verifyTls' must be true or false")
        users = env.get("users", {})
        if not isinstance(users, dict):
            problems.append(f"field '{where}.users' must be an object")
            continue
        for role, user in users.items():
            problems += _user_problems(user, f"{where}.users.{role}")
    return problems


def _user_problems(user, where: str) -> list[str]:
    if not isinstance(user, dict) or not isinstance(user.get("token"), str):
        return [f"field '{where}' must be {{\"token\": \"...\"}} (username/password is not supported)"]
    if user.keys() & {"username", "password"}:
        return [f"field '{where}' has username/password, which is not supported; keep only the token"]
    problems = []
    if _CONTROL_RE.search(user["token"]):
        problems.append(f"field '{where}.token' contains a line break or control character; paste only the token")
    given = [k for k in _AUTH_PLACES if k in user]
    if len(given) > 1:
        problems.append(f"field '{where}' has {' and '.join(given)}; use at most one of {', '.join(_AUTH_PLACES)}")
    for k in given:
        v = user[k]
        ok = isinstance(v, str) and (_HEADER_NAME_RE.fullmatch(v) if k != "query" else bool(v))
        if k == "scheme":
            ok = isinstance(v, str) and v.isalnum()
        if not ok:
            problems.append(f"field '{where}.{k}' must be a non-empty {'word' if k == 'scheme' else 'name'} string, got {json.dumps(v)}")
    return problems


def load_app(name: str) -> dict:
    path = _config_path(name)
    if not path.is_file():
        known = ", ".join(a["name"] for a in list_apps()) or "none"
        raise ApiProbeError("APP_NOT_FOUND", f"no config for app '{name}' at {path}. Known apps: {known}. Create one with 'apiprobe init {name}'")
    cfg = _read_json(path)
    problems = _validate(cfg, path)
    if problems:
        raise ApiProbeError("CONFIG_INVALID", f"{path}: " + "; ".join(problems))
    return {**cfg, "name": name, "configPath": str(path)}


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(p)).rstrip("\\/")


def detect_app(cwd: Path) -> dict:
    here = _norm(str(cwd))
    best = None
    for app in list_apps():
        repo = app.get("repoPath")
        if not isinstance(repo, str) or not repo:
            continue
        root = _norm(repo)
        if (here == root or here.startswith(root + os.sep)) and (best is None or len(root) > best[0]):
            best = (len(root), app["name"])
    if best is None:
        known = ", ".join(a["name"] for a in list_apps()) or "none"
        raise ApiProbeError("APP_NOT_FOUND", f"no app config has a repoPath containing {cwd}. Known apps: {known}. Pass --app NAME or create one with 'apiprobe init NAME'")
    return load_app(best[1])


def init_app(name: str, repo_path: str | None) -> Path:
    path = _config_path(name)
    if path.exists():
        raise ApiProbeError("APP_EXISTS", f"{path} already exists; edit that file instead")
    template = {
        "repoPath": str(Path(repo_path).resolve()).replace("\\", "/") if repo_path else None,
        "specUrl": "/swagger/v1/swagger.json",
        "defaultEnvironment": "local",
        "timeoutSeconds": 30,
        "environments": {
            "local": {
                "baseUrl": "https://localhost:5001",
                "verifyTls": False,
                "users": {"admin": {"token": PLACEHOLDER}},
            }
        },
    }
    if template["repoPath"] is None:
        del template["repoPath"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    return path


def resolve_env(app: dict, env: str | None) -> dict:
    name = env or app["defaultEnvironment"]
    envs = app["environments"]
    if name not in envs:
        raise ApiProbeError("ENV_NOT_FOUND", f"environment '{name}' not in {app['configPath']} (environments has: {', '.join(envs)})")
    e = envs[name]
    return {"name": name, "baseUrl": e["baseUrl"].rstrip("/"), "verifyTls": e.get("verifyTls", True), "users": e.get("users", {})}


def jwt_expiry(token: str) -> int | None:
    """The `exp` claim of a JWT, or None if the token is not a JWT or has no exp."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return exp if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


def get_user(app: dict, env: dict, role: str) -> dict:
    """The user entry for a role; raises only when the role is actually used (PLAN.md 7.4)."""
    where = f"environments.{env['name']}.users"
    if role not in env["users"]:
        known = ", ".join(env["users"]) or "none"
        raise ApiProbeError("USER_NOT_FOUND", f"role '{role}' not in {app['configPath']} field '{where}' (known roles: {known})")
    user = env["users"][role]
    field = f"field '{where}.{role}.token' in {app['configPath']}"
    if not user["token"].strip() or user["token"] == PLACEHOLDER:
        raise ApiProbeError("USER_NOT_CONFIGURED", f"no token for role '{role}': paste a fresh token into {field}")
    exp = jwt_expiry(user["token"])
    if exp is not None and exp <= time.time():
        when = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        raise ApiProbeError("TOKEN_EXPIRED", f"token for role '{role}' expired at {when}: generate a new one and paste it into {field}")
    return user
