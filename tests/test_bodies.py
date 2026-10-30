import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from apiprobe import cli, config, http, runner, spec
from apiprobe.errors import ApiProbeError
from test_runner import TempHome, env_of


class BodyCase(TempHome):
    def setUp(self):
        super().setUp()
        self.repo = tempfile.TemporaryDirectory()
        self.root = Path(self.repo.name).resolve()
        (self.root / "files").mkdir()
        self.pdf = self.root / "files" / "c.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake")
        self.png = self.root / "files" / "a.png"
        self.png.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
        self.app["repoPath"] = str(self.root)
        self.env = env_of(self.app)

    def tearDown(self):
        self.repo.cleanup()
        super().tearDown()

    def plan(self, call, **step):
        return {"title": "t", "steps": [{"id": "s", "call": {"method": "POST", "as": "admin", **call}, **step}]}

    def run_plan(self, plan):
        return runner.run_plan(plan, self.app, plan_path=str(self.root / "plan.json"))


class HttpBodyTests(BodyCase):
    def test_form_repeats_list_keys(self):
        r = http.send(self.env, 1, "POST", "/form-echo", form={"a": "x y", "tag": ["p", "q"], "n": 5, "b": True})
        self.assertEqual(r["body"]["fields"], {"a": ["x y"], "tag": ["p", "q"], "n": ["5"], "b": ["true"]})
        self.assertEqual(r["body"]["contentType"], "application/x-www-form-urlencoded")

    def test_multipart_text_json_and_files(self):
        r = http.send(self.env, 1, "POST", "/upload-echo", multipart={
            "title": "Contract", "meta": {"json": {"type": "NDA"}},
            "file": {"file": str(self.pdf), "filename": "x.pdf", "contentType": "application/pdf"},
            "attachments": [{"file": str(self.png)}, {"file": str(self.pdf)}]})
        self.assertTrue(r["body"]["contentType"].startswith("multipart/form-data; boundary="))
        parts = {(p["name"], p["filename"]): p for p in r["body"]["parts"]}
        self.assertEqual(len(r["body"]["parts"]), 5)
        self.assertEqual(parts[("title", None)]["value"], "Contract")
        self.assertEqual(json.loads(parts[("meta", None)]["value"]), {"type": "NDA"})
        self.assertEqual(parts[("meta", None)]["contentType"], "application/json")
        pdf = parts[("file", "x.pdf")]
        self.assertEqual((pdf["contentType"], pdf["size"]), ("application/pdf", 13))
        self.assertEqual(pdf["sha256"], hashlib.sha256(b"%PDF-1.4 fake").hexdigest())
        png = parts[("attachments", "a.png")]  # binary bytes with CR/LF/0xff survive
        self.assertEqual((png["contentType"], png["sha256"]), ("image/png", hashlib.sha256(self.png.read_bytes()).hexdigest()))
        self.assertIn(("attachments", "c.pdf"), parts)

    def test_text_and_file_bodies(self):
        http.send(self.env, 1, "POST", "/echo", text="<a>hi</a>", headers={"content-type": "application/xml"})
        http.send(self.env, 1, "POST", "/echo", text="plain")
        http.send(self.env, 1, "PUT", "/echo", file=str(self.pdf))
        xml, plain, raw = self.server.requests[-3:]
        self.assertEqual((xml["raw"], xml["headers"]["Content-Type"]), (b"<a>hi</a>", "application/xml"))
        self.assertEqual((plain["raw"], plain["headers"]["Content-Type"]), (b"plain", "text/plain"))
        self.assertEqual((raw["raw"], raw["headers"]["Content-Type"]), (b"%PDF-1.4 fake", "application/pdf"))

    def test_multipart_content_type_cannot_be_overridden(self):
        http.send(self.env, 1, "POST", "/upload-echo", multipart={"a": "1"}, headers={"Content-Type": "text/plain"})
        self.assertTrue(self.server.requests[-1]["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))

    def test_json_body_unchanged(self):
        http.send(self.env, 1, "POST", "/echo", body={"k": 1})
        self.assertEqual(self.server.requests[-1]["headers"]["Content-Type"], "application/json")

    def test_head_and_options(self):
        self.assertEqual(http.send(self.env, 1, "HEAD", "/text")["status"], 200)
        self.assertEqual(http.send(self.env, 1, "OPTIONS", "/echo")["status"], 200)


class PlanBodyTests(BodyCase):
    def problems(self, call, files=True):
        plan = self.plan(call)
        return runner.validate_plan(plan, self.env, (self.root, [str(self.root)]) if files else None)

    def test_two_body_keys_rejected(self):
        for call in ({"path": "/echo", "body": {}, "form": {}}, {"path": "/echo", "text": "x", "file": "files/c.pdf"}):
            self.assertIn("use only one of", " ".join(self.problems(call)))

    def test_shapes_rejected(self):
        for call in ({"form": "x"}, {"form": {"a": {"b": 1}}}, {"multipart": {}}, {"multipart": {"a": {"nope": 1}}},
                     {"multipart": {"a": {"file": "files/c.pdf", "x": 1}}}, {"text": 5}, {"file": 5}):
            self.assertTrue(self.problems({"path": "/echo", **call}), call)

    def test_valid_bodies_accepted(self):
        for call in ({"form": {"a": "1", "t": ["x", "y"]}}, {"text": "x"}, {"file": "files/c.pdf"},
                     {"multipart": {"t": "x", "j": {"json": [1]}, "f": {"file": "files/c.pdf", "contentType": "a/b"}, "l": [{"file": "files/a.png"}]}}):
            self.assertEqual(self.problems({"path": "/echo", **call}), [], call)

    def test_missing_file_fails_validation_and_sends_nothing(self):
        result = self.run_plan(self.plan({"path": "/echo", "file": "files/nope.pdf"}))
        self.assertEqual((result["status"], result["error"]["code"]), ("error", "PLAN_INVALID"))
        self.assertIn("file not found", result["error"]["message"])
        self.assertEqual(self.server.requests, [])

    def test_file_outside_repo_rejected_before_sending(self):
        with tempfile.TemporaryDirectory() as other:
            secret = Path(other) / "secret.txt"
            secret.write_text("s")
            for path in (str(secret), "../" + Path(other).name + "/secret.txt", "../../../../../../../../" + str(secret).replace("\\", "/").lstrip("/")):
                result = self.run_plan(self.plan({"path": "/echo", "multipart": {"f": {"file": path}}}))
                self.assertEqual(result["error"]["code"], "PLAN_INVALID", path)
                self.assertIn("outside", result["error"]["message"])
            self.assertEqual(self.server.requests, [])

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            (Path(other) / "s.txt").write_text("s")
            link = self.root / "files" / "link.txt"
            try:
                link.symlink_to(Path(other) / "s.txt")
            except OSError:
                self.skipTest("symlinks not permitted here")
            result = self.run_plan(self.plan({"path": "/echo", "file": "files/link.txt"}))
            self.assertEqual(result["error"]["code"], "PLAN_INVALID")

    def test_file_path_from_variable_is_checked_after_substitution(self):
        plan = self.plan({"path": "/echo", "file": "{{p}}"})
        plan["steps"].insert(0, {"id": "first", "call": {"method": "GET", "path": "/echo", "as": "admin"}})
        s = runner._Session(self.app, self.env, (self.root, [str(self.root)]))
        s.variables["p"] = "../outside.txt"
        with self.assertRaises(ApiProbeError) as ctx:
            s.prepare(plan["steps"][1]["call"])
        self.assertIn("outside", ctx.exception.message)

    def test_round_trip_multipart_in_plan_with_vars_and_summary(self):
        plan = self.plan({"path": "/upload-echo", "multipart": {
            "title": "Contract {{$uuid}}", "meta": {"json": {"type": "NDA"}},
            "file": {"file": "files/c.pdf", "contentType": "application/pdf"},
            "attachments": [{"file": "files/a.png"}, {"file": "files/c.pdf"}]}},
            expect={"status": 200, "json": {"parts[0].name": "title"}})
        result = self.run_plan(plan)
        self.assertEqual(result["status"], "passed", result)
        step = result["steps"][0]
        self.assertEqual(step["request"]["multipart"]["file"], {"file": "c.pdf (13 bytes, application/pdf)"})
        self.assertEqual(step["request"]["multipart"]["attachments"][0], {"file": "a.png (10 bytes, image/png)"})
        saved = Path(result["savedTo"]).read_text(encoding="utf-8")
        self.assertNotIn("PDF-1.4", saved)

    def test_round_trip_form_text_and_file_in_plan(self):
        for call, kind in (({"path": "/form-echo", "form": {"a": ["1", "2"]}}, "form"),
                           ({"path": "/echo", "text": "<x/>", "headers": {"Content-Type": "application/xml"}}, "text"),
                           ({"path": "/echo", "file": "files/c.pdf"}, "file")):
            result = self.run_plan(self.plan(call))
            self.assertEqual(result["status"], "passed", result)
            self.assertIn(kind, result["steps"][0]["request"])
        self.assertEqual(self.server.requests[-3]["raw"], b"a=1&a=2")

    def test_v1_json_plan_still_has_same_request_shape(self):
        result = self.run_plan(self.plan({"path": "/echo", "body": {"k": 1}}))
        self.assertEqual(set(result["steps"][0]["request"]), {"method", "url", "body"})

    def test_head_in_plan(self):
        self.assertEqual(self.run_plan(self.plan({"method": "HEAD", "path": "/text"}))["status"], "passed")


class CliBodyTests(BodyCase):
    def setUp(self):
        super().setUp()
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        config.home().joinpath("apps").mkdir(parents=True)
        cfg = {"specUrl": "/openapi.json", "repoPath": str(self.root), "defaultEnvironment": "local",
               "environments": {"local": {"baseUrl": self.server.url, "users": {"admin": {"token": "tok-a"}}}}}
        config.home().joinpath("apps", "demo.json").write_text(json.dumps(cfg))

    def tearDown(self):
        os.chdir(self.old_cwd)
        super().tearDown()

    def call(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["call", *argv, "--app", "demo", "--json"])
        return code, json.loads(out.getvalue())

    def test_form(self):
        code, out = self.call("POST", "/form-echo", "--form", "a=1", "--form", "t=x", "--form", "t=y")
        self.assertEqual((code, out["body"]["fields"]), (0, {"a": ["1"], "t": ["x", "y"]}))

    def test_part_text_and_file_options(self):
        code, out = self.call("POST", "/upload-echo", "--part", "title=Hi", "--part", "file=@files/c.pdf;type=application/pdf;filename=z.pdf",
                              "--part", "att=@files/a.png", "--part", "att=@files/c.pdf")
        self.assertEqual(code, 0)
        got = [(p["name"], p["filename"], p["contentType"]) for p in out["body"]["parts"]]
        self.assertEqual(got, [("title", None, None), ("file", "z.pdf", "application/pdf"), ("att", "a.png", "image/png"), ("att", "c.pdf", "application/pdf")])

    def test_text_literal_text_file_and_raw_file(self):
        self.call("POST", "/echo", "--text", "@files/c.pdf")
        self.call("POST", "/echo", "--text", "hello")
        self.call("POST", "/echo", "--file", "@files/a.png")
        a, b, c = self.server.requests[-3:]
        self.assertEqual((a["raw"], b["raw"], c["raw"]), (b"%PDF-1.4 fake", b"hello", self.png.read_bytes()))
        self.assertEqual(c["headers"]["Content-Type"], "image/png")

    def test_json_body_flag_unchanged(self):
        code, out = self.call("POST", "/echo", "--body", '{"k": 1}')
        self.assertEqual(out["body"]["body"], {"k": 1})

    def test_body_flags_are_exclusive(self):
        code, out = self.call("POST", "/echo", "--body", "{}", "--form", "a=1")
        self.assertEqual((code, out["error"]["code"]), (2, "PLAN_INVALID"))
        self.assertIn("only one body flag", out["error"]["message"])

    def test_file_outside_repo_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            (Path(other) / "s.txt").write_text("s")
            for argv in (["--part", f"f=@{Path(other) / 's.txt'}"], ["--file", f"@{Path(other) / 's.txt'}"], ["--text", f"@{Path(other) / 's.txt'}"]):
                code, out = self.call("POST", "/echo", *argv)
                self.assertEqual((code, out["error"]["code"]), (2, "PLAN_INVALID"), argv)
        self.assertEqual(self.server.requests, [])

    def test_bad_part_option(self):
        code, out = self.call("POST", "/echo", "--part", "f=@files/c.pdf;bogus=1")
        self.assertEqual(code, 2)
        self.assertIn("type=MIME", out["error"]["message"])

    def show(self, target):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["show", target, "--app", "demo"])
        return out.getvalue()

    def test_show_multi_type_endpoint(self):
        text = self.show("POST /api/documents")
        self.assertIn("Request body (multipart/form-data, required):", text)
        self.assertIn("Request body (application/x-www-form-urlencoded, required):", text)
        self.assertIn("file  file  [required]", text)

    def test_show_same_schema_types_print_once(self):
        text = self.show("POST /api/leave")
        self.assertEqual(text.count("Request body ("), 1)
        self.assertIn("Request body (application/json, required):", text)


class SpecBodyTests(BodyCase):
    def test_describe_lists_every_content_type_with_file_fields(self):
        d = spec.describe(json.loads(Path(__file__).with_name("fixtures").joinpath("openapi.json").read_text(encoding="utf-8")), "POST", "/api/documents")
        types = {c["contentType"]: c["fields"] for c in d["requestBody"]["contentTypes"]}
        self.assertEqual(set(types), {"multipart/form-data", "application/x-www-form-urlencoded"})
        fields = {f["property"]: f["type"] for f in types["multipart/form-data"]}
        self.assertEqual(fields, {"title": "string", "file": "file", "attachments": "array<file>"})


if __name__ == "__main__":
    unittest.main()
