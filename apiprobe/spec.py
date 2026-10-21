"""OpenAPI 3.x load, find, describe (PLAN.md section 12)."""
import json
from pathlib import Path

from . import http
from .errors import ApiProbeError

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
MAX_DEPTH = 6


def load_spec(app: dict, env: dict) -> dict:
    url = app["specUrl"]
    try:
        if url.startswith(("/", "http://", "https://")):
            # empty baseUrl lets send() take an absolute specUrl as is
            target = env if url.startswith("/") else {**env, "baseUrl": ""}
            resp = http.send(target, app.get("timeoutSeconds", 30), "GET", url)
            if not 200 <= resp["status"] < 300:
                raise ApiProbeError("SPEC_UNAVAILABLE", f"GET {resp['url']} returned {resp['status']}. Check 'specUrl' in {app['configPath']}")
            where, text = resp["url"], resp["bodyText"]
        else:
            path = Path(url)
            if not path.is_absolute() and app.get("repoPath"):
                path = Path(app["repoPath"]) / path
            where, text = str(path), path.read_text(encoding="utf-8-sig")
        spec = json.loads(text)
    except ApiProbeError as e:
        if e.code == "SPEC_UNAVAILABLE":
            raise
        raise ApiProbeError("SPEC_UNAVAILABLE", f"could not fetch spec: {e.message}")
    except OSError as e:
        raise ApiProbeError("SPEC_UNAVAILABLE", f"could not read spec file {url}: {e}. Check 'specUrl' in {app['configPath']}")
    except json.JSONDecodeError:
        raise ApiProbeError("SPEC_UNAVAILABLE", f"spec at {where} is not JSON (YAML is not supported). Check 'specUrl' in {app['configPath']}")
    if not isinstance(spec, dict):
        raise ApiProbeError("SPEC_UNAVAILABLE", f"spec at {where} is not an OpenAPI document")
    if spec.get("swagger"):
        raise ApiProbeError("SPEC_UNAVAILABLE", f"spec at {where} is Swagger {spec['swagger']}; Swagger 2.0 not supported, only OpenAPI 3.x")
    if not str(spec.get("openapi", "")).startswith("3"):
        raise ApiProbeError("SPEC_UNAVAILABLE", f"spec at {where} has no 'openapi: 3.x' field")
    return spec


def _operations(spec: dict):
    for path, item in (spec.get("paths") or {}).items():
        for method in HTTP_METHODS:
            if isinstance(item, dict) and method in item:
                yield method.upper(), path, item, item[method]


def find(spec: dict, text: str) -> list[dict]:
    words = text.lower().split()
    found = []
    for method, path, _, op in _operations(spec):
        entry = {"method": method, "path": path, "summary": op.get("summary"),
                 "operationId": op.get("operationId"), "tags": op.get("tags", [])}
        hay = " ".join([method, path, entry["summary"] or "", entry["operationId"] or "", *entry["tags"]]).lower()
        if all(w in hay for w in words):
            found.append(entry)
    return found


def _match_path(spec: dict, path: str) -> str | None:
    """Exact template match, else the template whose segments fit the concrete path with most literal matches."""
    paths = spec.get("paths") or {}
    if path in paths:
        return path
    segs = path.strip("/").split("/")
    best = None
    for template in paths:
        tsegs = template.strip("/").split("/")
        if len(tsegs) != len(segs):
            continue
        literal = 0
        for t, s in zip(tsegs, segs):
            if t.startswith("{") and t.endswith("}"):
                continue
            if t.lower() != s.lower():
                break
            literal += 1
        else:
            if best is None or literal > best[0]:
                best = (literal, template)
    return best[1] if best else None


def _resolve(spec: dict, ref: str):
    if not ref.startswith("#/"):
        return None
    node = spec
    for part in ref[2:].split("/"):
        node = node.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(node, dict) else None
    return node


def _deref(spec, obj):
    """Follow a $ref chain on a parameter/body/response object (not schemas)."""
    seen = set()
    while isinstance(obj, dict) and "$ref" in obj and obj["$ref"] not in seen:
        seen.add(obj["$ref"])
        obj = _resolve(spec, obj["$ref"]) or {}
    return obj if isinstance(obj, dict) else {}


def _merge_all_of(spec, schema, visited):
    if "allOf" not in schema:
        return schema
    merged = {k: v for k, v in schema.items() if k != "allOf"}
    props, required = dict(merged.get("properties", {})), list(merged.get("required", []))
    for part in schema["allOf"]:
        if "$ref" in part:
            if part["$ref"] in visited:
                continue
            part = _resolve(spec, part["$ref"]) or {}
        part = _merge_all_of(spec, part, visited)
        props.update(part.get("properties", {}))
        required += part.get("required", [])
        merged.setdefault("type", part.get("type"))
    return {**merged, "properties": props, "required": required} if props else merged


