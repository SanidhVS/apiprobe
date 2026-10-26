import json
import re
import unittest
from pathlib import Path

from apiprobe import assertions, runner
from apiprobe.runner import check
from test_runner import TempHome, env_of


def ok(expectation, value, found=True):
    return check(expectation, found, value)[0]


class OperatorTests(unittest.TestCase):
    def test_matches(self):
        self.assertTrue(ok({"matches": "^INV-[0-9]{4}$"}, "INV-2026"))
        self.assertTrue(ok({"matches": "2026"}, "INV-2026-x"))  # search, not full match
        self.assertFalse(ok({"matches": "^INV"}, "x-INV"))
        passed, msg = check({"matches": "a"}, True, 5)
        self.assertFalse(passed)
        self.assertIn("needs a string", msg)

    def test_length(self):
        self.assertTrue(ok({"length": 3}, "abc"))
        self.assertTrue(ok({"length": 2}, [1, 2]))
        self.assertTrue(ok({"length": 1}, {"a": 1}))
        self.assertTrue(ok({"length": {"gte": 1}}, [1]))
        self.assertFalse(ok({"length": {"gte": 1}}, []))
        self.assertFalse(ok({"length": 2}, [1]))
        self.assertIn("needs a string, list or object", check({"length": 1}, True, 5)[1])
        self.assertIn("length is 0", check({"length": {"gt": 0}}, True, [])[1])

    def test_type(self):
        cases = {"string": "a", "number": 1.5, "integer": 3, "boolean": True, "array": [], "object": {}, "null": None}
        for name, value in cases.items():
            self.assertTrue(ok({"type": name}, value), name)
        self.assertTrue(ok({"type": "number"}, 3))  # integers are numbers
        self.assertTrue(ok({"type": "integer"}, 2.0))
        self.assertFalse(ok({"type": "integer"}, 1.5))
        self.assertFalse(ok({"type": "number"}, True))  # booleans are not numbers
        self.assertFalse(ok({"type": "string"}, None))
        self.assertTrue(ok({"type": "null"}, None))  # null is a value that was found

    def test_comparisons(self):
        self.assertTrue(ok({"gt": 1}, 2) and ok({"gte": 2}, 2) and ok({"lt": 3}, 2) and ok({"lte": 2}, 2))
        self.assertFalse(ok({"gt": 2}, 2) or ok({"gte": 3}, 2) or ok({"lt": 2}, 2) or ok({"lte": 1}, 2))
        self.assertTrue(ok({"gt": 1.5}, 2))
        for bad in (True, "5", None, [5]):
            self.assertFalse(ok({"gte": 0}, bad), bad)
        self.assertIn("expected a number > 2", check({"gt": 2}, True, 1)[1])

    def test_in(self):
        self.assertTrue(ok({"in": ["a", 2, None, {"k": 1}]}, {"k": 1}))
        self.assertTrue(ok({"in": ["a", 2]}, 2.0))
        self.assertFalse(ok({"in": [1, 2]}, True))  # true != 1
        self.assertFalse(ok({"in": []}, "a"))

    def test_equals_compares_a_literal_object(self):
        self.assertTrue(ok({"equals": {"type": "x"}}, {"type": "x"}))
        self.assertFalse(ok({"equals": {"type": "x"}}, {"type": "y"}))

    def test_operators_fail_on_missing_path_except_exists(self):
        for op in ({"matches": "a"}, {"length": 1}, {"type": "string"}, {"gt": 1}, {"in": [1]}, {"equals": 1}, {"contains": "a"}):
            self.assertEqual(check(op, False, None), (False, "path not found"), op)
        self.assertTrue(ok({"exists": False}, None, found=False))

    def test_v1_expectations_unchanged(self):
        self.assertTrue(ok({"a": 1}, {"a": 1}))
        self.assertTrue(ok({"a": 1, "type": "x"}, {"a": 1, "type": "x"}))  # two keys: still a literal
        self.assertTrue(ok({"exists": True}, None))
        self.assertTrue(ok({"contains": "x"}, "axb"))
        self.assertEqual(check("Pending", True, "Done"), (False, 'expected "Pending", got "Done"'))


