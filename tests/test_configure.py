import contextlib
import io
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

import configure
from core import env as envmod

ROOT = Path(__file__).resolve().parent.parent


def _root(tmp: Path) -> Path:
    """在临时目录里放一份 config.py / .env.example / adapters，供命令行读写。"""
    shutil.copy(ROOT / "config.py", tmp / "config.py")
    shutil.copy(ROOT / ".env.example", tmp / ".env.example")
    shutil.copytree(ROOT / "adapters", tmp / "adapters")
    return tmp


def _run(argv: list[str], answers: list[str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    # 换成非终端 stdin：密钥输入也走被替换的 input()，在终端里跑测试不会卡在 getpass
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            patch("sys.stdin", io.StringIO()), patch("builtins.input", side_effect=answers or []):
        code = configure.main(argv)
    return code, out.getvalue(), err.getvalue()


class EnvFormatTest(unittest.TestCase):
    def test_parser_matches_bot(self):
        text = (
            "# comment\n\nPLAIN=abc\nexport EXPORTED = spaced \nDOUBLE=\"has # hash\"\n"
            "SINGLE='x=y'\nINLINE=value # note\nHASH=a#b\nEMPTY=\nexport  TWO=1\nexportX=2\nDUP=1\nDUP=2\n"
        )
        self.assertEqual(configure.parse_env(text), envmod.parse(text))
        for bad in ("sk-secret\n", "export =1\n", "1KEY=2\n"):
            with self.assertRaises(ValueError):
                envmod.parse(bad)
            with self.assertRaises(ValueError) as ctx:
                configure.parse_env(bad)
            self.assertIn("第 1 行", str(ctx.exception))
            self.assertNotIn("sk-secret", str(ctx.exception))

    def test_quote_round_trips_through_bot_parser(self):
        for value in ("", "plain", "a #b", " lead", "trail ", "'q'", '"q"', "#start", 'x"', "a=b",
                      "中文 # 注释", "\ttab", "a\tb #c", '"', "'mixed\""):
            line = f"KEY={configure.env_quote(value)}"
            self.assertEqual(envmod.parse(line)["KEY"], value, line)
        for bad in ("a\nb", "a\rb", "a\x0cb", "a\N{LINE SEPARATOR}b"):
            with self.assertRaises(ValueError):
                configure.env_quote(bad)

    def test_mask_hides_secrets(self):
        self.assertEqual(configure.mask("short-token"), "****")
        self.assertEqual(configure.mask("sk-0123456789abcdef"), "sk-****cdef")
        self.assertEqual(configure.env_display("UNKNOWN", "custom-value"), "****")
        self.assertEqual(configure.env_display("MASTER_QQ", "10001"), "10001")

    def test_normalize(self):
        qq_list = configure.ENV_BY_NAME["ADMIN_QQ"]
        self.assertEqual(configure.normalize_env_value(qq_list, " 1，2 , ,3 "), "1,2,3")
        with self.assertRaises(ValueError):
            configure.normalize_env_value(qq_list, "1,abc")
        with self.assertRaises(ValueError):
            configure.normalize_env_value(configure.ENV_BY_NAME["MASTER_QQ"], "１２３")
        self.assertEqual(configure.normalize_env_value(configure.ENV_BY_NAME["MASTER_QQ"], ""), "")


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_create_from_template(self):
        env = configure.EnvFile(self.tmp / ".env", ROOT / ".env.example")
        self.assertFalse(env.exists)
        env.set("LLM_API_KEY", "sk-test")
        env.set(configure.SEARCH_KEY, "")      # 复用模板里的 `# DEEPSEEK_SEARCH_API_KEY=` 占位行
        env.set("CUSTOM_KEY", "a #b")          # 模板里没有，追加到末尾
        env.save()
        path = self.tmp / ".env"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text(encoding="utf-8")
        values = envmod.parse(text)
        self.assertEqual(values["LLM_API_KEY"], "sk-test")
        self.assertEqual(values[configure.SEARCH_KEY], "")
        self.assertEqual(values["CUSTOM_KEY"], "a #b")
        self.assertIn("# 主人昵称，会写进人设提示词", text)  # 模板注释保留
        self.assertNotIn("# DEEPSEEK_SEARCH_API_KEY=", text)
        self.assertTrue(text.rstrip().endswith('CUSTOM_KEY="a #b"'))

    def test_update_preserves_layout(self):
        path = self.tmp / ".env"
        path.write_text("# head\nexport LLM_API_KEY=old  # 备注\nDUP=1\nOTHER=keep\nDUP=2\n", encoding="utf-8")
        path.chmod(0o640)
        env = configure.EnvFile(path)
        env.set("LLM_API_KEY", "new")
        env.set("DUP", "3")
        env.save()
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "# head\nexport LLM_API_KEY=new  # 备注\nOTHER=keep\nDUP=3\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_inline_comment_moves_when_value_needs_quotes(self):
        path = self.tmp / ".env"
        path.write_text("NAME=old # 备注\n", encoding="utf-8")
        env = configure.EnvFile(path)
        env.set("NAME", "")
        env.save()
        self.assertEqual(path.read_text(encoding="utf-8"), "# 备注\nNAME=\n")
        self.assertEqual(envmod.parse(path.read_text(encoding="utf-8")), {"NAME": ""})

    def test_unset_leaves_empty_placeholder(self):
        path = self.tmp / ".env"
        path.write_text("A=1\nDEEPSEEK_SEARCH_API_KEY=sk-secret\n", encoding="utf-8")
        env = configure.EnvFile(path)
        env.set(configure.SEARCH_KEY, None)
        env.save()
        self.assertEqual(path.read_text(encoding="utf-8"), "A=1\n# DEEPSEEK_SEARCH_API_KEY=\n")

    def test_symlink_target_is_updated(self):
        real = self.tmp / "real.env"
        real.write_text("A=1\n", encoding="utf-8")
        link = self.tmp / ".env"
        link.symlink_to(real)
        env = configure.EnvFile(link)
        env.set("A", "2")
        env.save()
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(encoding="utf-8"), "A=2\n")


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _config(self, text: str) -> configure.ConfigFile:
        path = self.tmp / "config.py"
        path.write_text(text, encoding="utf-8")
        return configure.ConfigFile(path)

    def test_classifies_real_config(self):
        cfg = configure.load_config(ROOT)
        s = cfg.settings
        self.assertEqual((s["PLATFORMS"].kind, s["PLATFORMS"].section), ("list", "运行平台"))
        self.assertEqual((s["IMAGE_MAX_BYTES"].kind, s["IMAGE_MAX_BYTES"].value), ("int", 8 * 1024 * 1024))
        self.assertEqual(s["LLM_TIMEOUT"].kind, "float")
        self.assertEqual(s["LLM_THINKING"].kind, "bool")
        self.assertEqual(s["MASTER_QQ"].env_key, "MASTER_QQ")
        self.assertEqual(s["SYSTEM_PROMPT"].kind, "")       # f-string
        self.assertEqual(s["GROUP_GATE_PROMPT"].kind, "")   # 多行文本
        self.assertNotIn("_MASTER_INTRO", s)
        self.assertEqual(set(cfg.platforms), {"onebot", "feishu"})
        for name in configure.COMMON:
            self.assertTrue(s[name].kind, name)

    def test_rewriting_every_setting_keeps_file_valid(self):
        cfg = configure.load_config(ROOT)
        changes = {name: (s.value, configure.py_literal(s.value))
                   for name, s in cfg.settings.items() if s.kind}
        content = cfg.render(changes)  # 内部会重新解析并逐项校验
        self.assertEqual(configure.ConfigFile(cfg.path, content).settings.keys(), cfg.settings.keys())

    def test_replaces_only_value_with_multibyte_text(self):
        cfg = self._config('# 注释\nNAME = "中文"  # 说明：中文\nN = 8 * 1024  # 字节\nL = ["a"]\n')
        changes = {
            "NAME": cfg.parse_value(cfg.settings["NAME"], 'he said "喵"'),
            "N": cfg.parse_value(cfg.settings["N"], "16*1024"),
            "L": cfg.parse_value(cfg.settings["L"], "x，y"),
        }
        cfg.write(cfg.render(changes))
        self.assertEqual(cfg.path.read_text(encoding="utf-8"),
                         '# 注释\nNAME = "he said \\"喵\\""  # 说明：中文\nN = 16 * 1024  # 字节\nL = ["x", "y"]\n')
        self.assertEqual(configure.ConfigFile(cfg.path).settings["NAME"].comment, "说明：中文")

    def test_parse_value(self):
        cfg = self._config('B = True\nI = 3\nF = 0.5\nS = "s"\nL = []\nPLATFORMS = ["onebot"]\n'
                           'NAPCAT_WS_URL = "ws://x"\nIMAGE_DETAIL = "auto"\n')
        s = cfg.settings
        self.assertEqual(cfg.parse_value(s["B"], "否"), (False, "False"))
        self.assertEqual(cfg.parse_value(s["I"], "2 ** 10"), (1024, "2 ** 10"))
        self.assertEqual(cfg.parse_value(s["F"], "1"), (1.0, "1.0"))
        self.assertEqual(cfg.parse_value(s["L"], '["a,b", "c"]'), (["a,b", "c"], '["a,b", "c"]'))
        self.assertEqual(cfg.parse_value(s["L"], ""), ([], "[]"))
        for name, text in (("B", "maybe"), ("I", "1.5"), ("I", "-1"), ("I", "2 ** 1000"), ("I", "1 / 0"),
                           ("F", "1e999"), ("I", "__import__('os')"), ("L", "[1]"), ("S", "a\nb"),
                           ("PLATFORMS", "onebot, wechat"), ("PLATFORMS", ""), ("NAPCAT_WS_URL", "http://x"),
                           ("IMAGE_DETAIL", "huge")):
            with self.assertRaises(ValueError, msg=f"{name}={text!r}"):
                cfg.parse_value(s[name], text)

    def test_duplicate_assignment_is_read_only(self):
        cfg = self._config("A = 1\nA = 2\n")
        self.assertEqual(cfg.settings["A"].kind, "")
        with self.assertRaises(ValueError):
            cfg.parse_value(cfg.settings["A"], "3")


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.root = _root(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(patch.dict(os.environ))
        for key in configure.ENV_BY_NAME:
            os.environ.pop(key, None)

    def test_set_routes_to_env_and_config(self):
        code, out, _ = _run(["--root", str(self.root), "set", "LLM_API_KEY=sk-0123456789abcdefXYZ",
                             "MASTER_QQ=10001", "LLM_MODEL=deepseek-chat", "PLATFORMS=onebot"])
        self.assertEqual(code, 0)
        self.assertNotIn("sk-0123456789abcdefXYZ", out)
        self.assertIn("/restart", out)
        env = envmod.parse((self.root / ".env").read_text(encoding="utf-8"))
        self.assertEqual((env["LLM_API_KEY"], env["MASTER_QQ"]), ("sk-0123456789abcdefXYZ", "10001"))
        cfg = configure.load_config(self.root)
        self.assertEqual(cfg.settings["LLM_MODEL"].value, "deepseek-chat")
        self.assertEqual(cfg.settings["PLATFORMS"].value, ["onebot"])

    def test_set_env_only_reports_change(self):
        code, out, _ = _run(["--root", str(self.root), "set", "MASTER_NAME=主人"])
        self.assertEqual(code, 0)
        self.assertIn("MASTER_NAME", out)
        self.assertNotIn("没有改动", out)
        self.assertEqual((self.root / "config.py").read_bytes(), (ROOT / "config.py").read_bytes())

    def test_set_validates_everything_before_writing(self):
        before = (self.root / "config.py").read_bytes()
        code, _, err = _run(["--root", str(self.root), "set", "LLM_API_KEY=x", "LLM_MODEL=y", "NOPE=1"])
        self.assertEqual(code, 1)
        self.assertIn("NOPE", err)
        self.assertFalse((self.root / ".env").exists())
        self.assertEqual((self.root / "config.py").read_bytes(), before)

    def test_env_wizard(self):
        (self.root / ".env").write_text("LLM_API_KEY=keep\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            configure.main(["--root", str(self.root), "set", "PLATFORMS=onebot"])
        # LLM_API_KEY 保留；搜索关闭；NAPCAT_TOKEN 填写；MASTER_NAME 跳过；MASTER_QQ 先输错再改正；其余保留；确认保存
        answers = ["", "3", "tok", "", "abc", "10001", "", "", "", "y"]
        code, out, _ = _run(["--root", str(self.root), "env"], answers)
        self.assertEqual(code, 0)
        self.assertIn("QQ 号应为纯数字", out)
        self.assertIn("FEISHU_APP_ID", out)  # 未启用 feishu，列在跳过提示里
        env = envmod.parse((self.root / ".env").read_text(encoding="utf-8"))
        self.assertEqual(env, {"LLM_API_KEY": "keep", configure.SEARCH_KEY: "", "NAPCAT_TOKEN": "tok",
                               "MASTER_QQ": "10001"})

    def test_config_menu(self):
        # 1 = PLATFORMS；LLM_MODEL 按名字选；先 q 但不放弃，再保存
        answers = ["1", "onebot", "llm_model", "deepseek-chat", "q", "n", "s", "y"]
        code, _, _ = _run(["--root", str(self.root), "config"], answers)
        self.assertEqual(code, 0)
        cfg = configure.load_config(self.root)
        self.assertEqual(cfg.settings["PLATFORMS"].value, ["onebot"])
        self.assertEqual(cfg.settings["LLM_MODEL"].value, "deepseek-chat")

    def test_cancel_discards_changes(self):
        before = (self.root / "config.py").read_bytes()
        code, _, err = _run(["--root", str(self.root), "config"], ["1", "onebot", EOFError()])  # Ctrl-D
        self.assertEqual(code, 130)
        self.assertIn("已取消", err)
        self.assertEqual((self.root / "config.py").read_bytes(), before)

    def test_show_masks_and_checks(self):
        (self.root / ".env").write_text("LLM_API_KEY=sk-0123456789abcdefXYZ\nMASTER_QQ=abc\n", encoding="utf-8")
        code, out, _ = _run(["--root", str(self.root), "show"])
        self.assertEqual(code, 0)
        self.assertNotIn("sk-0123456789abcdefXYZ", out)
        self.assertIn("MASTER_QQ 应为纯数字", out)
        self.assertNotIn("未填写 LLM_API_KEY", out)


if __name__ == "__main__":
    unittest.main()
