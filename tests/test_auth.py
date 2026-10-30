import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from apiprobe import cli, config, http, runner, spec
from apiprobe.errors import ApiProbeError
from test_config import valid_cfg
from test_runner import TempHome, env_of

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "openapi.json").read_text(encoding="utf-8"))
SECRET = "sekret-key-123"


class SendAuthTests(TempHome):
    def send(self, auth, **kw):
        return http.send(env_of(self.app), 1, "GET", "/echo", auth=auth, **kw)

    def test_bearer_is_default(self):
        self.send({"token": "t1"})
        self.assertEqual(self.server.requests[-1]["headers"]["Authorization"], "Bearer t1")

    def test_scheme(self):
        self.send({"token": "dXNlcjpwdw==", "scheme": "Basic"})
        self.assertEqual(self.server.requests[-1]["headers"]["Authorization"], "Basic dXNlcjpwdw==")

    def test_header(self):
        self.send({"token": SECRET, "header": "X-Api-Key"})
        h = self.server.requests[-1]["headers"]
        self.assertEqual(h["X-Api-Key"], SECRET)
        self.assertNotIn("Authorization", h)

    def test_cookie_alone_and_joined_to_an_existing_cookie_header(self):
        self.send({"token": SECRET, "cookie": "session"})
        self.assertEqual(self.server.requests[-1]["headers"]["Cookie"], f"session={SECRET}")
        self.send({"token": SECRET, "cookie": "session"}, headers={"cookie": "a=1"})
        self.assertEqual(self.server.requests[-1]["headers"]["Cookie"], f"a=1; session={SECRET}")

    def test_query_is_sent_but_masked_in_result(self):
        r = self.send({"token": "a b&c", "query": "api_key"}, query={"x": "1"})
        self.assertEqual(r["body"]["query"], {"x": ["1"], "api_key": ["a b&c"]})
        self.assertTrue(r["url"].endswith("/echo?x=1&api_key=***"), r["url"])

    def test_query_token_masked_in_transport_errors(self):
        with self.assertRaises(ApiProbeError) as ctx:
            http.send({"baseUrl": "http://127.0.0.1:1"}, 1, "GET", "/x", auth={"token": SECRET, "query": "k"})
        self.assertNotIn(SECRET, ctx.exception.message)
        self.assertIn("k=***", ctx.exception.message)

    def test_no_auth_sends_no_credentials(self):
        self.send(None)
        h = self.server.requests[-1]["headers"]
        self.assertFalse({"Authorization", "Cookie", "X-Api-Key"} & set(h))


class PlanAuthTests(TempHome):
    def use(self, user):
        self.app["environments"]["local"]["users"] = {"svc": user}

    def run_plan(self, path="/echo"):
        plan = {"title": "t", "steps": [{"id": "s", "call": {"method": "GET", "path": path, "as": "svc"}}]}
        return runner.run_plan(plan, self.app)

    def test_each_form_reaches_the_api(self):
        for user, check in (({"token": "t"}, lambda h: h["Authorization"] == "Bearer t"),
                            ({"token": "t", "scheme": "Token"}, lambda h: h["Authorization"] == "Token t"),
                            ({"token": "t", "header": "X-Api-Key"}, lambda h: h["X-Api-Key"] == "t"),
                            ({"token": "t", "cookie": "sid"}, lambda h: h["Cookie"] == "sid=t")):
            self.use(user)
            self.assertEqual(self.run_plan()["status"], "passed")
            self.assertTrue(check(self.server.requests[-1]["headers"]), user)

    def test_query_token_never_in_result_or_saved_run(self):
        self.use({"token": SECRET, "query": "api_key"})
        result = self.run_plan("/text")  # /echo would reflect the key back in the response body
        self.assertEqual(result["status"], "passed")
        text = Path(result["savedTo"]).read_text(encoding="utf-8") + json.dumps(result)
        self.assertNotIn(SECRET, text)

    def test_401_gets_token_hint_for_any_form(self):
        self.use({"token": "bad"})
        self.assertIn("users.svc.token", self.run_plan("/protected")["steps"][0]["assertions"][0]["message"])
        self.use({"token": "tok-ok", "header": "X-Api-Key"})  # stub only accepts Bearer -> 401
        self.assertIn("users.svc.token", self.run_plan("/protected")["steps"][0]["assertions"][0]["message"])

    def test_auth_rejected_covers_401_and_empty_403_only(self):
        self.assertTrue(runner.auth_rejected({"status": 401, "bodyText": "x"}))
        self.assertTrue(runner.auth_rejected({"status": 403, "bodyText": " "}))
        self.assertFalse(runner.auth_rejected({"status": 403, "bodyText": '{"message":"role"}'}))
        self.assertFalse(runner.auth_rejected({"status": 404, "bodyText": ""}))


