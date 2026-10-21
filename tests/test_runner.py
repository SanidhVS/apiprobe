import json
import os
import tempfile
import unittest
from pathlib import Path

from apiprobe import http, runner, store
from apiprobe.errors import ApiProbeError
from stub_server import StubServer


def make_app(base_url, **users):
    users = users or {"admin": {"token": "tok-admin"}}
    return {
        "name": "demo", "configPath": "C:/home/apps/demo.json", "specUrl": "/openapi.json",
        "defaultEnvironment": "local", "timeoutSeconds": 1,
        "environments": {"local": {"baseUrl": base_url, "users": users}},
    }


def env_of(app):
    return {"name": "local", "baseUrl": app["environments"]["local"]["baseUrl"], "verifyTls": True,
            "users": app["environments"]["local"]["users"]}


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["APIPROBE_HOME"] = self.tmp.name
        self.server = StubServer().__enter__()
        self.app = make_app(self.server.url)

    def tearDown(self):
        self.server.__exit__()
        del os.environ["APIPROBE_HOME"]
        self.tmp.cleanup()


class HttpTests(TempHome):
    def test_2xx_and_4xx_are_data(self):
        env = env_of(self.app)
        ok = http.send(env, 1, "post", "/echo", query={"a": "x y", "n": 5, "b": True}, headers={"X-Test": "h"}, body={"k": 1})
        self.assertEqual(ok["status"], 200)
        self.assertEqual(ok["body"]["query"], {"a": ["x y"], "n": ["5"], "b": ["true"]})
        self.assertEqual(ok["body"]["headers"]["X-Test"], "h")
        self.assertEqual(ok["body"]["body"], {"k": 1})
        self.assertEqual(ok["body"]["method"], "POST")
        missing = http.send(env, 1, "GET", "/missing")
        self.assertEqual((missing["status"], missing["body"]), (404, {"message": "not found"}))

    def test_non_json_body(self):
        r = http.send(env_of(self.app), 1, "GET", "/text")
        self.assertEqual((r["body"], r["bodyText"]), (None, "hello"))

    def test_connection_refused(self):
        with self.assertRaises(ApiProbeError) as ctx:
            http.send({"baseUrl": "http://127.0.0.1:1"}, 1, "GET", "/x")
        self.assertEqual(ctx.exception.code, "REQUEST_FAILED")

    def test_timeout(self):
        with self.assertRaises(ApiProbeError) as ctx:
            http.send(env_of(self.app), 0.3, "GET", "/slow")
        self.assertEqual(ctx.exception.code, "REQUEST_FAILED")


PASS_PLAN = {
    "title": "Create and read item",
    "steps": [
        {"id": "create", "call": {"method": "post", "path": "/items", "as": "admin", "body": {"name": "n-{{$uuid}}"}},
         "extract": {"itemId": "data.id"},
         "expect": {"status": 201, "json": {"data.id": {"exists": True}, "data.tags": {"contains": "new"}}},
         "source": "ItemsController.cs:10"},
        {"id": "read", "call": {"method": "GET", "path": "/items/{{itemId}}", "as": "admin"},
         "expect": {"json": {"data.id": {"exists": True}, "data.status": "Pending"}}},
        {"id": "protected", "call": {"method": "GET", "path": "/protected", "as": "admin"}},
    ],
    "manualChecks": [{"sql": "SELECT * FROM Items WHERE Id = '{{itemId}}'", "expect": "1 row", "why": "persisted"}],
    "cleanup": [{"method": "DELETE", "path": "/items/{{itemId}}", "as": "admin"}],
}


