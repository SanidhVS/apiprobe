import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from apiprobe import cli, config, http, runner
from apiprobe.errors import ApiProbeError
from stub_server import BINARY
from test_runner import PASS_PLAN, TempHome, env_of

SHA = hashlib.sha256(BINARY).hexdigest()


class WorkCase(TempHome):
    """A repo folder as cwd with an app config pointing at the stub server."""

    def setUp(self):
        super().setUp()
        self.old_cwd = os.getcwd()
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name).resolve()
        os.chdir(self.root)
        (config.home() / "apps").mkdir(parents=True)
        cfg = {"specUrl": "/openapi.json", "repoPath": str(self.root), "defaultEnvironment": "local",
               "environments": {"local": {"baseUrl": self.server.url, "users": {"admin": {"token": "tok-admin"}}}}}
        (config.home() / "apps" / "demo.json").write_text(json.dumps(cfg))
        self.app = config.load_app("demo")

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.work.cleanup()
        super().tearDown()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--app", "demo"])
        return code, out.getvalue()

    def jmain(self, *argv):
        code, out = self.main(*argv, "--json")
        return code, json.loads(out)

    def write_plan(self, name, plan):
        path = self.root / name
        path.write_text(json.dumps(plan))
        return str(path)


class BinaryTests(WorkCase):
    def test_binary_response_is_summarized_never_decoded(self):
        r = http.send(env_of(self.app), 1, "GET", "/binary")
        self.assertEqual(r["body"], {"binary": True, "bytes": len(BINARY), "contentType": "application/pdf", "sha256": SHA})
        self.assertEqual((r["bodyText"], r["binary"]), ("", True))

    def test_text_json_and_empty_bodies_are_not_binary(self):
        env = env_of(self.app)
        self.assertFalse(http.send(env, 1, "GET", "/text")["binary"])
        self.assertFalse(http.send(env, 1, "GET", "/echo")["binary"])
        self.assertFalse(http.send(env, 1, "HEAD", "/binary")["binary"])  # no body to summarize

    def test_save_to_writes_raw_bytes_only_on_2xx(self):
        target = self.root / "out" / "r.pdf"
        r = http.send(env_of(self.app), 1, "GET", "/binary", save_to=str(target))
        self.assertEqual(target.read_bytes(), BINARY)
        self.assertEqual(r["saved"], {"path": str(target), "bytes": len(BINARY)})
        missing = self.root / "out" / "nope.bin"
        self.assertIsNone(http.send(env_of(self.app), 1, "GET", "/missing", save_to=str(missing))["saved"])
        self.assertFalse(missing.exists())

    def run_step(self, call, **step):
        plan = {"title": "t", "steps": [{"id": "s", "call": {"method": "GET", "as": "admin", **call}, **step}]}
        return runner.run_plan(plan, self.app, plan_path=str(self.root / "plan.json"))

    def test_plan_asserts_on_binary_summary_and_saves_file(self):
        result = self.run_step({"path": "/binary", "saveTo": "out/{{$uuid}}.pdf"},
                               expect={"status": 200, "json": {"bytes": len(BINARY), "contentType": "application/pdf", "sha256": SHA}})
        self.assertEqual(result["status"], "passed", result)
        saved = result["steps"][0]["response"]["saved"]
        self.assertEqual(Path(saved["path"]).read_bytes(), BINARY)
        self.assertNotIn("PDF-1.4", Path(result["savedTo"]).read_text(encoding="utf-8"))

    def test_save_to_outside_repo_rejected_before_sending(self):
        with tempfile.TemporaryDirectory() as other:
            for path in (str(Path(other) / "x.bin"), "../" + Path(other).name + "/x.bin", "../../../../../../../../tmp/x.bin"):
                result = self.run_step({"path": "/binary", "saveTo": path})
                self.assertEqual(result["error"]["code"], "PLAN_INVALID", path)
                self.assertIn("outside", result["error"]["message"])
            self.assertEqual(self.server.requests, [])
            self.assertEqual(list(Path(other).iterdir()), [])

    def test_save_to_bad_values(self):
        (self.root / "adir").mkdir()
        for bad in (5, "", "adir"):
            result = self.run_step({"path": "/binary", "saveTo": bad})
            self.assertEqual(result["error"]["code"], "PLAN_INVALID", bad)
        self.assertEqual(self.server.requests, [])

    def test_save_to_from_variable_is_checked_after_substitution(self):
        s = runner._Session(self.app, env_of(self.app), (self.root, [str(self.root)]))
        s.variables["p"] = "../escape.bin"
        with self.assertRaises(ApiProbeError) as ctx:
            s.prepare({"method": "GET", "path": "/binary", "saveTo": "{{p}}"})
        self.assertIn("outside", ctx.exception.message)


