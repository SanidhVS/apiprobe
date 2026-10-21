import unittest

from apiprobe.runner import check


class CheckTests(unittest.TestCase):
    def test_plain_value(self):
        self.assertTrue(check("Pending", True, "Pending")[0])
        ok, msg = check("Pending", True, "Approved")
        self.assertFalse(ok)
        self.assertIn('expected "Pending", got "Approved"', msg)

    def test_plain_value_path_missing(self):
        self.assertEqual(check("Pending", False, None), (False, "path not found"))

    def test_null_and_number(self):
        self.assertTrue(check(None, True, None)[0])
        self.assertFalse(check(None, False, None)[0])
        self.assertTrue(check(5, True, 5)[0])
        self.assertTrue(check(5, True, 5.0)[0])
        self.assertFalse(check(1, True, True)[0])
        self.assertFalse(check(True, True, 1)[0])
        self.assertFalse(check("5", True, 5)[0])

    def test_deep_equal(self):
        self.assertTrue(check({"a": [1, {"b": 2}]}, True, {"a": [1, {"b": 2}]})[0])
        self.assertFalse(check({"a": 1}, True, {"a": 1, "b": 2})[0])
        self.assertTrue(check([1, 2], True, [1, 2])[0])
        self.assertFalse(check([1, 2], True, [2, 1])[0])

    def test_exists_true(self):
        self.assertTrue(check({"exists": True}, True, None)[0])
        self.assertEqual(check({"exists": True}, False, None), (False, "path not found"))

    def test_exists_false(self):
        self.assertTrue(check({"exists": False}, False, None)[0])
        self.assertFalse(check({"exists": False}, True, 1)[0])

    def test_contains_string(self):
        self.assertTrue(check({"contains": "lap"}, True, "overlap found")[0])
        self.assertFalse(check({"contains": "zzz"}, True, "overlap")[0])

    def test_contains_list(self):
        self.assertTrue(check({"contains": {"id": 1}}, True, [{"id": 2}, {"id": 1}])[0])
        self.assertFalse(check({"contains": 3}, True, [1, 2])[0])

    def test_contains_wrong_type(self):
        ok, msg = check({"contains": 1}, True, 5)
        self.assertFalse(ok)
        self.assertIn("string or list", msg)
        self.assertEqual(check({"contains": "x"}, False, None), (False, "path not found"))

    def test_object_with_extra_keys_is_plain_value(self):
        exp = {"exists": True, "other": 1}
        self.assertTrue(check(exp, True, {"exists": True, "other": 1})[0])
        self.assertFalse(check(exp, True, "anything")[0])
        self.assertFalse(check({"contains": "a", "x": 1}, True, "abc")[0])


if __name__ == "__main__":
    unittest.main()
