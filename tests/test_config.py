import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from apiprobe import cli, config
from apiprobe.errors import ApiProbeError


def valid_cfg(repo=None, **over):
    cfg = {
        "specUrl": "/openapi.json",
        "defaultEnvironment": "local",
        "environments": {"local": {"baseUrl": "http://127.0.0.1:1/", "users": {
            "admin": {"username": "a", "password": "pw"},
            "todo": {"username": "b", "password": "CHANGE_ME"},
            "bot": {"token": ""},
        }}},
    }
    if repo:
        cfg["repoPath"] = str(repo)
    cfg.update(over)
    return cfg


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["APIPROBE_HOME"] = str(self.root / "home")
        (self.root / "home" / "apps").mkdir(parents=True)

    def tearDown(self):
        del os.environ["APIPROBE_HOME"]
        self.tmp.cleanup()

    def write(self, name, cfg):
        path = self.root / "home" / "apps" / f"{name}.json"
        path.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg))
        return path

    def assertError(self, code, fn, *needles):
        with self.assertRaises(ApiProbeError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        for n in needles:
            self.assertIn(n, ctx.exception.message)
        return ctx.exception

    def test_home_override(self):
        self.assertEqual(config.home(), self.root / "home")

    def test_valid_load(self):
        self.write("demo", valid_cfg())
        app = config.load_app("demo")
        self.assertEqual(app["name"], "demo")
        env = config.resolve_env(app, None)
        self.assertEqual(env["baseUrl"], "http://127.0.0.1:1")
        self.assertTrue(env["verifyTls"])

    def test_invalid_json(self):
        path = self.write("demo", "{bad")
        self.assertError("CONFIG_INVALID", lambda: config.load_app("demo"), str(path), "not valid JSON")

    def test_validation_names_file_and_field(self):
        cases = [
            ({"specUrl": None}, "specUrl"),
            ({"defaultEnvironment": "dev"}, "defaultEnvironment"),
            ({"environments": {"local": {"baseUrl": "localhost:5000"}}}, "environments.local.baseUrl"),
            ({"environments": {"local": {"baseUrl": "http://x", "users": {"u": {"username": "x"}}}}}, "environments.local.users.u"),
            ({"login": {"path": "/login"}}, "login.body"),
            ({"login": {"path": "/login", "body": {}}}, "login.tokenPath"),
            ({"timeoutSeconds": 0}, "timeoutSeconds"),
        ]
        for over, field in cases:
            with self.subTest(field=field):
                path = self.write("demo", valid_cfg(**over))
                self.assertError("CONFIG_INVALID", lambda: config.load_app("demo"), str(path), field)

    def test_unknown_app(self):
        self.write("one", valid_cfg())
        self.assertError("APP_NOT_FOUND", lambda: config.load_app("two"), "one")

    def test_detect_from_subfolder(self):
        repo = self.root / "repo"
        (repo / "src" / "deep").mkdir(parents=True)
        self.write("demo", valid_cfg(repo))
        self.assertEqual(config.detect_app(repo / "src" / "deep")["name"], "demo")
        self.assertEqual(config.detect_app(repo)["name"], "demo")

    def test_detect_normalises_separators_and_case(self):
        repo = self.root / "Repo"
        repo.mkdir()
        self.write("demo", valid_cfg(str(repo).replace("\\", "/").upper() if os.name == "nt" else repo))
        self.assertEqual(config.detect_app(repo)["name"], "demo")

    def test_detect_longest_match(self):
        outer = self.root / "outer"
        inner = outer / "inner"
        (inner / "x").mkdir(parents=True)
        self.write("outer", valid_cfg(outer))
        self.write("inner", valid_cfg(inner))
        self.assertEqual(config.detect_app(inner / "x")["name"], "inner")
        self.assertEqual(config.detect_app(outer)["name"], "outer")

    def test_detect_sibling_prefix_does_not_match(self):
        (self.root / "repo").mkdir()
        (self.root / "repo2").mkdir()
        self.write("demo", valid_cfg(self.root / "repo"))
        self.assertError("APP_NOT_FOUND", lambda: config.detect_app(self.root / "repo2"), "demo")

    def test_app_flag_wins(self):
        repo = self.root / "repo"
        repo.mkdir()
        self.write("detected", valid_cfg(repo))
        self.write("chosen", valid_cfg())
        cwd = os.getcwd()
        os.chdir(repo)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["app", "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue())["detected"], "detected")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["app", "--app", "chosen", "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue())["detected"], "chosen")
        finally:
            os.chdir(cwd)

    def test_init_writes_template_and_refuses_overwrite(self):
        path = config.init_app("new-app", str(self.root))
        data = json.loads(path.read_text())
        self.assertEqual(data["environments"]["local"]["users"]["admin"]["password"], "CHANGE_ME")
        self.assertEqual(config.load_app("new-app")["name"], "new-app")  # template itself is valid
        self.assertError("APP_EXISTS", lambda: config.init_app("new-app", None), str(path))

    def test_change_me_only_errors_when_used(self):
        path = self.write("demo", valid_cfg())
        app = config.load_app("demo")  # placeholders don't block loading
        env = config.resolve_env(app, None)
        self.assertEqual(config.get_user(app, env, "admin")["password"], "pw")
        self.assertError("USER_NOT_CONFIGURED", lambda: config.get_user(app, env, "todo"), str(path), "local.users.todo")
        self.assertError("USER_NOT_CONFIGURED", lambda: config.get_user(app, env, "bot"), "bot.token")
        self.assertError("USER_NOT_FOUND", lambda: config.get_user(app, env, "ghost"), "ghost", "admin")

    def test_unknown_env(self):
        self.write("demo", valid_cfg())
        app = config.load_app("demo")
        self.assertError("ENV_NOT_FOUND", lambda: config.resolve_env(app, "prod"), "prod", "local")

    def test_list_apps_reports_broken_config(self):
        self.write("good", valid_cfg())
        self.write("bad", "[1")
        apps = {a["name"]: a for a in config.list_apps()}
        self.assertEqual(apps["good"]["environments"], ["local"])
        self.assertIn("not valid JSON", apps["bad"]["error"])


if __name__ == "__main__":
    unittest.main()
