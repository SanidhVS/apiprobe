"""Tiny local HTTP server for tests: echo, form/upload echo, items CRUD, protected (Bearer tok-*), slow, text."""
import hashlib
import json
import threading
import time
import urllib.parse
import uuid
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "openapi.json"


def _parse_multipart(content_type: str, raw: bytes) -> list:
    msg = BytesParser(policy=default).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + raw)
    parts = []
    for part in msg.iter_parts():
        payload = part.get_payload(decode=True)
        filename = part.get_filename()
        parts.append({"name": part.get_param("name", header="content-disposition"), "filename": filename,
                      "contentType": part.get_content_type() if part["Content-Type"] else None,
                      "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                      "value": None if filename else payload.decode()})
    return parts


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body=None, text=None):
        raw = (text if text is not None else json.dumps(body) if body is not None else "").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if text is not None else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _handle(self):
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:  # form, multipart, text and file bodies
            body = None
        self.server.requests.append({"method": self.command, "path": url.path, "headers": dict(self.headers), "body": body, "raw": raw})
        auth = self.headers.get("Authorization", "")
        parts = url.path.strip("/").split("/")

        if url.path == "/echo":
            return self._reply(200, {"method": self.command, "query": urllib.parse.parse_qs(url.query),
                                     "headers": {"X-Test": self.headers.get("X-Test")}, "body": body, "auth": auth})
        if url.path == "/form-echo":
            return self._reply(200, {"contentType": self.headers.get("Content-Type"), "fields": urllib.parse.parse_qs(raw.decode())})
        if url.path == "/upload-echo":
            return self._reply(200, {"contentType": self.headers.get("Content-Type"), "parts": _parse_multipart(self.headers.get("Content-Type", ""), raw)})
        if url.path == "/protected":
            return self._reply(200, {"ok": True}) if auth.startswith("Bearer tok-") else self._reply(401, {"message": "no token"})
        if url.path == "/slow":
            time.sleep(2)
            return self._reply(200, {})
        if url.path == "/text":
            return self._reply(200, text="hello")
        if url.path == "/openapi.json":
            return self._reply(200, json.loads(FIXTURE.read_text()))
        if parts[0] == "items":
            if self.command == "POST" and len(parts) == 1:
                return self._reply(201, {"data": {"id": str(uuid.uuid4()), "name": (body or {}).get("name"), "tags": ["new"]}})
            if self.command == "GET" and len(parts) == 2:
                return self._reply(200, {"data": {"id": parts[1], "status": "Pending"}})
            if self.command == "DELETE" and len(parts) == 2:
                return self._reply(204)
        return self._reply(404, {"message": "not found"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


class StubServer:
    def __enter__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.httpd.handle_error = lambda *a: None  # client hang-ups from timeout tests
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    @property
    def requests(self):
        return self.httpd.requests

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
