import json
import tempfile
import unittest
from pathlib import Path

from apiprobe import spec
from apiprobe.errors import ApiProbeError
from stub_server import FIXTURE, StubServer

SPEC = json.loads(FIXTURE.read_text())


def fields(rows):
    return {r["property"]: r for r in rows}


class LoadTests(unittest.TestCase):
    def app(self, url, repo=None):
        return {"specUrl": url, "configPath": "cfg.json", "repoPath": repo}

    def test_load_from_server_path_and_url(self):
        with StubServer() as s:
            env = {"name": "local", "baseUrl": s.url, "verifyTls": True}
            self.assertEqual(spec.load_spec(self.app("/openapi.json"), env)["openapi"], "3.0.1")
            self.assertEqual(spec.load_spec(self.app(s.url + "/openapi.json"), {**env, "baseUrl": "http://unused"})["openapi"], "3.0.1")

    def test_load_from_file_relative_to_repo(self):
        app = self.app("fixtures/openapi.json", str(FIXTURE.parent.parent))
        self.assertIn("paths", spec.load_spec(app, {}))

    def test_swagger2_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps({"swagger": "2.0", "paths": {}}))
            with self.assertRaises(ApiProbeError) as ctx:
                spec.load_spec(self.app(str(p)), {})
        self.assertEqual(ctx.exception.code, "SPEC_UNAVAILABLE")
        self.assertIn("Swagger 2.0 not supported", ctx.exception.message)

    def test_unreachable_and_404(self):
        with StubServer() as s:
            env = {"name": "local", "baseUrl": s.url}
            for url in ("/nope.json", "/text"):
                with self.subTest(url=url), self.assertRaises(ApiProbeError) as ctx:
                    spec.load_spec(self.app(url), env)
                self.assertEqual(ctx.exception.code, "SPEC_UNAVAILABLE")
        with self.assertRaises(ApiProbeError) as ctx:
            spec.load_spec(self.app("/openapi.json"), {"baseUrl": "http://127.0.0.1:1"})
        self.assertEqual(ctx.exception.code, "SPEC_UNAVAILABLE")


class FindTests(unittest.TestCase):
    def test_all_words_case_insensitive(self):
        found = spec.find(SPEC, "LEAVE approve POST")
        self.assertEqual([(f["method"], f["path"]) for f in found], [("POST", "/api/leave/{id}/approve")])

    def test_matches_method_tag_operation_id(self):
        self.assertEqual(len(spec.find(SPEC, "delete")), 1)
        self.assertEqual(len(spec.find(SPEC, "staff")), 1)
        self.assertEqual(spec.find(SPEC, "createleave")[0]["method"], "POST")

    def test_no_results(self):
        self.assertEqual(spec.find(SPEC, "payroll"), [])


class DescribeTests(unittest.TestCase):
    def test_request_body_resolved_with_enum_and_required(self):
        d = spec.describe(SPEC, "post", "/api/leave")
        rb = d["requestBody"]
        self.assertEqual(rb["contentType"], "application/json")
        self.assertTrue(rb["required"])
        f = fields(rb["fields"])
        self.assertEqual(f["from"], {"property": "from", "type": "string(date)", "required": True})
        self.assertEqual(f["type"]["enum"], [0, 1, 2])
        self.assertEqual(f["type"]["schema"], "LeaveType")
        self.assertTrue(f["note"]["nullable"])
        self.assertFalse(f["note"]["required"])

    def test_responses_flattened(self):
        d = spec.describe(SPEC, "POST", "/api/leave")
        f = fields(d["responses"]["201"]["fields"])
        self.assertEqual(f["data.id"]["type"], "string(uuid)")
        self.assertEqual(f["data.warnings"]["type"], "array<string>")
        self.assertEqual(d["responses"]["400"], {"description": "Bad Request"})

    def test_array_root_response(self):
        f = fields(spec.describe(SPEC, "GET", "/api/leave")["responses"]["200"]["fields"])
        self.assertEqual(f["(root)"]["type"], "array<Leave>")
        self.assertIn("[].status", f)

    def test_parameters_ref_and_enum(self):
        d = spec.describe(SPEC, "GET", "/api/leave/{id}")
        self.assertEqual(d["parameters"], [{"name": "id", "in": "path", "required": True, "type": "string(uuid)"}])
        p = spec.describe(SPEC, "GET", "/api/leave")["parameters"][0]
        self.assertEqual(p["enum"], ["Pending", "Approved"])

    def test_concrete_path(self):
        self.assertEqual(spec.describe(SPEC, "POST", "/api/leave/123/approve")["path"], "/api/leave/{id}/approve")
        self.assertEqual(spec.describe(SPEC, "GET", "/api/leave/abc")["path"], "/api/leave/{id}")

    def test_literal_beats_template(self):
        self.assertEqual(spec.describe(SPEC, "GET", "/api/leave/pending")["path"], "/api/leave/pending")

    def test_recursive_schema(self):
        f = fields(spec.describe(SPEC, "GET", "/api/staff/1")["responses"]["200"]["fields"])
        self.assertEqual(f["manager"]["$ref"], "Staff (recursive)")
        self.assertEqual(f["reports[]"]["$ref"], "Staff (recursive)")
        self.assertEqual(f["name"]["type"], "string")

    def test_not_found(self):
        for method, path in (("GET", "/api/nothing"), ("PUT", "/api/leave/{id}")):
            with self.subTest(path=path), self.assertRaises(ApiProbeError) as ctx:
                spec.describe(SPEC, method, path)
            self.assertEqual(ctx.exception.code, "ENDPOINT_NOT_FOUND")
        with self.assertRaises(ApiProbeError) as ctx:
            spec.describe(SPEC, "PUT", "/api/leave/{id}")
        self.assertIn("GET, DELETE", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