class ShapeTests(unittest.TestCase):
    def test_valid_shapes_have_no_problems(self):
        for exp in ({"matches": "^a"}, {"length": 0}, {"length": {"lte": 5}}, {"type": "array"}, {"gt": 0}, {"in": []}, {"equals": 1},
                    {"exists": True}, "plain", {"a": 1}, {"gt": 1, "lt": 5}):
            self.assertEqual(assertions.expectation_problems(exp, "x"), [], exp)

    def test_invalid_shapes_are_reported_with_the_equals_hint(self):
        for exp in ({"matches": "("}, {"matches": 5}, {"length": -1}, {"length": "3"}, {"length": {"matches": "a"}}, {"length": True},
                    {"type": "str"}, {"type": ["string"]}, {"gt": "1"}, {"lte": True}, {"in": "abc"}):
            problems = assertions.expectation_problems(exp, "p")
            self.assertEqual(len(problems), 1, exp)
            self.assertIn('{"equals"', problems[0])

    def test_status_shapes(self):
        for good in (200, [200, 201], "2xx", "4xx"):
            self.assertEqual(assertions.status_problems(good, "s"), [], good)
        for bad in ("2XX_", "20x", "6xx", [], ["2xx"], True, 2.5, None, {}):
            self.assertTrue(assertions.status_problems(bad, "s"), bad)

    def test_status_matching(self):
        self.assertTrue(assertions.status_ok(201, 201) and assertions.status_ok([200, 204], 204) and assertions.status_ok("2xx", 299))
        self.assertFalse(assertions.status_ok(201, 200) or assertions.status_ok([200, 204], 201) or assertions.status_ok("4xx", 500))


class PlanCase(TempHome):
    def run_steps(self, *steps, **plan):
        plan = {"title": "t", "steps": [{"id": f"s{i}", **s} for i, s in enumerate(steps)], **plan}
        return runner.run_plan(plan, self.app, plan_path=str(Path(self.tmp.name) / "p.json"))

    @staticmethod
    def get(path, **kw):
        return {"call": {"method": "GET", "path": path, "as": "admin"}, **kw}

    @staticmethod
    def echo(body, **kw):
        return {"call": {"method": "POST", "path": "/echo", "as": "admin", "body": body}, **kw}


class StatusTests(PlanCase):
    def test_list_and_class(self):
        for want in ([200, 201], "2xx", 200):
            self.assertEqual(self.run_steps(self.get("/echo", expect={"status": want}))["status"], "passed", want)

    def test_fail_message_shows_what_was_expected(self):
        r = self.run_steps(self.get("/echo", expect={"status": [201, 202]}))
        self.assertEqual(r["steps"][0]["assertions"][0]["message"], "expected [201, 202], got 200")
        r = self.run_steps(self.get("/missing", expect={"status": "2xx"}))
        self.assertEqual(r["steps"][0]["assertions"][0]["message"], 'expected "2xx", got 404')

    def test_class_matches_error_responses(self):
        self.assertEqual(self.run_steps(self.get("/missing", expect={"status": "4xx"}))["status"], "passed")

    def test_default_is_still_2xx(self):
        self.assertEqual(self.run_steps(self.get("/missing"))["status"], "failed")