class CallOutputTests(WorkCase):
    def test_brief_json_drops_body_text_and_noise_headers(self):
        code, out = self.jmain("call", "GET", "/binary")
        self.assertEqual(code, 0)
        self.assertEqual(set(out), {"status", "headers", "body", "durationMs", "url"})
        self.assertEqual(out["headers"], {"content-type": "application/pdf", "content-length": str(len(BINARY)),
                                          "content-disposition": 'attachment; filename="r.pdf"', "etag": '"v1"'})
        _, out = self.jmain("call", "POST", "/echo", "--body", "{}")
        self.assertNotIn("bodyText", out)

    def test_non_json_body_keeps_body_text(self):
        _, out = self.jmain("call", "GET", "/text")
        self.assertEqual((out["body"], out["bodyText"]), (None, "hello"))

    def test_set_cookie_shows_names_only(self):
        _, out = self.jmain("call", "GET", "/cookies")
        self.assertEqual(out["headers"]["set-cookie"], ["session", "XSRF-TOKEN"])
        self.assertNotIn("SESSIONVALUE", json.dumps(out))

    def test_header_out_adds_named_headers_case_insensitively(self):
        _, out = self.jmain("call", "GET", "/binary", "--header-out", "x-secret")
        self.assertEqual(out["headers"]["x-secret"], "hidden")

    def test_full_keeps_the_v1_shape(self):
        _, out = self.jmain("call", "GET", "/binary", "--full")
        self.assertTrue({"status", "headers", "body", "bodyText", "durationMs", "url"} <= set(out))
        self.assertEqual(out["headers"]["X-Secret"], "hidden")

    def test_pick(self):
        _, out = self.jmain("call", "POST", "/echo", "--body", '{"a": {"b": [10, 20]}}', "--pick", "body.a.b[1]")
        self.assertEqual((out["found"], out["value"], out["pick"]), (True, 20, "body.a.b[1]"))
        self.assertNotIn("headers", out)
        _, out = self.jmain("call", "POST", "/echo", "--body", "{}", "--pick", "nope")
        self.assertEqual(out["found"], False)
        self.assertNotIn("value", out)
        _, human = self.main("call", "POST", "/echo", "--body", '{"a": 5}', "--pick", "body.a")
        self.assertTrue(human.rstrip().endswith("5"))
        _, human = self.main("call", "POST", "/echo", "--body", "{}", "--pick", "nope")
        self.assertIn("not found", human)

    def test_max_chars_cuts_human_body(self):
        _, human = self.main("call", "POST", "/echo", "--body", json.dumps({"k": "x" * 400}), "--max-chars", "50")
        self.assertIn("more chars, see saved run", human)
        _, human = self.main("call", "POST", "/echo", "--body", json.dumps({"k": "x" * 400}))
        self.assertNotIn("more chars", human)  # default limit 1500

    def test_binary_human_output(self):
        _, human = self.main("call", "GET", "/binary")
        self.assertIn(SHA, human)


class RunOutputTests(WorkCase):
    def five_step_plan(self):
        return {"title": "Five", "steps": [{"id": f"s{i}", "call": {"method": "GET", "path": "/echo", "as": "admin"}} for i in range(5)]}

    def test_passing_five_step_plan_is_under_1kb(self):
        path = self.write_plan("five.json", self.five_step_plan())
        code, out = self.main("run", path, "--json")
        self.assertEqual(code, 0)
        self.assertLess(len(out.encode()), 1024, out)
        brief = json.loads(out)
        self.assertEqual([set(s) for s in brief["steps"]], [{"id", "status", "durationMs"}] * 5)

    def test_full_is_the_v1_shape_and_saved_run_is_always_full(self):
        path = self.write_plan("five.json", self.five_step_plan())
        _, full = self.jmain("run", path, "--full")
        self.assertTrue({"request", "response", "assertions", "extracted", "source"} <= set(full["steps"][0]))
        self.assertTrue({"planPath", "startedAt", "cleanup", "manualChecks", "variables"} <= set(full))
        _, brief = self.jmain("run", path)
        saved = json.loads(Path(brief["savedTo"]).read_text(encoding="utf-8"))
        self.assertIn("body", saved["steps"][0]["response"])
        self.assertIn("assertions", saved["steps"][0])

    def test_failed_step_carries_diagnosis_with_clipped_body(self):
        plan = {"title": "Fails", "steps": [
            {"id": "ok", "call": {"method": "GET", "path": "/echo"}},
            {"id": "bad", "call": {"method": "POST", "path": "/echo", "body": {"big": "y" * 300}}, "source": "X.cs:1",
             "expect": {"status": 404, "json": {"body.big": "nope", "method": "POST"}}},
            {"id": "later", "call": {"method": "GET", "path": "/echo"}}]}
        code, out = self.jmain("run", self.write_plan("fails.json", plan), "--max-chars", "80")
        self.assertEqual(code, 1)
        ok, bad, later = out["steps"]
        self.assertEqual(set(ok), {"id", "status", "durationMs"})
        self.assertEqual(later, {"id": "later", "status": "skipped"})
        self.assertEqual({a["target"] for a in bad["assertions"]}, {"status", "body.big"})  # the passed `method` check is left out
        self.assertEqual(bad["source"], "X.cs:1")
        self.assertEqual(bad["response"]["status"], 200)
        self.assertIn("more chars", bad["response"]["body"])
        self.assertIn("more chars", bad["request"]["body"])

    def test_brief_keeps_cleanup_manual_checks_and_variables(self):
        _, out = self.jmain("run", self.write_plan("p.json", PASS_PLAN))
        self.assertEqual(out["status"], "passed")
        self.assertIn("itemId", out["variables"])
        self.assertIn(out["variables"]["itemId"], out["manualChecks"][0]["sql"])
        self.assertEqual(out["cleanup"][0]["status"], "passed")

    def test_preflight_error_is_still_reported(self):
        bad = {"title": "t", "steps": [{"id": "s", "call": {"method": "GET", "path": "/echo", "as": "ghost"}}]}
        code, out = self.jmain("run", self.write_plan("bad.json", bad))
        self.assertEqual((code, out["status"], out["steps"]), (2, "error", []))
        self.assertNotIn("planPath", out)  # compact like any other run
        _, full = self.jmain("run", self.write_plan("bad.json", bad), "--full")
        self.assertIn("planPath", full)
        self.assertIn("ghost", out["error"]["message"])

    def test_human_run_output_unchanged_by_default(self):
        code, human = self.main("run", self.write_plan("p.json", PASS_PLAN))
        self.assertIn("PASSED  3 passed", human)


