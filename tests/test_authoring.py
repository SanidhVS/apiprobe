import contextlib
import copy
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from apiprobe import cli, config, runner, spec
from apiprobe.scaffold import check_plan, scaffold
from test_runner import PASS_PLAN, TempHome, env_of

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "openapi.json").read_text(encoding="utf-8"))


class ScaffoldTests(unittest.TestCase):
    def test_json_body_uses_required_fields_and_enum_values(self):
        step = scaffold(FIXTURE, "POST", "/api/leave", "staff")
        self.assertEqual(step, {"id": "create_leave", "expect": {"status": 201}, "call": {
            "method": "POST", "path": "/api/leave", "as": "staff", "body": {"from": "{{$uuid}}", "to": "{{$uuid}}", "type": 0}}})
        self.assertNotIn("note", step["call"]["body"])  # optional

    def test_multipart_has_file_placeholder_and_skips_optional_parts(self):
        call = scaffold(FIXTURE, "POST", "/api/documents")["call"]
        self.assertEqual(call["multipart"], {"title": "{{$uuid}}", "file": {"file": "files/CHANGE_ME"}})
        self.assertNotIn("as", call)

    def test_path_params_become_variables_and_concrete_paths_match_the_template(self):
        step = scaffold(FIXTURE, "get", "/api/leave/123")
        self.assertEqual(step["call"]["path"], "/api/leave/{{id}}")
        self.assertEqual(step["expect"], {"status": 200})
        self.assertEqual(scaffold(FIXTURE, "DELETE", "/api/leave/{id}")["expect"], {"status": 204})

    def test_example_default_enum_then_typed_placeholder(self):
        s = copy.deepcopy(FIXTURE)
        s["paths"]["/t"] = {"post": {"operationId": "DoThing", "parameters": [
            {"name": "page", "in": "query", "required": True, "schema": {"type": "integer"}},
            {"name": "sort", "in": "query", "required": True, "schema": {"type": "string", "enum": ["asc", "desc"]}},
            {"name": "skip", "in": "query", "schema": {"type": "string"}}],
            "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["a", "b", "c", "d", "e", "f", "g", "h"], "properties": {
                "a": {"type": "string", "example": "ex"}, "b": {"type": "string", "default": "df"}, "c": {"type": "string", "enum": ["x", "y"]},
                "d": {"type": "string"}, "e": {"type": "number"}, "f": {"type": "boolean"}, "g": {"type": "array", "items": {"type": "string"}},
                "h": {"type": "object", "required": ["k"], "properties": {"k": {"type": "integer"}, "z": {"type": "string"}}}}}}}},
            "responses": {"default": {"description": "x"}, "202": {"description": "Accepted"}}}}
        step = scaffold(s, "POST", "/t")
        self.assertEqual(step["call"]["query"], {"page": 0, "sort": "asc"})
        self.assertEqual(step["call"]["body"], {"a": "ex", "b": "df", "c": "x", "d": "{{$uuid}}", "e": 0, "f": False, "g": [], "h": {"k": 0}})
        self.assertEqual((step["id"], step["expect"]), ("do_thing", {"status": 202}))

    def test_form_text_and_raw_file_bodies_and_recursive_schema(self):
        s = copy.deepcopy(FIXTURE)
        s["components"]["schemas"]["Node"] = {"type": "object", "required": ["name", "next"], "properties": {
            "name": {"type": "string"}, "next": {"$ref": "#/components/schemas/Node"}}}
        s["paths"]["/f"] = {"post": {"requestBody": {"content": {"application/x-www-form-urlencoded": {"schema": {"type": "object", "required": ["q"], "properties": {"q": {"type": "string"}}}}}}, "responses": {"200": {"description": "ok"}}}}
        s["paths"]["/x"] = {"post": {"requestBody": {"content": {"application/xml": {"schema": {"type": "string"}}}}, "responses": {"200": {"description": "ok"}}}}
        s["paths"]["/r"] = {"post": {"requestBody": {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}, "responses": {"200": {"description": "ok"}}}}
        s["paths"]["/n"] = {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}, "responses": {"200": {"description": "ok"}}}}
        self.assertEqual(scaffold(s, "POST", "/f")["call"]["form"], {"q": "{{$uuid}}"})
        self.assertEqual(scaffold(s, "POST", "/x")["call"]["text"], "CHANGE_ME")
        self.assertEqual(scaffold(s, "POST", "/r")["call"]["file"], "files/CHANGE_ME")
        self.assertEqual(scaffold(s, "POST", "/n")["call"]["body"], {"name": "{{$uuid}}", "next": {}})

    def test_unknown_endpoint(self):
        with self.assertRaises(runner.ApiProbeError) as ctx:
            scaffold(FIXTURE, "POST", "/nope")
        self.assertEqual(ctx.exception.code, "ENDPOINT_NOT_FOUND")


