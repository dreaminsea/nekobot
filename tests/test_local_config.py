import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import env as envmod
from core import local_config

ROOT = Path(__file__).resolve().parent.parent


class LocalConfigTest(unittest.TestCase):
    def setUp(self):
        self.local = Path(self.enterContext(tempfile.TemporaryDirectory())) / "config.local.py"
        self.enterContext(patch.object(local_config, "LOCAL_FILE", self.local))
        self.enterContext(patch.object(envmod, "_values", {"LLM_API_KEY": "sk-main", "MASTER_NAME": "主人"}))
        self.enterContext(patch.dict(os.environ))
        for key in ("LLM_API_KEY", "DEEPSEEK_SEARCH_API_KEY", "MASTER_NAME", "MASTER_QQ"):
            os.environ.pop(key, None)

    def load(self, local: str | None = None):
        """按 bot 的方式执行一份全新的 config.py。"""
        if local is not None:
            self.local.write_text(local, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("config_under_test", ROOT / "config.py")
        module = importlib.util.module_from_spec(spec)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            spec.loader.exec_module(module)
        return module, err.getvalue()

    def test_missing_local_file_keeps_defaults(self):
        config, err = self.load()
        self.assertEqual(config.LLM_MODEL, "deepseek-flash")
        self.assertEqual(config.DEEPSEEK_SEARCH_API_KEY, "sk-main")
        self.assertEqual(err, "")

    def test_overrides_can_reference_defaults(self):
        config, _ = self.load('BOT_NAMES = BOT_NAMES + ["小喵"]\nGROUP_TRIGGER = ["/neko"]\nN = len(BOT_NAMES)\n')
        self.assertEqual(config.BOT_NAMES[-1], "小喵")
        self.assertEqual(config.GROUP_TRIGGER, ["/neko"])

    def test_dependent_defaults_follow_overrides(self):
        config, _ = self.load('LLM_MODEL = "other"\nLLM_BASE_URL = "https://api.example.com/v1"\n'
                              'REPLY_SEPARATOR = "<|sep|>"\n')
        self.assertEqual(config.AMBIENT_REPLY_MODEL, "other")
        # 换了供应商就不能再把主 Key 发给 DeepSeek 搜索
        self.assertEqual(config.DEEPSEEK_SEARCH_API_KEY, "")
        self.assertIn("<|sep|>", config.REPLY_STYLE_PROMPT)
        self.assertNotIn("<|new_message|>", config.REPLY_STYLE_PROMPT)

    def test_explicit_local_value_beats_recomputation(self):
        config, _ = self.load('LLM_MODEL = "other"\nAMBIENT_REPLY_MODEL = "gate-model"\n')
        self.assertEqual((config.LLM_MODEL, config.AMBIENT_REPLY_MODEL), ("other", "gate-model"))

    def test_env_backed_override_updates_prompt(self):
        config, _ = self.load('MASTER_NAME = "本地主人"\n')
        self.assertIn("本地主人", config.SYSTEM_PROMPT)

    def test_unknown_names_warn_but_apply(self):
        config, err = self.load('LLM_MODLE = "typo"\n_helper = 1\n')
        self.assertEqual(config.LLM_MODLE, "typo")
        self.assertIn("LLM_MODLE", err)
        self.assertNotIn("_helper", err)

    def test_syntax_error_points_at_local_file(self):
        with self.assertRaises(SyntaxError) as ctx:
            self.load("LLM_MODEL = \n")
        self.assertEqual(Path(ctx.exception.filename).name, "config.local.py")


if __name__ == "__main__":
    unittest.main()
