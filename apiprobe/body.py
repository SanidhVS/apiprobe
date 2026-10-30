"""Request body types besides JSON: shape checks, file containment, result summaries (PLAN.md 19.3, D14, D15)."""
import mimetypes
from pathlib import Path

from .errors import ApiProbeError

BODY_KEYS = ("body", "form", "multipart", "text", "file")
_SCALAR = (str, int, float, bool)


def guess_type(path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def resolve_file(path, where: str, base, roots) -> Path:
    """Absolute path of an existing file that sits inside one of roots (D15). Relative paths start at base."""
    if not isinstance(path, str) or not path:
        raise ApiProbeError("PLAN_INVALID", f"{where} must be a non-empty file path string, got {path!r}")
    p = Path(path)
    full = (p if p.is_absolute() else Path(base) / p).resolve()  # follows '..' and symlinks
    if not any(full.is_relative_to(Path(r).resolve()) for r in roots if r):
        inside = " or ".join(dict.fromkeys(str(Path(r).resolve()) for r in roots if r))
        raise ApiProbeError("PLAN_INVALID", f"{where}: '{path}' resolves outside {inside}; move the file under '.apiprobe/files/' in the repo")
    if not full.is_file():
        raise ApiProbeError("PLAN_INVALID", f"{where}: file not found: {full}")
    return full


def resolve_output(path, where: str, base, roots) -> Path:
    """Absolute path for a file apiprobe will write; it must sit inside one of roots (D15) and not be a folder."""
    if not isinstance(path, str) or not path:
        raise ApiProbeError("PLAN_INVALID", f"{where} must be a non-empty file path string, got {path!r}")
    p = Path(path)
    full = (p if p.is_absolute() else Path(base) / p).resolve()
    if not any(full.is_relative_to(Path(r).resolve()) for r in roots if r):
        inside = " or ".join(dict.fromkeys(str(Path(r).resolve()) for r in roots if r))
        raise ApiProbeError("PLAN_INVALID", f"{where}: '{path}' resolves outside {inside}; save under '.apiprobe/out/' in the repo")
    if full.is_dir():
        raise ApiProbeError("PLAN_INVALID", f"{where}: '{path}' is a folder; give a file name")
    return full


def check_save_to(call: dict, where: str, problems: list, ctx):
    if "saveTo" not in call:
        return
    path = call["saveTo"]
    if not isinstance(path, str) or not path:
        problems.append(f"{where}.saveTo must be a non-empty file path string")
    elif ctx and "{{" not in path:  # paths with variables are checked after substitution
        try:
            resolve_output(path, f"{where}.saveTo", *ctx)
        except ApiProbeError as e:
            problems.append(e.message)


def iter_parts(multipart: dict):
    """Yield (field, part); a list value repeats the field."""
    for name, value in multipart.items():
        for part in value if isinstance(value, list) else [value]:
            yield name, part


def _is_file_part(part) -> bool:
    return isinstance(part, dict) and "file" in part


def _check_part(part, where, problems, ctx):
    if isinstance(part, dict):
        if len(part.keys() & {"json", "file"}) != 1:
            problems.append(f"{where} must be a string, {{\"json\": ...}} or {{\"file\": path}}")
        elif "file" in part:
            for k in part.keys() - {"file", "contentType", "filename"}:
                problems.append(f"{where}.{k} is not allowed in a file part (use file, contentType, filename)")
            for k in ("contentType", "filename"):
                if k in part and not isinstance(part[k], str):
                    problems.append(f"{where}.{k} must be a string")
            _check_path(part["file"], f"{where}.file", problems, ctx)
        elif part.keys() != {"json"}:
            problems.append(f"{where}: a json part takes only the key 'json'")
    elif not isinstance(part, _SCALAR):
        problems.append(f"{where} must be a string, {{\"json\": ...}} or {{\"file\": path}}")


def _check_path(path, where, problems, ctx):
    if not isinstance(path, str) or not path:
        problems.append(f"{where} must be a non-empty file path string")
    elif ctx and "{{" not in path:  # paths with variables are checked after substitution
        try:
            resolve_file(path, where, *ctx)
        except ApiProbeError as e:
            problems.append(e.message)


def check_call_body(call: dict, where: str, problems: list, ctx):
    """ctx = (base folder, allowed roots) enables file checks; None checks shapes only."""
    keys = [k for k in BODY_KEYS if k in call]
    if len(keys) > 1:
        problems.append(f"{where} has {' and '.join(keys)}; use only one of {', '.join(BODY_KEYS)}")
        return
    if "form" in call:
        form = call["form"]
        if not isinstance(form, dict) or not all(isinstance(v, _SCALAR) or isinstance(v, list) and all(isinstance(x, _SCALAR) for x in v) for v in form.values()):
            problems.append(f"{where}.form must be a flat object whose values are strings or lists of strings")
    if "multipart" in call:
        mp = call["multipart"]
        if not isinstance(mp, dict) or not mp:
            problems.append(f"{where}.multipart must be a non-empty object of field -> part")
        else:
            for name, part in iter_parts(mp):
                _check_part(part, f"{where}.multipart.{name}", problems, ctx)
    if "text" in call and not isinstance(call["text"], str):
        problems.append(f"{where}.text must be a string")
    if "file" in call:
        _check_path(call["file"], f"{where}.file", problems, ctx)


def resolve_multipart(multipart: dict, base, roots) -> dict:
    """Copy of multipart with every file part's path made absolute and checked."""
    def one(name, part):
        if _is_file_part(part):
            return {**part, "file": str(resolve_file(part["file"], f"multipart.{name}.file", base, roots))}
        return part
    return {n: [one(n, p) for p in v] if isinstance(v, list) else one(n, v) for n, v in multipart.items()}


def _file_summary(path, content_type=None) -> str:
    p = Path(path)
    return f"{p.name} ({p.stat().st_size} bytes, {content_type or guess_type(p)})"


def summarize(req: dict) -> dict:
    """What a saved result shows for a non-JSON body: values for form/text, never file bytes."""
    if req.get("form") is not None:
        return {"form": req["form"]}
    if req.get("text") is not None:
        return {"text": req["text"]}
    if req.get("file") is not None:
        return {"file": _file_summary(req["file"])}
    if req.get("multipart") is not None:
        def one(part):
            return {"file": _file_summary(part["file"], part.get("contentType"))} if _is_file_part(part) else part
        return {"multipart": {n: [one(p) for p in v] if isinstance(v, list) else one(v) for n, v in req["multipart"].items()}}
    return {}