class CheckTests(TempHome):
    def setUp(self):
        super().setUp()
        self.base = Path(self.tmp.name)
        (self.base / "files").mkdir()
        (self.base / "files" / "c.pdf").write_bytes(b"x")
        self.env = env_of(self.app)

    def check(self, *calls, doc=FIXTURE, **plan):
        plan = {"title": "t", "steps": [{"id": f"s{i}", "call": {"as": "admin", **c}} for i, c in enumerate(calls)], **plan}
        return check_plan(doc, plan, self.env, (self.base, [str(self.base)]))

    def test_scaffolded_steps_pass_check_unedited(self):
        for method, path in (("POST", "/api/leave"), ("GET", "/api/leave/{id}"), ("GET", "/api/leave"), ("POST", "/api/documents"),
                             ("POST", "/api/leave/{id}/approve"), ("DELETE", "/api/leave/{id}")):
            call = scaffold(FIXTURE, method, path, "admin")["call"]
            if "multipart" in call:
                call["multipart"]["file"] = {"file": "files/c.pdf"}  # the one edit scaffold asks for
            problems, warnings = self.check(call)
            self.assertEqual((problems, warnings), ([], []), f"{method} {path}")

    def test_missing_required_body_field(self):
        problems, _ = self.check({"method": "POST", "path": "/api/leave", "body": {"from": "a", "type": 0}})
        self.assertEqual(problems, ["steps[0].call.body: required field 'to' is missing"])

    def test_unknown_body_field_is_only_a_warning(self):
        problems, warnings = self.check({"method": "POST", "path": "/api/leave", "body": {"from": "a", "to": "b", "type": 0, "bogus": 1}})
        self.assertEqual(problems, [])
        self.assertEqual(warnings, ["steps[0].call.body: field 'bogus' is not in the spec"])

    def test_wrong_method_unknown_path_and_cleanup_are_reported_with_location(self):
        problems, _ = self.check({"method": "PUT", "path": "/api/leave"}, {"method": "GET", "path": "/api/nope"},
                                 cleanup=[{"method": "GET", "path": "/gone"}])
        self.assertEqual([p.split(":")[0] for p in problems], ["steps[0].call", "steps[1].call", "cleanup[0]"])
        self.assertIn("supports: GET, POST", problems[0])

    def test_missing_required_query_param(self):
        doc = copy.deepcopy(FIXTURE)
        doc["paths"]["/api/leave"]["get"]["parameters"] = [{"name": "status", "in": "query", "required": True, "schema": {"type": "string"}}]
        self.assertEqual(self.check({"method": "GET", "path": "/api/leave"}, doc=doc)[0],
                         ["steps[0].call.query: required query parameter 'status' is missing"])
        self.assertEqual(self.check({"method": "GET", "path": "/api/leave", "query": {"status": "x"}}, doc=doc)[0], [])

    def test_body_key_must_match_an_accepted_content_type(self):
        problems, _ = self.check({"method": "POST", "path": "/api/documents", "body": {"title": "x"}})
        self.assertIn("steps[0].call.body: the endpoint accepts multipart/form-data (multipart), application/x-www-form-urlencoded (form)", problems[0])
        self.assertEqual(self.check({"method": "POST", "path": "/api/documents", "form": {"title": "x"}})[0], [])

    def test_body_on_endpoint_without_one_and_missing_required_body(self):
        self.assertIn("takes no request body", self.check({"method": "GET", "path": "/api/leave", "body": {}})[0][0])
        self.assertIn("request body is required", self.check({"method": "POST", "path": "/api/leave"})[0][0])

    def test_upload_file_must_exist_and_stay_inside(self):
        problems, _ = self.check({"method": "POST", "path": "/api/documents", "multipart": {"title": "t", "file": {"file": "files/nope.pdf"}}})
        self.assertIn("file not found", problems[0])
        problems, _ = self.check({"method": "POST", "path": "/api/documents", "multipart": {"title": "t", "file": {"file": "../x.pdf"}}})
        self.assertIn("outside", problems[0])

    def test_spec_and_file_problems_are_reported_together(self):
        problems, _ = self.check({"method": "POST", "path": "/api/leave", "body": {"from": "a"}},
                                 {"method": "POST", "path": "/api/documents", "multipart": {"title": "t", "file": {"file": "files/none.pdf"}}})
        self.assertEqual(len(problems), 3)
        self.assertEqual([p.split(":")[0].split(".")[0] for p in problems], ["steps[0]", "steps[0]", "steps[1]"])

    def test_variable_paths_and_vars_are_not_flagged(self):
        problems, warnings = self.check({"method": "GET", "path": "/api/leave/{{id}}"}, {"method": "GET", "path": "{{loc}}"})
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1)

    def test_structural_plan_problems_come_first_and_stop(self):
        problems, _ = check_plan(FIXTURE, {"title": "t", "steps": [{"id": "BAD", "call": {"method": "GET", "path": "/api/leave"}}]}, self.env, None)
        self.assertEqual(len(problems), 1)
        self.assertIn("steps[0].id", problems[0])
        self.assertEqual(check_plan(FIXTURE, [], self.env, None)[0], ["plan must be a JSON object"])


