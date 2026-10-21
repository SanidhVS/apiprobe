import unittest

from apiprobe.errors import ApiProbeError
from apiprobe.vars import lookup, substitute, valid_path


class LookupTests(unittest.TestCase):
    data = {"data": {"token": "t", "items": [{"id": 7}, {"id": 8}], "none": None}}

    def test_nested(self):
        self.assertEqual(lookup(self.data, "data.token"), (True, "t"))

    def test_array_index(self):
        self.assertEqual(lookup(self.data, "data.items[1].id"), (True, 8))

    def test_root_array(self):
        self.assertEqual(lookup([{"id": 1}], "[0].id"), (True, 1))
        self.assertEqual(lookup({"items": [1, 2, 3]}, "items[2]"), (True, 3))

    def test_whole_body(self):
        self.assertEqual(lookup(self.data, ""), (True, self.data))

    def test_missing_vs_null(self):
        self.assertEqual(lookup(self.data, "data.none"), (True, None))
        self.assertEqual(lookup(self.data, "data.nope"), (False, None))

    def test_out_of_range(self):
        self.assertEqual(lookup(self.data, "data.items[5].id"), (False, None))

    def test_case_sensitive(self):
        self.assertEqual(lookup(self.data, "Data.token"), (False, None))

    def test_index_on_dict_and_key_on_list(self):
        self.assertFalse(lookup(self.data, "data[0]")[0])
        self.assertFalse(lookup(self.data, "data.items.id")[0])

    def test_malformed(self):
        self.assertFalse(valid_path("a..b"))
        self.assertFalse(valid_path("a[x]"))
        self.assertFalse(lookup(self.data, "data..token")[0])


class SubstituteTests(unittest.TestCase):
    def test_exact_match_keeps_int(self):
        self.assertEqual(substitute("{{n}}", {"n": 5}), 5)

    def test_embedded_stringifies(self):
        self.assertEqual(substitute("/api/x/{{n}}/y", {"n": 5}), "/api/x/5/y")
        self.assertEqual(substitute("a{{s}}b", {"s": "Z"}), "aZb")

    def test_recursive(self):
        out = substitute({"a": ["{{id}}", {"b": "x-{{id}}"}], "c": 1}, {"id": "q"})
        self.assertEqual(out, {"a": ["q", {"b": "x-q"}], "c": 1})

    def test_undefined_raises(self):
        with self.assertRaises(ApiProbeError) as ctx:
            substitute({"a": "{{missing}}"}, {})
        self.assertEqual(ctx.exception.code, "VAR_UNDEFINED")
        self.assertIn("missing", ctx.exception.message)

    def test_uuid_differs_per_occurrence(self):
        a, b = substitute(["{{$uuid}}", "{{$uuid}}"], {})
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 36)

    def test_now_format(self):
        self.assertRegex(substitute("{{$now}}", {}), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


if __name__ == "__main__":
    unittest.main()
