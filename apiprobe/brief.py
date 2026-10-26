"""Compact views of call and run results, so the agent reads less (PLAN.md 19.5, D17). Saved runs stay full."""
import json

from .vars import lookup

KEEP_HEADERS = ("content-type", "content-length", "location", "content-disposition", "etag")


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... ({len(text) - limit} more chars, see saved run)"


def _clip(value, limit: int):
    """value as is when its JSON text fits the limit, else a truncated string."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return value if len(text) <= limit else truncate(text, limit)


def pick(value, path: str) -> dict:
    found, picked = lookup(value, path)
    return {"pick": path, "found": found, **({"value": picked} if found else {})}


def brief_call(resp: dict, header_out=(), path: str | None = None) -> dict:
    """status, the few headers worth reading, and the body; bodyText only when the body is not JSON. With path: only that value."""
    if path is not None:
        return {"status": resp["status"], "durationMs": resp["durationMs"], "url": resp["url"], **pick(resp["body"], path)}
    wanted = {h.lower() for h in header_out}
    headers = {k.lower(): v for k, v in resp["headers"].items() if k.lower() in KEEP_HEADERS or k.lower() in wanted}
    if resp["cookieNames"] and "set-cookie" not in wanted:  # values are session secrets: names only
        headers["set-cookie"] = resp["cookieNames"]
    out = {"status": resp["status"], "headers": headers, "body": resp["body"], "durationMs": resp["durationMs"], "url": resp["url"]}
    if resp["body"] is None:
        out["bodyText"] = resp["bodyText"]
    if resp["saved"]:
        out["saved"] = resp["saved"]
    return out


def _step(s: dict, limit: int) -> dict:
    out = {"id": s["id"], "status": s["status"]}
    resp = s["response"]
    if s["status"] == "skipped":
        return out
    out["durationMs"] = resp["durationMs"] if resp else 0
    if s["status"] == "passed":
        return out
    if s["request"]:
        out["request"] = {**s["request"], "body": _clip(s["request"]["body"], limit)}
    out["assertions"] = [{**a, "actual": _clip(a["actual"], limit)} for a in s["assertions"] if not a["passed"]]
    if s["error"]:
        out["error"] = s["error"]
    if resp:
        out["response"] = {"status": resp["status"], "body": _clip(resp["body"], limit)}
    if s.get("source"):
        out["source"] = s["source"]
    return out


def brief_run(r: dict, limit: int) -> dict:
    """Passed steps are id/status/time; failed steps carry what is needed to diagnose them (bodies cut at limit)."""
    out = {k: r[k] for k in ("app", "environment", "title", "status", "durationMs") if k in r}
    if r.get("error"):
        out["error"] = r["error"]
    out["steps"] = [_step(s, limit) for s in r["steps"]]
    if r.get("cleanup"):
        out["cleanup"] = [{k: v for k, v in c.items() if v is not None} for c in r["cleanup"]]
    for key in ("manualChecks", "variables"):
        if r.get(key):
            out[key] = r[key]
    out["savedTo"] = r.get("savedTo")
    return out