class CliCase(TempHome):
    def setUp(self):
        super().setUp()
        self.old_cwd = os.getcwd()
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name).resolve()
        os.chdir(self.root)
        (config.home() / "apps").mkdir(parents=True)
        self.write_config()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.work.cleanup()
        super().tearDown()

    def write_config(self, spec_url="/openapi.json"):
        cfg = {"specUrl": spec_url, "repoPath": str(self.root), "defaultEnvironment": "local",
               "environments": {"local": {"baseUrl": self.server.url, "users": {"admin": {"token": "tok-admin"}}}}}
        (config.home() / "apps" / "demo.json").write_text(json.dumps(cfg))

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--app", "demo"])
        return code, out.getvalue()

    def jmain(self, *argv):
        code, out = self.main(*argv, "--json")
        return code, json.loads(out)

    def spec_fetches(self):
        return sum(r["path"] == "/openapi.json" for r in self.server.requests)

    def write_plan(self, name, plan):
        path = self.root / name
        path.write_text(json.dumps(plan))
        return str(path)


class SpecCacheTests(CliCase):
    def test_second_command_reuses_the_cached_spec_and_refresh_bypasses(self):
        self.main("show", "GET /api/leave")
        self.main("find", "leave")
        self.main("scaffold", "GET /api/leave")
        self.assertEqual(self.spec_fetches(), 1)
        self.main("show", "GET /api/leave", "--refresh")
        self.assertEqual(self.spec_fetches(), 2)

    def test_cache_expires_after_ten_minutes(self):
        self.main("find", "leave")
        path = next((config.home() / "cache").glob("*.json"))
        entry = json.loads(path.read_text())
        entry["fetchedAt"] = time.time() - 601
        path.write_text(json.dumps(entry))
        self.main("find", "leave")
        self.assertEqual(self.spec_fetches(), 2)

    def test_cache_is_ignored_when_base_url_or_spec_url_changed(self):
        self.main("find", "leave")
        path = next((config.home() / "cache").glob("*.json"))
        entry = json.loads(path.read_text())
        entry["baseUrl"] = "http://other"
        path.write_text(json.dumps(entry))
        self.main("find", "leave")
        self.assertEqual(self.spec_fetches(), 2)

    def tamper(self, drop):
        path = next((config.home() / "cache").glob("*.json"))
        entry = json.loads(path.read_text())
        del entry["spec"]["paths"][drop]
        path.write_text(json.dumps(entry))

    def test_endpoint_missing_from_a_cached_spec_triggers_one_refetch(self):
        self.main("find", "leave")
        self.tamper("/api/documents")  # the cache is "stale": the rebuilt API has this endpoint
        code, out = self.main("show", "POST /api/documents")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.spec_fetches(), 2)
        code, out = self.jmain("show", "GET /api/really-not-there")
        self.assertEqual(out["error"]["code"], "ENDPOINT_NOT_FOUND")
        self.assertEqual(self.spec_fetches(), 3)  # one refetch, not a loop
        self.main("show", "GET /api/leave")
        self.assertEqual(self.spec_fetches(), 3)  # and the cache is fresh again

    def test_find_with_no_hits_on_a_cached_spec_refetches_once(self):
        self.main("find", "leave")
        self.tamper("/api/documents")
        _, found = self.jmain("find", "documents")
        self.assertEqual(len(found), 1)
        self.assertEqual(self.spec_fetches(), 2)

    def test_spec_file_in_the_repo_is_never_cached(self):
        (self.root / "spec.json").write_text(json.dumps(FIXTURE))
        self.write_config("spec.json")
        self.main("find", "leave")
        changed = copy.deepcopy(FIXTURE)
        changed["paths"] = {"/only": FIXTURE["paths"]["/api/leave"]}
        (self.root / "spec.json").write_text(json.dumps(changed))
        _, found = self.jmain("find", "only")
        self.assertEqual({e["path"] for e in found}, {"/only"})
        self.assertFalse((config.home() / "cache").exists())

    def test_run_never_fetches_the_spec(self):
        self.main("run", self.write_plan("p.json", PASS_PLAN))
        self.assertEqual(self.spec_fetches(), 0)

    def test_check_uses_and_refreshes_the_cache(self):
        plan = self.write_plan("p.json", {"title": "t", "steps": [{"id": "up", "call": {"method": "POST", "path": "/api/documents", "form": {"title": "x"}}}]})
        self.main("find", "leave")
        self.tamper("/api/documents")
        code, result = self.jmain("check", plan)
        self.assertEqual((code, result["problems"]), (0, []))
        self.assertEqual(self.spec_fetches(), 2)


