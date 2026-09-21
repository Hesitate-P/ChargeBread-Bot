"""commands.py 的期望契约（TDD：先写测试，再实现）。

参数策略是用户拍定的"宽进"：多个同义词都认，打错字静默回落到默认（本群），
不因为一个错别字就把人挡在门外 —— 娱乐机器人不该有这种脾气。

有两个真实世界的细节来自实测：
  · 群里 @机器人 之后，content 是 `' 充能面包 '` —— **前后带空格**，
    @ 前缀被平台剥掉了但空格留着，所以必须先 strip。
  · 手机上容易打出全角斜杠 `／`，要认。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread import commands  # noqa: E402


class ParseTextTest(unittest.TestCase):
    def test_signin_with_surrounding_spaces(self) -> None:
        """实测：事件里的 content 是 ' 充能面包 '，不 strip 就会漏匹配。"""
        cmd = commands.parse(" 充能面包 ")
        self.assertEqual(cmd.name, commands.SIGNIN)

    def test_signin_with_leading_slash(self) -> None:
        self.assertEqual(commands.parse("/充能面包").name, commands.SIGNIN)

    def test_signin_with_fullwidth_slash(self) -> None:
        """手机输入法容易打出全角斜杠。"""
        self.assertEqual(commands.parse("／充能面包").name, commands.SIGNIN)

    def test_bare_at_means_help(self) -> None:
        """空 @ 显示菜单。"""
        self.assertEqual(commands.parse("").name, commands.HELP)
        self.assertEqual(commands.parse("   ").name, commands.HELP)

    def test_help_aliases(self) -> None:
        for text in ("/帮助", "帮助", "/help", "help", "/菜单", "菜单"):
            self.assertEqual(commands.parse(text).name, commands.HELP, text)

    def test_profile(self) -> None:
        for text in ("/我的面包", "我的面包"):
            self.assertEqual(commands.parse(text).name, commands.PROFILE, text)

    def test_makeup(self) -> None:
        for text in ("/补签", "补签"):
            self.assertEqual(commands.parse(text).name, commands.MAKEUP, text)

    def test_unknown_command(self) -> None:
        self.assertEqual(commands.parse("/不存在的命令").name, commands.UNKNOWN)
        self.assertEqual(commands.parse("今天天气不错").name, commands.UNKNOWN)


class ParseScopeTest(unittest.TestCase):
    def test_defaults_to_group_scope(self) -> None:
        cmd = commands.parse("/面包排行榜")
        self.assertEqual(cmd.name, commands.BOARD_BREAD)
        self.assertEqual(cmd.scope, commands.SCOPE_GROUP)

    def test_accepts_synonyms_for_group(self) -> None:
        for token in ("本群", "群内", "本群榜", "group"):
            cmd = commands.parse(f"/面包排行榜 {token}")
            self.assertEqual(cmd.scope, commands.SCOPE_GROUP, token)

    def test_accepts_synonyms_for_all(self) -> None:
        for token in ("全部", "全服", "全部群", "所有", "all"):
            cmd = commands.parse(f"/面包排行榜 {token}")
            self.assertEqual(cmd.scope, commands.SCOPE_ALL, token)

    def test_typo_falls_back_to_default_silently(self) -> None:
        """打错字静默回落默认，不报错、不提示用法。"""
        for token in ("全布", "quanbu", "???"):
            cmd = commands.parse(f"/面包排行榜 {token}")
            self.assertEqual(cmd.scope, commands.SCOPE_GROUP, token)
            self.assertEqual(cmd.name, commands.BOARD_BREAD)

    def test_extra_spaces_between_command_and_arg(self) -> None:
        cmd = commands.parse("  /面包排行榜    全部  ")
        self.assertEqual(cmd.scope, commands.SCOPE_ALL)

    def test_signin_board(self) -> None:
        self.assertEqual(commands.parse("/签到排行榜").name, commands.BOARD_SIGNIN)
        cmd = commands.parse("/签到排行榜 全部")
        self.assertEqual(cmd.name, commands.BOARD_SIGNIN)
        self.assertEqual(cmd.scope, commands.SCOPE_ALL)

    def test_command_accepts_scope_without_slash_and_with_alias(self) -> None:
        self.assertEqual(commands.parse("面包榜 全部").scope, commands.SCOPE_ALL)
        self.assertEqual(commands.parse("签到榜").name, commands.BOARD_SIGNIN)

    def test_scope_only_matters_for_boards(self) -> None:
        cmd = commands.parse("/我的面包 全部")
        self.assertEqual(cmd.name, commands.PROFILE)


class ParseCallbackTest(unittest.TestCase):
    """按钮回调的 data 也要走同一套解析，这样按钮和命令不会两套逻辑。"""

    def test_board_callback_all_scope(self) -> None:
        cmd = commands.parse_callback("board:all:bread")
        self.assertEqual(cmd.name, commands.BOARD_BREAD)
        self.assertEqual(cmd.scope, commands.SCOPE_ALL)

    def test_board_callback_group_scope(self) -> None:
        cmd = commands.parse_callback("board:group:bread")
        self.assertEqual(cmd.name, commands.BOARD_BREAD)
        self.assertEqual(cmd.scope, commands.SCOPE_GROUP)

    def test_board_callback_signin_kind(self) -> None:
        cmd = commands.parse_callback("board:group:signin")
        self.assertEqual(cmd.name, commands.BOARD_SIGNIN)
        self.assertEqual(cmd.scope, commands.SCOPE_GROUP)

    def test_garbage_callback_is_unknown(self) -> None:
        for data in ("", "garbage", "board", "board:all", "board:all:bread:extra", "probe:callback"):
            self.assertEqual(commands.parse_callback(data).name, commands.UNKNOWN, data)


class CommandDataclassTest(unittest.TestCase):
    def test_raw_is_preserved_for_logging(self) -> None:
        cmd = commands.parse("  /面包排行榜 全部 ")
        self.assertEqual(cmd.raw, "/面包排行榜 全部")


if __name__ == "__main__":
    unittest.main(verbosity=2)
