"""One HTTP call + login, stdlib urllib only (PLAN.md 8.6, 10)."""
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import get_user
from .errors import ApiProbeError
from .vars import lookup, substitute


def _text(v) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def build_url(env: dict, path: str, query: dict | None = None) -> str:
    url = env["baseUrl"] + path
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({k: _text(v) for k, v in query.items()})
    return url


def send(env: dict, timeout: int, method, path, query=None, headers=None, body=None, token=None) -> dict:
    """Send one request. Any HTTP status comes back as data; only transport errors raise REQUEST_FAILED."""
    url = build_url(env, path, query)
    req_headers = {"Accept": "application/json", **{k: _text(v) for k, v in (headers or {}).items()}}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())

    context = None
    if url.startswith("https://") and not env.get("verifyTls", True):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    started = time.perf_counter()
    try:
        try:
            resp = urllib.request.urlopen(request, timeout=timeout, context=context)
        except urllib.error.HTTPError as e:
            resp = e  # non-2xx is a normal result
        with resp:
            raw = resp.read()
            status, resp_headers = resp.status, dict(resp.headers.items())
    except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as e:
        reason = getattr(e, "reason", e)
        raise ApiProbeError("REQUEST_FAILED", f"{method.upper()} {url} failed: {reason}. Is the API running, and is baseUrl right in the app config?")
    duration = round((time.perf_counter() - started) * 1000)

    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return {"status": status, "headers": resp_headers, "body": parsed, "bodyText": text, "durationMs": duration, "url": url}


def login(app: dict, env: dict, role: str) -> str:
    """Return a bearer token for the role: static token, or call the app's login endpoint."""
    user = get_user(app, env, role)
    if "token" in user:
        return user["token"]
    cfg = app.get("login")
    if not cfg:
        raise ApiProbeError("LOGIN_FAILED", f"role '{role}' has username/password but {app['configPath']} has no 'login' section; add one or give the role a 'token'")
    body = substitute(cfg["body"], {"username": user["username"], "password": user["password"]})
    method = cfg.get("method", "POST")
    resp = send(env, app.get("timeoutSeconds", 30), method, cfg["path"], body=body)
    where = f"{method.upper()} {cfg['path']} as '{role}'"
    if not 200 <= resp["status"] < 300:
        raise ApiProbeError("LOGIN_FAILED", f"{where} returned {resp['status']}: {resp['bodyText'][:300]}. Check credentials and 'login' in {app['configPath']}")
    found, token = lookup(resp["body"], cfg["tokenPath"])
    if not found or not isinstance(token, str) or not token:
        raise ApiProbeError("LOGIN_FAILED", f"{where}: no token at tokenPath '{cfg['tokenPath']}' in response {resp['bodyText'][:300]}. Fix 'login.tokenPath' in {app['configPath']}")
    return token