class CliAuthoringTests(CliCase):
    def test_scaffold_json_is_just_the_step(self):
        code, step = self.jmain("scaffold", "POST /api/leave", "--as", "admin")
        self.assertEqual((code, step["id"], step["call"]["as"]), (0, "create_leave", "admin"))
        self.assertEqual(set(step), {"id", "call", "expect"})
        _, human = self.main("scaffold", "GET /api/leave/{id}")
        self.assertEqual(json.loads(human)["call"]["path"], "/api/leave/{{id}}")

    def test_scaffold_needs_method_and_path(self):
        code, out = self.jmain("scaffold", "/api/leave")
        self.assertEqual((code, out["error"]["code"]), (2, "ENDPOINT_NOT_FOUND"))
        self.assertIn("scaffold expects", out["error"]["message"])

    def test_check_exit_codes_and_output(self):
        good = self.write_plan("good.json", {"title": "t", "steps": [{"id": "g", "call": {"method": "GET", "path": "/api/leave", "as": "admin"}}]})
        code, human = self.main("check", good)
        self.assertEqual(code, 0)
        self.assertIn("OK  1 steps", human)
        bad = self.write_plan("bad.json", {"title": "t", "steps": [{"id": "g", "call": {"method": "POST", "path": "/api/leave", "as": "admin", "body": {"from": "x"}}}]})
        code, human = self.main("check", bad)
        self.assertEqual(code, 1)
        self.assertIn("steps[0].call.body: required field 'to' is missing", human)
        self.assertIn("required field 'type'", human)
        code, out = self.jmain("check", bad)
        self.assertEqual((out["ok"], len(out["problems"])), (False, 2))
        code, out = self.jmain("check", str(self.root / "missing.json"))
        self.assertEqual((code, out["error"]["code"]), (2, "PLAN_INVALID"))

    def test_check_sends_nothing_to_the_api(self):
        plan = self.write_plan("p.json", {"title": "t", "steps": [{"id": "g", "call": {"method": "POST", "path": "/items", "as": "admin"}}]})
        self.main("check", plan)
        self.assertEqual([r["path"] for r in self.server.requests], ["/openapi.json"])

    def test_check_flags_unknown_roles_and_missing_upload_files(self):
        call = {"method": "POST", "path": "/api/documents", "as": "ghost", "multipart": {"title": "t", "file": {"file": "files/none.pdf"}}}
        code, out = self.jmain("check", self.write_plan("p.json", {"title": "t", "steps": [{"id": "g", "call": call}]}))
        self.assertEqual(code, 1)
        self.assertIn("ghost", " ".join(out["problems"]))  # structure problems come first
        call["as"] = "admin"
        code, out = self.jmain("check", self.write_plan("p.json", {"title": "t", "steps": [{"id": "g", "call": call}]}))
        self.assertEqual(code, 1)
        self.assertIn("none.pdf", " ".join(out["problems"]))

    def test_check_picks_the_plans_environment(self):
        plan = self.write_plan("p.json", {"title": "t", "environment": "nope", "steps": [{"id": "g", "call": {"method": "GET", "path": "/api/leave"}}]})
        code, out = self.jmain("check", plan)
        self.assertEqual((code, out["error"]["code"]), (2, "ENV_NOT_FOUND"))


