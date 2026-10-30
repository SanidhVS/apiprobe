import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apiprobe import cli, config, spec
from apiprobe.errors import ApiProbeError
from test_runner import TempHome, env_of

try:
    import yaml
except ImportError:
    yaml = None

FIXTURES = Path(__file__).parent / "fixtures"
JSON_SPEC = json.loads((FIXTURES / "openapi.json").read_text(encoding="utf-8"))


class YamlCase(TempHome):
    def setUp(self):
        super().setUp()
        self.old_cwd = os.getcwd()
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name).resolve()
        os.chdir(self.root)
        (config.home() / "apps").mkdir(parents=True)
        self.write_config("/openapi.yaml")

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.work.cleanup()
        super().tearDown()

    def write_config(self, spec_url):
        cfg = {"specUrl": spec_url, "repoPath": str(self.root), "defaultEnvironment": "local",
               "environments": {"local": {"baseUrl": self.server.url, "users": {"admin": {"token": "tok-admin"}}}}}
        (config.home() / "apps" / "demo.json").write_text(json.dumps(cfg))
        self.app = config.load_app("demo")

    def load(self):
        return spec.load_spec(self.app, env_of(self.app))

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main([*argv, "--app", "demo", "--json"])
        return code, json.loads(out.getvalue())


@unittest.skipUnless(yaml, "PyYAML not installed (uv run --with pyyaml ...)")
class WithPyYamlTests(YamlCase):
    def test_yaml_spec_over_http_loads_and_looks_like_a_json_spec(self):
        doc = self.load()
        self.assertEqual(doc["openapi"], "3.0.1")
        responses = doc["paths"]["/api/pets/{id}"]["get"]["responses"]
        self.assertEqual(list(responses), ["200", "404"])  # unquoted `200:` keys are strings, as in JSON
        self.assertEqual(responses["200"]["content"]["application/json"]["schema"]["properties"]["born"]["example"], "2026-01-31")  # not a date object

    def test_yaml_spec_file_in_the_repo(self):
        (self.root / "spec.yaml").write_text((FIXTURES / "openapi.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        self.write_config("spec.yaml")
        self.assertIn("/api/pets/{id}", self.load()["paths"])

    def test_a_dumped_json_fixture_round_trips_through_yaml(self):
        (self.root / "spec.yml").write_text(yaml.safe_dump(JSON_SPEC), encoding="utf-8")
        self.write_config("spec.yml")
        self.assertEqual(self.load(), JSON_SPEC)

    def test_find_show_scaffold_work_and_the_spec_cache_is_written(self):
        code, found = self.main("find", "pet")
        self.assertEqual((code, found[0]["path"]), (0, "/api/pets/{id}"))
        code, d = self.main("show", "GET /api/pets/1")
        self.assertEqual((code, d["parameters"][0]["name"]), (0, "id"))
        code, step = self.main("scaffold", "GET /api/pets/1")
        self.assertEqual((code, step["call"]["path"], step["expect"]), (0, "/api/pets/{{id}}", {"status": 200}))
        cached = json.loads(next((config.home() / "cache").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(cached["spec"]["info"]["title"], "Yaml API")

    def test_json_specs_still_load_first(self):
        self.write_config("/openapi.json")
        self.assertEqual(self.load(), JSON_SPEC)

    def test_text_that_is_neither_json_nor_yaml(self):
        (self.root / "bad.yaml").write_text("a: [unclosed\nb: }{", encoding="utf-8")
        self.write_config("bad.yaml")
        with self.assertRaises(ApiProbeError) as ctx:
            self.load()
        self.assertEqual(ctx.exception.code, "SPEC_UNAVAILABLE")
        self.assertIn("neither JSON nor valid YAML", ctx.exception.message)

    def test_yaml_that_is_not_an_openapi_document(self):
        (self.root / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")
        self.write_config("list.yaml")
        with self.assertRaises(ApiProbeError) as ctx:
            self.load()
        self.assertIn("not an OpenAPI document", ctx.exception.message)

    def test_swagger_2_yaml_is_still_refused(self):
        (self.root / "s2.yaml").write_text("swagger: '2.0'\npaths: {}\n", encoding="utf-8")
        self.write_config("s2.yaml")
        with self.assertRaises(ApiProbeError) as ctx:
            self.load()
        self.assertIn("Swagger 2.0 not supported", ctx.exception.message)


class WithoutPyYamlTests(YamlCase):
    """PyYAML is optional: with the import blocked, YAML specs fail clearly and JSON specs are unaffected."""

    def test_yaml_spec_names_the_install_command(self):
        with mock.patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaises(ApiProbeError) as ctx:
                self.load()
        self.assertEqual(ctx.exception.code, "SPEC_UNAVAILABLE")
        self.assertIn('uv tool install --editable ".[yaml]"', ctx.exception.message)
        self.assertIn(self.app["configPath"], ctx.exception.message)

    def test_cli_reports_the_same_error_with_exit_2(self):
        with mock.patch.dict(sys.modules, {"yaml": None}):
            code, out = self.main("find", "pet")
        self.assertEqual((code, out["error"]["code"]), (2, "SPEC_UNAVAILABLE"))
        self.assertIn(".[yaml]", out["error"]["message"])

    def test_json_spec_does_not_need_pyyaml(self):
        self.write_config("/openapi.json")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            self.assertEqual(self.load(), JSON_SPEC)

    def test_garbled_json_without_pyyaml_still_points_at_the_spec(self):
        (self.root / "bad.json").write_text('{"openapi": "3.0.1",', encoding="utf-8")
        self.write_config("bad.json")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaises(ApiProbeError) as ctx:
                self.load()
        self.assertIn("bad.json", ctx.exception.message)

    def test_pyproject_declares_the_optional_extra(self):
        text = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[project.optional-dependencies]", text)
        self.assertIn('yaml = ["pyyaml>=6"]', text)
        self.assertIn("dependencies = []", text)  # runtime stays stdlib-only


if __name__ == "__main__":
    unittest.main()