class HeaderTextTimeTests(PlanCase):
    def test_headers_case_insensitive_with_operators(self):
        r = self.run_steps(self.get("/created", expect={"status": 201, "headers": {
            "x-total": "5", "LOCATION": {"matches": "/items/[0-9]+$"}, "Content-Type": {"contains": "json"}}}))
        self.assertEqual(r["status"], "passed", r)

    def test_header_failures(self):
        r = self.run_steps(self.get("/created", expect={"headers": {"X-Total": "6", "X-Nope": {"exists": True}, "X-Gone": {"exists": False}}}))
        msgs = {a["target"]: a["message"] for a in r["steps"][0]["assertions"] if not a["passed"]}
        self.assertEqual(msgs, {"header X-Total": 'expected "6", got "5"', "header X-Nope": "header not in response"})

    def test_text_expectation_on_non_json(self):
        r = self.run_steps(self.get("/text", expect={"text": {"matches": "^hel+o$"}}), self.get("/text", expect={"text": "hello"}))
        self.assertEqual(r["status"], "passed", r)
        r = self.run_steps(self.get("/text", expect={"text": {"contains": "bye"}}))
        self.assertEqual(r["steps"][0]["assertions"][1]["message"], 'expected to contain "bye", got "hello"')

    def test_max_ms(self):
        self.assertEqual(self.run_steps(self.get("/echo", expect={"maxMs": 5000}))["status"], "passed")
        self.app["timeoutSeconds"] = 5
        r = self.run_steps(self.get("/slow", expect={"maxMs": 100}))
        a = r["steps"][0]["assertions"][1]
        self.assertFalse(a["passed"])
        self.assertRegex(a["message"], r"took \d+ms, limit 100ms")

    def test_new_operators_on_json_paths(self):
        r = self.run_steps(self.echo({"id": "INV-0042", "n": 7, "tags": ["a", "b"], "state": "open", "meta": {"type": "NDA"}}, expect={"json": {
            "body.id": {"matches": "^INV-[0-9]{4}$"}, "body.tags": {"length": {"gte": 1}}, "body.n": {"type": "integer"},
            "body.state": {"in": ["open", "closed"]}, "body.meta": {"equals": {"type": "NDA"}}}}))
        self.assertEqual(r["status"], "passed", r)
        bad = self.run_steps(self.echo({"n": 7}, expect={"json": {"body.n": {"lt": 5}}}))
        self.assertEqual(bad["steps"][0]["assertions"][1]["message"], "expected a number < 5, got 7")


class ExtractTests(PlanCase):
    def test_header_location_becomes_a_reusable_path(self):
        r = self.run_steps({"call": {"method": "GET", "path": "/created", "as": "admin"}, "extract": {"loc": "header:location", "total": "header:X-TOTAL"}},
                           {"call": {"method": "GET", "path": "{{loc}}", "as": "admin"}, "expect": {"json": {"data.id": "42"}}})
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["variables"], {"loc": "/items/42", "total": "5"})

    def test_location_on_another_host_is_kept_whole(self):
        r = self.run_steps({"call": {"method": "GET", "path": "/other-location"}, "extract": {"loc": "header:Location"}})
        self.assertEqual(r["variables"]["loc"], "http://elsewhere.test/items/42")

    def test_a_foreign_location_cannot_become_the_request_target(self):
        r = self.run_steps({"call": {"method": "GET", "path": "/other-location"}, "extract": {"loc": "header:Location"}},
                           {"call": {"method": "GET", "path": "{{loc}}"}})
        self.assertEqual(r["steps"][1]["error"]["code"], "PLAN_INVALID")
        self.assertEqual([q["path"] for q in self.server.requests], ["/other-location"])

    def test_location_must_start_at_the_base_url_boundary(self):
        self.assertEqual(runner._same_host_path("http://h:5000/a?x=1", "http://h:5000"), "/a?x=1")
        self.assertEqual(runner._same_host_path("http://h:5000/api/a", "http://h:5000/api"), "/a")
        self.assertEqual(runner._same_host_path("http://h:50001/a", "http://h:5000"), "http://h:50001/a")
        self.assertEqual(runner._same_host_path("/already/path", "http://h:5000"), "/already/path")

    def test_cookie_extract(self):
        r = self.run_steps({"call": {"method": "GET", "path": "/cookies"}, "extract": {"xsrf": "cookie:XSRF-TOKEN"}},
                           {"call": {"method": "POST", "path": "/echo", "headers": {"X-XSRF": "{{xsrf}}"}}})
        self.assertEqual(r["variables"], {"xsrf": "XVALUE"})
        self.assertEqual(self.server.requests[-1]["headers"]["X-Xsrf"], "XVALUE")

    def test_missing_header_and_cookie_fail_clearly(self):
        r = self.run_steps({"call": {"method": "GET", "path": "/echo"}, "extract": {"a": "header:X-Missing", "b": "cookie:nope", "c": "nope.path"}})
        msgs = [a["message"] for a in r["steps"][0]["assertions"] if not a["passed"]]
        self.assertEqual(msgs, ["extract header 'X-Missing' not in response", "extract cookie 'nope' not set by this response",
                                "extract path 'nope.path' not found in response"])

    def test_body_path_extract_unchanged(self):
        r = self.run_steps(self.echo({"id": 9}, extract={"v": "body.id"}))
        self.assertEqual(r["variables"], {"v": 9})


