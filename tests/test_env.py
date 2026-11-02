import os
import unittest
from unittest.mock import patch

from core import env as envmod


class ParseTest(unittest.TestCase):
    def test_basic_formats(self):
        values = envmod.parse(
            "# comment\n"
            "\n"
            "PLAIN=abc\n"
            "export EXPORTED = spaced \n"
            "DOUBLE=\"has # hash\"\n"
            "SINGLE='x=y'\n"
            "INLINE=value # note\n"
            "HASH=a#b\n"
            "EMPTY=\n"
        )
        self.assertEqual(values, {
            "PLAIN": "abc", "EXPORTED": "spaced", "DOUBLE": "has # hash", "SINGLE": "x=y",
            "INLINE": "value", "HASH": "a#b", "EMPTY": "",
        })

    def test_bad_line_reports_line_without_value(self):
        with self.assertRaises(ValueError) as ctx:
            envmod.parse("OK=1\nsk-secret-without-key\n")
        self.assertIn("第 2 行", str(ctx.exception))
        self.assertNotIn("sk-secret", str(ctx.exception))


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(envmod, "_values", {"FROM_FILE": "file", "BOTH": "file", "LIST": " a, b ,,c ", "BLANK": ""}))
        for key in ("FROM_FILE", "BOTH", "LIST", "BLANK", "MISSING"):
            self.enterContext(patch.dict(os.environ))
            os.environ.pop(key, None)

    def test_process_env_overrides_file(self):
        os.environ["BOTH"] = "process"
        self.assertEqual(envmod.env("BOTH"), "process")
        self.assertEqual(envmod.env("FROM_FILE"), "file")
        self.assertEqual(envmod.env("MISSING", "default"), "default")

    def test_explicit_empty_value_beats_default(self):
        self.assertEqual(envmod.env("BLANK", "default"), "")

    def test_env_list(self):
        self.assertEqual(envmod.env_list("LIST"), ["a", "b", "c"])
        self.assertEqual(envmod.env_list("BLANK", ["x"]), [])
        self.assertEqual(envmod.env_list("MISSING", ["x"]), ["x"])

    def test_values_are_not_exported_to_process_env(self):
        self.assertNotIn("FROM_FILE", os.environ)


if __name__ == "__main__":
    unittest.main()