class LastAndRunsTests(WorkCase):
    def setUp(self):
        super().setUp()
        good = {"title": "Good", "steps": [{"id": "create", "call": {"method": "POST", "path": "/echo", "body": {"a": {"b": 7}}}}]}
        bad = {"title": "Bad", "steps": [{"id": "boom", "call": {"method": "GET", "path": "/missing"}}]}
        self.main("run", self.write_plan("good-plan.json", good), "--json")
        self.main("run", self.write_plan("bad-plan.json", bad), "--json")
        self.main("run", self.write_plan("good-plan.json", good), "--json")  # same second: the newest must still win

    def test_last_is_the_newest_run_and_brief(self):
        _, out = self.jmain("last")
        self.assertEqual((out["title"], out["status"]), ("Good", "passed"))
        self.assertNotIn("request", out["steps"][0])
        self.assertTrue(out["savedTo"].endswith("-2.json"))

    def test_last_plan_filter(self):
        _, out = self.jmain("last", "--plan", "bad-plan")
        self.assertEqual(out["title"], "Bad")
        _, out = self.jmain("last", "--plan", "bad-plan.json")
        self.assertEqual(out["title"], "Bad")
        code, out = self.jmain("last", "--plan", "nope")
        self.assertEqual((code, out["error"]["code"]), (2, "RUN_NOT_FOUND"))

    def test_last_step_is_full_detail(self):
        _, step = self.jmain("last", "--step", "create")
        self.assertEqual(step["response"]["body"]["body"], {"a": {"b": 7}})
        self.assertTrue({"request", "response", "assertions", "extracted"} <= set(step))

    def test_last_step_pick(self):
        _, out = self.jmain("last", "--step", "create", "--pick", "body.a.b")
        self.assertEqual((out["step"], out["found"], out["value"]), ("create", True, 7))
        _, human = self.main("last", "--step", "create", "--pick", "body.a.b")
        self.assertEqual(human.strip(), "7")
        _, out = self.jmain("last", "--step", "create", "--pick", "zzz")
        self.assertFalse(out["found"])

    def test_last_errors(self):
        code, out = self.jmain("last", "--step", "ghost")
        self.assertEqual((code, out["error"]["code"]), (2, "STEP_NOT_FOUND"))
        self.assertIn("create", out["error"]["message"])
        code, out = self.jmain("last", "--pick", "x")
        self.assertEqual((code, out["error"]["code"]), (2, "PLAN_INVALID"))

    def test_last_full_returns_the_saved_run(self):
        _, out = self.jmain("last", "--full")
        self.assertIn("planPath", out)

    def test_last_with_no_runs(self):
        for p in (config.home() / "runs" / "demo").glob("*.json"):
            p.unlink()
        code, out = self.jmain("last")
        self.assertEqual((code, out["error"]["code"]), (2, "RUN_NOT_FOUND"))

    def test_runs_lists_newest_first_with_limit(self):
        _, runs = self.jmain("runs")
        self.assertEqual([r["title"] for r in runs], ["Good", "Bad", "Good"])
        _, runs = self.jmain("runs", "--limit", "1")
        self.assertEqual(len(runs), 1)
        _, human = self.main("runs")
        self.assertEqual(len(human.strip().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