class CookieJarTests(PlanCase):
    steps = ({"call": {"method": "GET", "path": "/login-cookie"}}, {"call": {"method": "GET", "path": "/need-cookie"}})

    def test_off_by_default_so_v1_plans_behave_the_same(self):
        r = self.run_steps(*self.steps)
        self.assertEqual([s["status"] for s in r["steps"]], ["passed", "failed"])
        self.assertNotIn("Cookie", self.server.requests[-1]["headers"])

    def test_opt_in_carries_cookies_between_steps_and_cleanup(self):
        r = self.run_steps(*self.steps, cookies=True, cleanup=[{"method": "GET", "path": "/need-cookie"}])
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["cleanup"][0]["status"], "passed")
        self.assertEqual(self.server.requests[-1]["headers"]["Cookie"], "sid=abc123")

    def test_not_persisted_between_runs_or_into_results(self):
        self.run_steps(*self.steps, cookies=True)
        r = self.run_steps(self.steps[1], cookies=True)  # a fresh run starts with an empty jar
        self.assertEqual(r["steps"][0]["status"], "failed")
        self.assertNotIn("abc123", Path(r["savedTo"]).read_text(encoding="utf-8"))

    def test_explicit_cookie_auth_still_wins(self):
        self.app["environments"]["local"]["users"] = {"svc": {"token": "abc123", "cookie": "sid"}}
        r = self.run_steps(self.steps[0], {"call": {"method": "GET", "path": "/need-cookie", "as": "svc"}}, cookies=True)
        self.assertEqual(r["status"], "passed", r)


class ValidationTests(PlanCase):
    def problems(self, expect=None, extract=None, **plan):
        step = {"id": "s", "call": {"method": "GET", "path": "/x"}}
        if expect is not None:
            step["expect"] = expect
        if extract is not None:
            step["extract"] = extract
        return runner.validate_plan({"title": "t", "steps": [step], **plan}, env_of(self.app))

    def test_valid(self):
        self.assertEqual(self.problems(
            expect={"status": "2xx", "headers": {"X-A": {"matches": "a"}}, "text": {"contains": "x"}, "maxMs": 100,
                    "json": {"a": {"length": {"gte": 1}}}},
            extract={"a": "data.id", "b": "header:Location", "c": "cookie:sid"}, cookies=True), [])

    def test_invalid_are_all_reported_with_their_location(self):
        text = " ".join(self.problems(
            expect={"status": "9xx", "headers": [], "text": {"matches": "("}, "maxMs": -1, "json": {"a": {"type": "str"}}},
            extract={"a": "header:", "b": "cookie:  "}, cookies="yes"))
        for needle in ("expect.status", "expect.headers", "expect.text", "expect.maxMs", "expect.json.a", "extract", "cookies must be"):
            self.assertIn(needle, text)

    def test_header_expectation_shape_errors_name_the_header(self):
        self.assertIn("expect.headers.X-A", " ".join(self.problems(expect={"headers": {"X-A": {"in": "no"}}})))

    def test_bad_plan_is_rejected_before_sending(self):
        r = self.run_steps(self.get("/echo", expect={"json": {"a": {"gt": "x"}}}))
        self.assertEqual((r["status"], r["error"]["code"]), ("error", "PLAN_INVALID"))
        self.assertEqual(self.server.requests, [])

    def test_v1_literal_object_with_an_operator_name_needs_equals(self):
        text = " ".join(self.problems(expect={"json": {"meta": {"type": "NDA"}}}))
        self.assertIn('{"equals"', text)
        self.assertEqual(self.problems(expect={"json": {"meta": {"equals": {"type": "NDA"}}}}), [])
        self.assertEqual(self.problems(expect={"json": {"meta": {"type": "NDA", "id": 1}}}), [])  # two keys: a literal


if __name__ == "__main__":
    unittest.main()
