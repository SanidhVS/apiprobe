"""Run results on disk: ~/.apiprobe/runs/<app>/<UTC yyyyMMdd-HHmmss>-<plan-slug>.json."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import home
from .errors import ApiProbeError


def _slug(result: dict) -> str:
    base = Path(result["planPath"]).stem if result.get("planPath") else (result.get("title") or "run")
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "run"


def save_run(app_name: str, result: dict) -> Path:
    folder = home() / "runs" / app_name
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{_slug(result)}"
    path, n = folder / f"{stem}.json", 2
    while path.exists():
        path, n = folder / f"{stem}-{n}.json", n + 1
    result["savedTo"] = str(path)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _newest_first(app_name: str) -> list[Path]:
    """Second-resolution timestamp in the name, then write time (runs in the same second get a -2 suffix that does not sort)."""
    paths = (home() / "runs" / app_name).glob("*.json")
    return sorted(paths, key=lambda p: (p.stem[:15], p.stat().st_mtime_ns), reverse=True)


def latest_run(app_name: str, plan: str | None = None) -> dict:
    """The newest saved run, optionally of one plan (its file name or slug)."""
    slug = re.sub(r"[^a-z0-9]+", "-", Path(plan).stem.lower()).strip("-") if plan else None
    for path in _newest_first(app_name):
        if slug is None or re.fullmatch(rf"\d{{8}}-\d{{6}}-{re.escape(slug)}(-\d+)?", path.stem):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    raise ApiProbeError("RUN_NOT_FOUND", f"no saved run for app '{app_name}'" + (f" and plan '{plan}'" if plan else "") + f" in {home() / 'runs' / app_name}")


def list_runs(app_name: str, limit: int | None = None) -> list[dict]:
    runs = []
    for path in _newest_first(app_name)[:limit]:
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs.append({k: r.get(k) for k in ("title", "environment", "status", "startedAt", "durationMs")} | {"path": str(path)})
    return runs
