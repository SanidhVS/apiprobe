"""Run results on disk: ~/.apiprobe/runs/<app>/<UTC yyyyMMdd-HHmmss>-<plan-slug>.json."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import home


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


def list_runs(app_name: str) -> list[dict]:
    runs = []
    for path in sorted((home() / "runs" / app_name).glob("*.json"), reverse=True):
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs.append({k: r.get(k) for k in ("title", "environment", "status", "startedAt", "durationMs")} | {"path": str(path)})
    return runs
