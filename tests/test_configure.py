import contextlib
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
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

    def _config(self, defaults: str, local: str | None = None) -> configure.Config:
        (self.tmp / "config.py").write_text(defaults, encoding="utf-8")
        if local is not None:
            (self.tmp / "config.local.py").write_text(local, encoding="utf-8")
        return configure.Config(self.tmp)

    def test_classifies_real_config(self):
        cfg = configure.load_config(ROOT)
        s = cfg.defaults.settings
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
            self.assertEqual(cfg.blocked(name), "", name)

    def test_copying_every_default_into_local_file_is_valid(self):
        cfg = configure.Config(ROOT)
        local = configure.ConfigFile(self.tmp / "config.local.py")
        changes = {name: cfg.defaults.segment(s.node) for name, s in cfg.defaults.settings.items()}
        content = local.edit(changes, configure.LOCAL_HEADER)  # 内部会重新解析并逐项校验
        self.assertTrue(content.startswith(configure.LOCAL_HEADER.encode()))
        self.assertEqual(configure.ConfigFile(local.path, content).settings.keys(), cfg.defaults.settings.keys())

    def test_edit_replaces_deletes_and_appends_keeping_comments(self):
        local = configure.ConfigFile(self.tmp / "config.local.py",
                                     '﻿# 注释\nNAME = "中文"  # 说明：中文\nN = 8 * 1024  # 字节\nOLD = [\n    "x",\n]\n'
                                     .encode())
        content = local.edit({"NAME": '"he said \\"喵\\""', "N": "16 * 1024", "OLD": None, "NEW": '["a"]'})
        self.assertEqual(content.decode(), '# 注释\nNAME = "he said \\"喵\\""  # 说明：中文\nN = 16 * 1024  # 字节\n'
                                           'NEW = ["a"]\n')
        self.assertEqual(configure.ConfigFile(local.path, content).settings["NAME"].comment, "说明：中文")
        shared = configure.ConfigFile(local.path, b"A = 1; B = 2\n")
        with self.assertRaises(RuntimeError):  # 删除会连带同一行的 B，校验发现后拒绝写入
            shared.edit({"A": None})

    def test_parse_value(self):
        cfg = self._config('B = True\nI = 3\nF = 0.5\nS = "s"\nL = []\nPLATFORMS = ["onebot"]\n'
                           'NAPCAT_WS_URL = "ws://x"\nIMAGE_DETAIL = "auto"\n')
        self.assertEqual(cfg.parse_value("B", "否"), (False, "False"))
        self.assertEqual(cfg.parse_value("I", "2 ** 10"), (1024, "2 ** 10"))
        self.assertEqual(cfg.parse_value("F", "1"), (1.0, "1.0"))
        self.assertEqual(cfg.parse_value("L", '["a,b", "c"]'), (["a,b", "c"], '["a,b", "c"]'))
        self.assertEqual(cfg.parse_value("L", "x，y"), (["x", "y"], '["x", "y"]'))
        self.assertEqual(cfg.parse_value("L", ""), ([], "[]"))
        for name, text in (("B", "maybe"), ("I", "1.5"), ("I", "-1"), ("I", "2 ** 1000"), ("I", "1 / 0"),
                           ("F", "1e999"), ("I", "__import__('os')"), ("L", "[1]"), ("S", "a\nb"),
                           ("PLATFORMS", "onebot, wechat"), ("PLATFORMS", ""), ("NAPCAT_WS_URL", "http://x"),
                           ("IMAGE_DETAIL", "huge")):
            with self.assertRaises(ValueError, msg=f"{name}={text!r}"):
                cfg.parse_value(name, text)

    def test_effective_values_and_changes(self):
        cfg = self._config('A = 1\nB = "b"\nC = [1]\nD = 2\nP = f"{A}"\n',
                           'A = 5\nB = B + "x"\nif True:\n    D = 3\n')
        self.assertEqual((cfg.value("A"), cfg.value("B"), cfg.value("D")), (5, None, None))
        self.assertIn("[本地]", cfg.describe("A"))
        self.assertEqual(cfg.change("A", 1, "1"), None)                # 等于默认值：删除本地覆盖
        self.assertIs(cfg.change("A", 5, "5"), configure.KEEP)         # 等于当前本地值：不用改
        self.assertEqual(cfg.change("A", 7, "7"), "7")
        self.assertEqual(cfg.blocked("A"), "")
        self.assertEqual(cfg.blocked("B"), "")                         # 本地是表达式，可以换成常量
        self.assertIn("复杂", cfg.blocked("D"))
        self.assertIn("暂不支持", cfg.blocked("C"))
        self.assertIn("手动赋值", cfg.blocked("P"))

    def test_duplicate_assignment_is_read_only(self):
        cfg = self._config("A = 1\nA = 2\n")
        with self.assertRaises(ValueError):
            cfg.parse_value("A", "3")


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.root = _root(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(patch.dict(os.environ))
        for key in configure.ENV_BY_NAME:
            os.environ.pop(key, None)
        self.defaults = (self.root / "config.py").read_bytes()

    def tearDown(self):
        # 本工具从不修改随代码更新的 config.py
        self.assertEqual((self.root / "config.py").read_bytes(), self.defaults)

    def local(self) -> str:
        path = self.root / "config.local.py"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_set_routes_to_env_and_local_config(self):
        code, out, _ = _run(["--root", str(self.root), "set", "LLM_API_KEY=sk-0123456789abcdefXYZ",
                             "MASTER_QQ=10001", "LLM_MODEL=deepseek-chat", "PLATFORMS=onebot", "LLM_TEMPERATURE=0.7"])
        self.assertEqual(code, 0)
        self.assertNotIn("sk-0123456789abcdefXYZ", out)
        self.assertIn("/restart", out)
        env = envmod.parse((self.root / ".env").read_text(encoding="utf-8"))
        self.assertEqual((env["LLM_API_KEY"], env["MASTER_QQ"]), ("sk-0123456789abcdefXYZ", "10001"))
        self.assertTrue(self.local().startswith(configure.LOCAL_HEADER))
        self.assertTrue(self.local().endswith('LLM_MODEL = "deepseek-chat"\nPLATFORMS = ["onebot"]\n'))  # 默认值不写
        cfg = configure.load_config(self.root)
        self.assertEqual((cfg.value("LLM_MODEL"), cfg.value("PLATFORMS")), ("deepseek-chat", ["onebot"]))
        self.assertEqual(stat.S_IMODE((self.root / "config.local.py").stat().st_mode),
                         stat.S_IMODE((self.root / "config.py").stat().st_mode))

    def test_default_value_and_unset_remove_local_override(self):
        _run(["--root", str(self.root), "set", "LLM_MODEL=x", "PLATFORMS=onebot", "MASTER_QQ=1"])
        _run(["--root", str(self.root), "set", "LLM_MODEL=deepseek-flash"])
        self.assertNotIn("LLM_MODEL", self.local())
        code, out, _ = _run(["--root", str(self.root), "unset", "PLATFORMS", "MASTER_QQ", "NOPE"])
        self.assertEqual(code, 0)
        self.assertIn("NOPE 本来就未设置", out)
        self.assertNotIn("PLATFORMS", self.local())
        self.assertNotIn("MASTER_QQ", envmod.parse((self.root / ".env").read_text(encoding="utf-8")))

    def test_set_env_only_reports_change(self):
        code, out, _ = _run(["--root", str(self.root), "set", "MASTER_NAME=主人"])
        self.assertEqual(code, 0)
        self.assertIn("MASTER_NAME", out)
        self.assertNotIn("没有改动", out)
        self.assertFalse((self.root / "config.local.py").exists())

    def test_set_validates_everything_before_writing(self):
        code, _, err = _run(["--root", str(self.root), "set", "LLM_API_KEY=x", "LLM_MODEL=y", "NOPE=1"])
        self.assertEqual(code, 1)
        self.assertIn("NOPE", err)
        self.assertFalse((self.root / ".env").exists())
        self.assertFalse((self.root / "config.local.py").exists())
        code, _, err = _run(["--root", str(self.root), "set", "SYSTEM_PROMPT=x"])
        self.assertIn("手动赋值", err)

    def test_env_wizard(self):
        (self.root / ".env").write_text("LLM_API_KEY=keep\n", encoding="utf-8")
        _run(["--root", str(self.root), "set", "PLATFORMS=onebot"])
        # LLM_API_KEY 保留；搜索关闭；NAPCAT_TOKEN 填写；MASTER_NAME 跳过；MASTER_QQ 先输错再改正；其余保留；确认保存
        answers = ["", "3", "tok", "", "abc", "10001", "", "", "", "y"]
        code, out, _ = _run(["--root", str(self.root), "env"], answers)
        self.assertEqual(code, 0)
        self.assertIn("QQ 号应为纯数字", out)
        self.assertIn("FEISHU_APP_ID", out)  # 本地配置只启用了 onebot，飞书项列在跳过提示里
        env = envmod.parse((self.root / ".env").read_text(encoding="utf-8"))
        self.assertEqual(env, {"LLM_API_KEY": "keep", configure.SEARCH_KEY: "", "NAPCAT_TOKEN": "tok",
                               "MASTER_QQ": "10001"})

    def test_config_menu(self):
        # 1 = PLATFORMS；LLM_MODEL 按名字选；SYSTEM_PROMPT 复制默认写法；先 q 但不放弃，再保存
        answers = ["1", "onebot", "llm_model", "deepseek-chat", "system_prompt", "y", "q", "n", "s", "y"]
        code, _, _ = _run(["--root", str(self.root), "config"], answers)
        self.assertEqual(code, 0)
        cfg = configure.load_config(self.root)
        self.assertEqual((cfg.value("PLATFORMS"), cfg.value("LLM_MODEL")), (["onebot"], "deepseek-chat"))
        self.assertIn('SYSTEM_PROMPT = f"""你是一个可爱的', self.local())
        # 再次进入：! 恢复默认
        code, _, _ = _run(["--root", str(self.root), "config"], ["1", "!", "s", "y"])
        self.assertNotIn("PLATFORMS", self.local())

    def test_cancel_discards_changes(self):
        code, _, err = _run(["--root", str(self.root), "config"], ["1", "onebot", EOFError()])  # Ctrl-D
        self.assertEqual(code, 130)
        self.assertIn("已取消", err)
        self.assertFalse((self.root / "config.local.py").exists())

    def test_show_masks_and_checks(self):
        (self.root / ".env").write_text("LLM_API_KEY=sk-0123456789abcdefXYZ\nMASTER_QQ=abc\n", encoding="utf-8")
        (self.root / "config.local.py").write_text('LLM_MODLE = "typo"\nAMBIENT_CHAT = False\n', encoding="utf-8")
        code, out, _ = _run(["--root", str(self.root), "show"])
        self.assertEqual(code, 0)
        self.assertNotIn("sk-0123456789abcdefXYZ", out)
        self.assertIn("MASTER_QQ 应为纯数字", out)
        self.assertIn("LLM_MODLE 不是 config.py 的配置项", out)
        self.assertNotIn("AMBIENT_CHAT 已开启", out)  # 检查按本地覆盖后的值
        self.assertNotIn("未填写 LLM_API_KEY", out)

    def test_migrate_stashed_config_edits(self):
        def git(*args):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
        git("add", "config.py")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "config")
        edited = self.defaults.decode().replace('LLM_MODEL = "deepseek-flash"', 'LLM_MODEL = "mine"')
        (self.root / "config.py").write_text(edited, encoding="utf-8")
        git("-c", "user.name=t", "-c", "user.email=t@t", "stash", "push", "-q", "--", "config.py")
        code, out, _ = _run(["--root", str(self.root), "migrate"], ["y"])
        self.assertEqual(code, 0, out)
        lines = [line for line in self.local().splitlines() if line and not line.startswith("#")]
        self.assertEqual(lines, ['LLM_MODEL = "mine"'])  # 只迁移改过的项
        code, _, err = _run(["--root", str(self.root), "migrate", "--ref", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("git stash push", err)


if __name__ == "__main__":
    unittest.main()