class ConfigAuthTests(unittest.TestCase):
    def problems(self, user):
        cfg = valid_cfg(environments={"local": {"baseUrl": "http://x", "users": {"u": user}}})
        return config._validate(cfg, Path("c.json"))

    def test_valid_forms(self):
        for user in ({"token": "t"}, {"token": "t", "scheme": "Basic"}, {"token": "t", "header": "X-Api-Key"},
                     {"token": "t", "query": "api_key"}, {"token": "t", "cookie": "session"}, {"token": "CHANGE_ME", "header": "X-Key"}):
            self.assertEqual(self.problems(user), [], user)

    def test_combinations_and_bad_values_rejected(self):
        self.assertIn("at most one of", " ".join(self.problems({"token": "t", "header": "X-K", "query": "k"})))
        for bad in ({"scheme": ""}, {"scheme": "Two Words"}, {"header": ""}, {"header": "Bad Header"}, {"header": "X:Y"},
                    {"query": ""}, {"cookie": "a b"}, {"header": 5}):
            self.assertTrue(self.problems({"token": "t", **bad}), bad)

    def test_username_password_rejected_even_next_to_a_token(self):
        for user in ({"username": "a", "password": "b"}, {"token": "t", "password": "b"}, {"token": "t", "username": "a"}):
            self.assertIn("not supported", " ".join(self.problems(user)), user)

    def test_token_with_line_break_rejected(self):
        self.assertIn("line break", " ".join(self.problems({"token": "abc\r\nX-Evil: 1"})))


class SpecAuthTests(unittest.TestCase):
    def test_describe_security_joins_schemes(self):
        d = spec.describe(FIXTURE, "POST", "/api/documents")
        self.assertEqual([[s["id"] for s in alt] for alt in d["security"]], [["ApiKey"], ["Bearer"]])
        self.assertEqual(d["security"][0][0], {"id": "ApiKey", "type": "apiKey", "in": "header", "param": "X-Api-Key"})
        self.assertEqual(d["security"][1][0], {"id": "Bearer", "type": "http", "scheme": "bearer", "bearerFormat": "JWT"})

    def test_global_security_inherited_and_op_level_empty_overrides(self):
        s = json.loads(json.dumps(FIXTURE))
        s["security"] = [{"Bearer": []}]
        s["paths"]["/api/leave/pending"]["get"]["security"] = []
        self.assertEqual(spec.describe(s, "GET", "/api/leave")["security"][0][0]["id"], "Bearer")
        self.assertEqual(spec.describe(s, "GET", "/api/leave/pending")["security"], [])
        self.assertEqual(spec.describe(FIXTURE, "GET", "/api/leave")["security"], [])

    def test_undefined_scheme_is_reported_not_crashed(self):
        s = json.loads(json.dumps(FIXTURE))
        s["paths"]["/api/leave"]["get"]["security"] = [{"Ghost": []}]
        self.assertEqual(spec.describe(s, "GET", "/api/leave")["security"], [[{"id": "Ghost", "type": None}]])

    def test_security_schemes_listed(self):
        self.assertEqual(set(spec.security_schemes(FIXTURE)), {"Bearer", "ApiKey"})


class CliAuthTests(TempHome):
    def setUp(self):
        super().setUp()
        self.old_cwd = os.getcwd()
        self.work = tempfile.TemporaryDirectory()
        os.chdir(self.work.name)
        (config.home() / "apps").mkdir(parents=True)
        self.write_config(self.server.url)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.work.cleanup()
        super().tearDown()

    def write_config(self, base_url, users=None):
        cfg = {"specUrl": "/openapi.json", "repoPath": str(Path(self.work.name).resolve()), "defaultEnvironment": "local",
               "environments": {"local": {"baseUrl": base_url, "users": users or {"k": {"token": SECRET, "header": "X-Api-Key"}}}}}
        (config.home() / "apps" / "demo.json").write_text(json.dumps(cfg))

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main([*argv, "--app", "demo"])
        return code, out.getvalue()

    def test_call_uses_configured_form_and_masks_query(self):
        self.main("call", "GET", "/echo", "--as", "k")
        self.assertEqual(self.server.requests[-1]["headers"]["X-Api-Key"], SECRET)
        self.write_config(self.server.url, {"k": {"token": SECRET, "query": "api_key"}})
        code, out = self.main("call", "GET", "/text", "--as", "k")
        self.assertNotIn(SECRET, out)
        self.assertIn("/text?api_key=***", out)

    def test_show_prints_auth_line(self):
        _, out = self.main("show", "POST /api/documents")
        self.assertIn("Auth: X-Api-Key header (apiKey) | http bearer", out)
        _, out = self.main("show", "GET /api/leave")
        self.assertNotIn("Auth:", out)

    def test_app_json_lists_security_schemes_unless_no_spec_or_unreachable(self):
        _, out = self.main("app", "--json")
        self.assertEqual(set(json.loads(out)["securitySchemes"]), {"Bearer", "ApiKey"})
        _, out = self.main("app", "--json", "--no-spec")
        self.assertNotIn("securitySchemes", json.loads(out))
        self.write_config("http://127.0.0.1:1")
        code, out = self.main("app", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("securitySchemes", json.loads(out))


if __name__ == "__main__":
    unittest.main()