class RunnerTests(TempHome):
    def run_plan(self, plan, **kw):
        events = []
        result = runner.run_plan(json.loads(json.dumps(plan)), self.app, on_event=events.append, **kw)
        return result, events

    def test_pass_flow_with_extract_and_reuse(self):
        r, events = self.run_plan(PASS_PLAN, plan_path="C:/repo/.apiprobe/plans/create-item.json")
        self.assertEqual(r["status"], "passed", json.dumps(r, indent=1))
        item_id = r["variables"]["itemId"]
        self.assertEqual(r["steps"][1]["request"]["url"], f"{self.server.url}/items/{item_id}")
        self.assertEqual(r["steps"][0]["source"], "ItemsController.cs:10")
        self.assertEqual(r["cleanup"][0]["status"], "passed")
        self.assertEqual(r["cleanup"][0]["responseStatus"], 204)
        self.assertEqual(r["manualChecks"][0]["sql"], f"SELECT * FROM Items WHERE Id = '{item_id}'")
        self.assertTrue(all(q["headers"]["Authorization"] == "Bearer tok-admin" for q in self.server.requests))
        self.assertTrue(Path(r["savedTo"]).name.endswith("-create-item.json"))
        self.assertEqual(Path(r["savedTo"]).parent, Path(self.tmp.name) / "runs" / "demo")

    def test_events_in_order(self):
        _, events = self.run_plan(PASS_PLAN)
        self.assertEqual([(e["type"], e.get("id")) for e in events], [
            ("step_started", "create"), ("step_finished", "create"),
            ("step_started", "read"), ("step_finished", "read"),
            ("step_started", "protected"), ("step_finished", "protected"),
            ("cleanup_finished", None),
        ])

    def test_failing_status_skips_rest_and_cleanup_still_runs(self):
        plan = json.loads(json.dumps(PASS_PLAN))
        plan["steps"][1]["expect"]["status"] = 418
        r, _ = self.run_plan(plan)
        self.assertEqual(r["status"], "failed")
        self.assertEqual([s["status"] for s in r["steps"]], ["passed", "failed", "skipped"])
        failed = [a for a in r["steps"][1]["assertions"] if not a["passed"]]
        self.assertEqual(failed[0]["message"], "expected 418, got 200")
        self.assertEqual(r["cleanup"][0]["status"], "passed")

    def test_cleanup_with_undefined_var_skipped(self):
        plan = json.loads(json.dumps(PASS_PLAN))
        plan["steps"][0]["expect"]["status"] = 200  # create returns 201, so step 1 fails
        plan["steps"][0]["extract"] = {"other": "data.id"}  # itemId never set
        r, _ = self.run_plan(plan)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["cleanup"][0]["status"], "skipped")
        self.assertEqual(r["cleanup"][0]["error"]["code"], "VAR_UNDEFINED")
        self.assertIn("note", r["manualChecks"][0])

    def test_undefined_var_fails_step_before_request(self):
        plan = {"title": "t", "steps": [{"id": "a", "call": {"method": "GET", "path": "/items/{{nope}}"}}]}
        r, _ = self.run_plan(plan)
        self.assertEqual(r["steps"][0]["error"]["code"], "VAR_UNDEFINED")
        self.assertEqual(self.server.requests, [])

    def test_extract_missing_fails_step(self):
        plan = {"title": "t", "steps": [{"id": "a", "call": {"method": "GET", "path": "/echo"}, "extract": {"x": "data.nope"}}]}
        r, _ = self.run_plan(plan)
        self.assertEqual(r["status"], "failed")
        self.assertIn("data.nope", r["steps"][0]["assertions"][-1]["message"])

    def test_non_json_response_fails_json_assertions(self):
        plan = {"title": "t", "steps": [{"id": "a", "call": {"method": "GET", "path": "/text"}, "expect": {"json": {"x": 1}}}]}
        r, _ = self.run_plan(plan)
        self.assertEqual(r["steps"][0]["assertions"][1]["message"], "response is not JSON")
        self.assertEqual(r["steps"][0]["response"]["body"], "hello")

    def test_request_failed_marks_step_failed(self):
        self.app["environments"]["local"]["baseUrl"] = "http://127.0.0.1:1"
        r, _ = self.run_plan({"title": "t", "steps": [{"id": "a", "call": {"method": "GET", "path": "/x"}}]})
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["steps"][0]["error"]["code"], "REQUEST_FAILED")

    def test_rejected_token_gets_hint(self):
        self.app["environments"]["local"]["users"]["admin"]["token"] = "revoked"
        plan = {"title": "t", "steps": [{"id": "a", "call": {"method": "GET", "path": "/protected", "as": "admin"}}]}
        r, _ = self.run_plan(plan)
        msg = r["steps"][0]["assertions"][0]["message"]
        self.assertIn("got 401", msg)
        self.assertIn("users.admin.token", msg)

    def test_expected_401_has_no_hint(self):
        self.app["environments"]["local"]["users"]["admin"]["token"] = "revoked"
        plan = {"title": "t", "steps": [{"id": "a", "call": {"method": "GET", "path": "/protected", "as": "admin"},
                                         "expect": {"status": 401}}]}
        r, _ = self.run_plan(plan)
        self.assertEqual(r["status"], "passed")

    def test_expired_token_stops_before_any_request(self):
        self.app["environments"]["local"]["users"]["admin"]["token"] = "x.eyJleHAiOjEwMDB9.y"  # exp 1000
        r, _ = self.run_plan(PASS_PLAN)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "TOKEN_EXPIRED")
        self.assertEqual(self.server.requests, [])

    def test_invalid_plan_lists_all_issues_and_sends_nothing(self):
        plan = {"title": "bad", "steps": [
            {"id": "a", "call": {"method": "GET", "path": "https://evil.example.com/x"}},
            {"id": "a", "call": {"method": "GET", "path": "/x", "as": "ghost"}},
            {"id": "B!", "call": {"method": "FETCH", "path": "/x"}, "expect": {"status": "200"}},
        ], "cleanup": [{"method": "DELETE", "path": "//host/x"}]}
        r, events = self.run_plan(plan)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "PLAN_INVALID")
        msg = r["error"]["message"]
        for needle in ("steps[0].call.path", "'a' is duplicated", "role 'ghost'", "steps[2].id", "steps[2].call.method",
                       "expect.status", "cleanup[0].path"):
            self.assertIn(needle, msg)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(events, [])
        self.assertTrue(Path(r["savedTo"]).is_file())

    def test_change_me_role_stops_before_any_request(self):
        self.app["environments"]["local"]["users"]["todo"] = {"token": "CHANGE_ME"}
        plan = {"title": "t", "steps": [{"id": "a", "call": {"method": "GET", "path": "/echo"}},
                                        {"id": "b", "call": {"method": "GET", "path": "/echo", "as": "todo"}}]}
        r, _ = self.run_plan(plan)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["code"], "USER_NOT_CONFIGURED")
        self.assertIn("demo.json", r["error"]["message"])
        self.assertIn("todo", r["error"]["message"])
        self.assertEqual(self.server.requests, [])

    def test_unused_change_me_role_does_not_block(self):
        self.app["environments"]["local"]["users"]["todo"] = {"token": "CHANGE_ME"}
        r, _ = self.run_plan(PASS_PLAN)
        self.assertEqual(r["status"], "passed")

    def test_token_not_in_saved_json(self):
        r, _ = self.run_plan(PASS_PLAN)
        saved = Path(r["savedTo"]).read_text()
        self.assertNotIn("tok-admin", saved)
        self.assertEqual(json.loads(saved)["status"], "passed")

    def test_unknown_env(self):
        r, _ = self.run_plan(PASS_PLAN, env_name="dev")
        self.assertEqual(r["error"]["code"], "ENV_NOT_FOUND")

    def test_list_runs(self):
        self.run_plan(PASS_PLAN)
        runs = store.list_runs("demo")
        self.assertEqual(runs[0]["status"], "passed")
        self.assertEqual(runs[0]["title"], "Create and read item")


if __name__ == "__main__":
    unittest.main()
