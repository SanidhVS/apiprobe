"""One HTTP call, stdlib urllib only (PLAN.md 8.6, 10)."""
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .body import guess_type, iter_parts
from .errors import ApiProbeError


MASK = "***"


def _text(v) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def build_url(env: dict, path: str, query: dict | None = None) -> str:
    url = env["baseUrl"] + path
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({k: _text(v) for k, v in query.items()})
    return url


def _read(path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as e:
        raise ApiProbeError("PLAN_INVALID", f"could not read file {path}: {e}")


def _quote(s: str) -> str:
    return s.replace("\r", "").replace("\n", "").replace('"', "%22")


def _encode_multipart(fields: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = []
    for name, part in iter_parts(fields):
        disposition, ctype = f'form-data; name="{_quote(name)}"', None
        if isinstance(part, dict) and "file" in part:
            filename = part.get("filename") or Path(part["file"]).name
            disposition += f'; filename="{_quote(filename)}"'
            ctype, content = part.get("contentType") or guess_type(part["file"]), _read(part["file"])
        elif isinstance(part, dict):
            ctype, content = "application/json", json.dumps(part["json"]).encode("utf-8")
        else:
            content = _text(part).encode("utf-8")
        head = f"--{boundary}\r\nContent-Disposition: {disposition}\r\n" + (f"Content-Type: {ctype}\r\n" if ctype else "") + "\r\n"
        out += [head.encode("utf-8"), content, b"\r\n"]
    out.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _encode_other(form, multipart, text, file) -> tuple[bytes, str, bool] | None:
    """(data, content type, may a caller header override the type) for form/multipart/text/file bodies."""
    if form is not None:
        pairs = [(k, _text(x)) for k, v in form.items() for x in (v if isinstance(v, list) else [v])]
        return urllib.parse.urlencode(pairs).encode("ascii"), "application/x-www-form-urlencoded", True
    if multipart is not None:
        data, ctype = _encode_multipart(multipart)
        return data, ctype, False
    if text is not None:
        return text.encode("utf-8"), "text/plain", True
    if file is not None:
        return _read(file), guess_type(file), True
    return None


def _add_key(url: str, name: str, value: str) -> str:
    return url + ("&" if "?" in url else "?") + f"{urllib.parse.quote_plus(name)}={value}"


def send(env: dict, timeout: int, method, path, query=None, headers=None, body=None, auth=None, *,
         form=None, multipart=None, text=None, file=None) -> dict:
    """Send one request. Any HTTP status comes back as data; only transport errors raise REQUEST_FAILED.
    auth is a config user entry: {"token"} plus at most one of scheme/header/query/cookie (D16); a query-string
    token is masked in the returned and error urls.
    At most one of body/form/multipart/text/file; file paths must already be resolved and checked (body.resolve_file)."""
    url = shown = build_url(env, path, query)
    req_headers = {"Accept": "application/json", **{k: _text(v) for k, v in (headers or {}).items()}}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    elif (other := _encode_other(form, multipart, text, file)) is not None:
        data, ctype, overridable = other
        have = [k for k in req_headers if k.lower() == "content-type"]
        if not (overridable and have):
            for k in have:
                del req_headers[k]
            req_headers["Content-Type"] = ctype
    if auth:
        token = auth["token"]
        if "query" in auth:
            url, shown = _add_key(url, auth["query"], urllib.parse.quote_plus(token)), _add_key(url, auth["query"], MASK)
        elif "header" in auth:
            req_headers[auth["header"]] = token
        elif "cookie" in auth:
            have = next((k for k in req_headers if k.lower() == "cookie"), "Cookie")
            req_headers[have] = "; ".join(filter(None, [req_headers.get(have), f"{auth['cookie']}={token}"]))
        else:
            req_headers["Authorization"] = f"{auth.get('scheme', 'Bearer')} {token}"
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
        raise ApiProbeError("REQUEST_FAILED", f"{method.upper()} {shown} failed: {reason}. Is the API running, and is baseUrl right in the app config?")
    duration = round((time.perf_counter() - started) * 1000)

    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return {"status": status, "headers": resp_headers, "body": parsed, "bodyText": text, "durationMs": duration, "url": shown}