class VarsOnlySuiteTests(CliCase):
    def plan(self, title, path="/echo", **extra):
        return {"title": title, "steps": [{"id": "s", "call": {"method": "GET", "path": path, "as": "admin"}}], **extra}

    def test_plan_vars_seed_the_variable_map_and_extract_overwrites(self):
        plan = {"title": "t", "vars": {"depotId": 12, "name": "n"}, "steps": [
            {"id": "a", "call": {"method": "POST", "path": "/echo", "as": "admin", "body": {"depot": "{{depotId}}", "name": "{{name}}"}},
             "expect": {"json": {"body.depot": 12, "body.name": "n"}}, "extract": {"depotId": "body.depot"}},
            {"id": "b", "call": {"method": "POST", "path": "/echo", "body": {"d": "{{depotId}}"}}}],
            "manualChecks": [{"sql": "SELECT {{depotId}}"}]}
        result = runner.run_plan(plan, config.load_app("demo"))
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["manualChecks"][0]["sql"], "SELECT 12")
        # an unseeded variable is still an error
        plan["steps"][0]["call"]["body"]["x"] = "{{nope}}"
        self.assertEqual(runner.run_plan(plan, config.load_app("demo"))["steps"][0]["error"]["code"], "VAR_UNDEFINED")

    def test_vars_validation(self):
        env = env_of(self.app)
        base = {"title": "t", "steps": [{"id": "s", "call": {"method": "GET", "path": "/x"}}]}
        self.assertEqual(runner.validate_plan({**base, "vars": {"a_1": [1, 2]}}, env), [])
        for bad in ([], "x", {"bad key": 1}, {"$uuid": 1}):
            self.assertTrue(any("vars must be" in p for p in runner.validate_plan({**base, "vars": bad}, env)), bad)

    def test_only_runs_up_to_and_including_the_step_and_cleanup_still_runs(self):
        plan = {"title": "t", "steps": [
            {"id": "one", "call": {"method": "POST", "path": "/items", "as": "admin", "body": {"name": "n"}}, "extract": {"itemId": "data.id"}},
            {"id": "two", "call": {"method": "GET", "path": "/items/{{itemId}}", "as": "admin"}},
            {"id": "three", "call": {"method": "GET", "path": "/never", "as": "admin"}}],
            "cleanup": [{"method": "DELETE", "path": "/items/{{itemId}}", "as": "admin"}]}
        result = runner.run_plan(plan, config.load_app("demo"), only="two")
        self.assertEqual([(s["id"], s["status"]) for s in result["steps"]], [("one", "passed"), ("two", "passed")])
        self.assertEqual(result["cleanup"][0]["status"], "passed")
        self.assertNotIn("/never", [r["path"] for r in self.server.requests])

    def test_only_with_unknown_step_is_a_plan_error_and_sends_nothing(self):
        result = runner.run_plan(self.plan("t"), config.load_app("demo"), only="zzz")
        self.assertEqual((result["status"], result["error"]["code"]), ("error", "PLAN_INVALID"))
        self.assertIn("steps: s", result["error"]["message"])
        self.assertEqual(self.server.requests, [])

    def test_only_via_cli(self):
        path = self.write_plan("p.json", {"title": "t", "steps": [
            {"id": "a", "call": {"method": "GET", "path": "/echo"}}, {"id": "b", "call": {"method": "GET", "path": "/missing"}}]})
        code, out = self.jmain("run", path, "--only", "a")
        self.assertEqual((code, [s["id"] for s in out["steps"]]), (0, ["a"]))

    def test_single_plan_run_keeps_its_shape_and_saves_one_run(self):
        path = self.write_plan("one.json", self.plan("One"))
        code, out = self.jmain("run", path)
        self.assertEqual((code, out["title"], out["status"]), (0, "One", "passed"))
        self.assertNotIn("plans", out)
        self.assertEqual(len(list((config.home() / "runs" / "demo").glob("*.json"))), 1)

    def test_several_plans_give_a_suite_with_one_saved_run_each(self):
        a = self.write_plan("a.json", self.plan("A"))
        b = self.write_plan("b.json", self.plan("B", "/missing"))
        code, out = self.jmain("run", a, b)
        self.assertEqual(code, 1)
        self.assertEqual((out["status"], out["counts"]), ("failed", {"passed": 1, "failed": 1, "error": 0}))
        self.assertEqual([p["title"] for p in out["plans"]], ["A", "B"])
        self.assertEqual(len(list((config.home() / "runs" / "demo").glob("*.json"))), 2)
        code, human = self.main("run", a, b)
        self.assertIn("SUITE FAILED  2 plans: 1 passed, 1 failed, 0 error", human)

    def test_folder_runs_sorted_json_files_and_worst_exit_wins(self):
        folder = self.root / "plans"
        folder.mkdir()
        (folder / "02-bad.json").write_text(json.dumps(self.plan("Bad", "/missing")))
        (folder / "01-good.json").write_text(json.dumps(self.plan("Good")))
        (folder / "03-broken.json").write_text("{not json")
        (folder / "notes.txt").write_text("ignored")
        code, out = self.jmain("run", str(folder))
        self.assertEqual(code, 2)
        self.assertEqual([p["title"] for p in out["plans"]], ["Good", "Bad", str(folder / "03-broken.json")])
        self.assertEqual(out["counts"], {"passed": 1, "failed": 1, "error": 1})
        self.assertEqual(out["plans"][2]["error"]["code"], "PLAN_INVALID")

    def test_all_passing_suite_exits_zero_and_empty_folder_is_an_error(self):
        folder = self.root / "ok"
        folder.mkdir()
        (folder / "a.json").write_text(json.dumps(self.plan("A")))
        code, out = self.jmain("run", str(folder))
        self.assertEqual((code, out["status"]), (0, "passed"))
        (self.root / "empty").mkdir()
        code, out = self.jmain("run", str(self.root / "empty"))
        self.assertEqual((code, out["error"]["code"]), (2, "PLAN_INVALID"))

    def test_single_missing_plan_still_raises_exit_2(self):
        code, out = self.jmain("run", str(self.root / "nope.json"))
        self.assertEqual((code, out["error"]["code"]), (2, "PLAN_INVALID"))


if __name__ == "__main__":
    unittest.main()