def _type(spec, schema) -> str:
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    for key in ("oneOf", "anyOf"):
        if key in schema:
            return " | ".join(_type(spec, s) for s in schema[key])
    t = schema.get("type")
    if isinstance(t, list):  # OpenAPI 3.1
        t = next((x for x in t if x != "null"), "null")
    if t == "array":
        return f"array<{_type(spec, schema.get('items', {}))}>"
    if not t:
        t = "object" if "properties" in schema or "allOf" in schema else "any"
    return f"{t}({schema['format']})" if schema.get("format") else t


def _walk(spec, schema, name, required, visited, depth, rows):
    ref, nullable = schema.get("$ref"), schema.get("nullable")
    all_of = schema.get("allOf", [])
    if not ref and len(all_of) == 1 and "$ref" in all_of[0]:  # Swashbuckle wraps nullable refs as allOf: [$ref]
        ref = all_of[0]["$ref"]
    if ref:
        if ref in visited:
            rows.append({"property": name, "$ref": f"{ref.split('/')[-1]} (recursive)", "required": required})
            return
        visited = visited | {ref}
        schema = {**(_resolve(spec, ref) or {}), **({"nullable": True} if nullable else {})}
    schema = _merge_all_of(spec, schema, visited)
    row = {"property": name, "type": _type(spec, schema), "required": required}
    if ref:
        row["schema"] = ref.split("/")[-1]
    if "enum" in schema:
        row["enum"] = schema["enum"]
    if schema.get("nullable") or (isinstance(schema.get("type"), list) and "null" in schema["type"]):
        row["nullable"] = True
    props = schema.get("properties")
    items = schema.get("items") if schema.get("type") == "array" else None
    if name or not props:  # a root object is shown by its properties only
        rows.append(row)
    if depth >= MAX_DEPTH:
        return
    if props:
        req = set(schema.get("required", []))
        for p, sub in props.items():
            _walk(spec, sub, f"{name}.{p}" if name else p, p in req, visited, depth + 1, rows)
    elif items:
        _walk_items(spec, items, f"{name}[]", visited, depth + 1, rows)


def _walk_items(spec, items, prefix, visited, depth, rows):
    """Array items: only expand when items are objects; primitive items are already in the parent's type."""
    target = items
    if "$ref" in items:
        if items["$ref"] in visited:
            rows.append({"property": prefix, "$ref": f"{items['$ref'].split('/')[-1]} (recursive)", "required": False})
            return
        target = _resolve(spec, items["$ref"]) or {}
        visited = visited | {items["$ref"]}
    target = _merge_all_of(spec, target, visited)
    if depth >= MAX_DEPTH or not target.get("properties"):
        return
    req = set(target.get("required", []))
    for p, sub in target["properties"].items():
        _walk(spec, sub, f"{prefix}.{p}", p in req, visited, depth + 1, rows)


def _flatten(spec, schema) -> list[dict]:
    rows = []
    _walk(spec, schema or {}, "", False, frozenset(), 0, rows)
    for r in rows:
        if r["property"] == "":
            r["property"] = "(root)"
    return rows


def _content(spec, content: dict | None):
    if not content:
        return None
    ct = "application/json" if "application/json" in content else next(iter(content))
    return {"contentType": ct, "fields": _flatten(spec, content[ct].get("schema"))}


def describe(spec: dict, method: str, path: str) -> dict:
    method = method.upper()
    template = _match_path(spec, path)
    item = (spec.get("paths") or {}).get(template) if template else None
    if not item or method.lower() not in item:
        hint = f"; {template} supports: {', '.join(m.upper() for m in HTTP_METHODS if m in item)}" if item else ""
        raise ApiProbeError("ENDPOINT_NOT_FOUND", f"{method} {path} is not in the spec{hint}. Use 'apiprobe find <words>' to search")
    op = item[method.lower()]

    params = {}
    for raw in item.get("parameters", []) + op.get("parameters", []):  # operation-level overrides path-level
        p = _deref(spec, raw)
        schema = p.get("schema", {})
        entry = {"name": p.get("name"), "in": p.get("in"), "required": bool(p.get("required")), "type": _type(spec, schema)}
        enum = (_resolve(spec, schema["$ref"]) or {}).get("enum") if "$ref" in schema else schema.get("enum")
        if enum:
            entry["enum"] = enum
        params[(entry["name"], entry["in"])] = entry

    body = None
    if "requestBody" in op:
        rb = _deref(spec, op["requestBody"])
        body = {"required": bool(rb.get("required")), **(_content(spec, rb.get("content")) or {"contentType": None, "fields": []})}

    responses = {}
    for code, raw in (op.get("responses") or {}).items():
        r = _deref(spec, raw)
        responses[code] = {"description": r.get("description"), **(_content(spec, r.get("content")) or {})}

    return {"method": method, "path": template, "summary": op.get("summary"), "operationId": op.get("operationId"),
            "tags": op.get("tags", []), "parameters": list(params.values()), "requestBody": body, "responses": responses}
