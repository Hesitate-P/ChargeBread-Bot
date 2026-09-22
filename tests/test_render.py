"""render.py 的期望契约（TDD：先写测试，再实现）。

这些测试同时充当"QQ 群聊 markdown 能力"的约束检查，因为实测确认过：
  · 只支持 # 和 ## 两级标题，不支持表格、不支持代码块（所以这里断言不出现表格）
  · 图片语法 ![alt #Wpx #Hpx](url)，且**前面必须换行**，否则文字和图片会挤在一起
  · 按钮 label 最多 10 个字，最多 5 行 × 每行 5 个，且 markdown 内容是必填的
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.db import BoardEntry  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")

from chargebread.render import (  # noqa: E402
    MeStanding,
    already_text,
    board_text,
    makeup_text,
    mention_tag,
    menu_text,
    need_signin_text,
    profile_text,
    safe_name,
    signin_text,
    welcome_text,
    kb_board,
    kb_makeup,
    kb_menu,
    kb_profile,
    kb_signin,
    kb_welcome,
)

IMAGE = "https://drive.example.tech:8443/sd/public/ChargeBread/ChargeBread.png"


def entries(*rows: tuple[str, str, int]) -> list[BoardEntry]:
    return [BoardEntry(uid, name, value) for uid, name, value in rows]


class KeyboardTest(unittest.TestCase):
    """把 QQ 对按钮的硬限制钉住 —— 超限会被平台拒绝（错误码 40034029）。"""

    def _check(self, kb: dict) -> None:
        rows = kb["content"]["rows"]
        self.assertLessEqual(len(rows), 5, "最多 5 行")
        for row in rows:
            self.assertLessEqual(len(row["buttons"]), 5, "每行最多 5 个按钮")
            for btn in row["buttons"]:
                self.assertLessEqual(
                    len(btn["render_data"]["label"]), 10, "label 最多 10 个字"
                )
                self.assertTrue(btn["id"], "按钮必须有 id")
                # permission.type=2 表示所有人可点
                self.assertEqual(btn["action"]["permission"]["type"], 2)
                self.assertIn(btn["action"]["type"], (1, 2), "只用回调按钮或指令按钮")

    def test_all_keyboards_respect_platform_limits(self) -> None:
        for kb in (
            kb_signin(),
            kb_menu(),
            kb_profile(),
            kb_board("group", "bread"),
            kb_board("all", "signin"),
        ):
            self._check(kb)

    def test_board_keyboard_offers_scope_switch_and_refresh(self) -> None:
        kb = kb_board("group", "bread")
        payloads = [
            b["action"]["data"] for row in kb["content"]["rows"] for b in row["buttons"]
        ]
        self.assertIn("board:all:bread", payloads, "应能切到全部榜")
        self.assertIn("board:group:bread", payloads, "应能切回本群榜")

    def test_board_keyboard_switches_between_bread_and_signin(self) -> None:
        kb = kb_board("group", "bread")
        payloads = [
            b["action"]["data"] for row in kb["content"]["rows"] for b in row["buttons"]
        ]
        self.assertIn("board:group:signin", payloads, "应能切到签到榜")

    def test_signin_keyboard_uses_command_buttons_for_commands(self) -> None:
        """命令类按钮用 type=2（替用户把命令打进输入框），避免额外回调。"""
        kb = kb_signin()
        types = {
            b["action"]["data"]: b["action"]["type"]
            for row in kb["content"]["rows"]
            for b in row["buttons"]
        }
        self.assertEqual(types["补签"], 2)
        self.assertEqual(types["我的面包"], 2)

    def test_signin_keyboard_offers_signin_for_bystanders(self) -> None:
        """签到成功的消息群里其他人也看得到。

        给他们一个「签到」按钮，点一下就把命令填进输入框，不用记命令名 ——
        所以它该排在最前面（最显眼、最好按）。
        """
        first = kb_signin()["content"]["rows"][0]["buttons"][0]
        self.assertEqual(first["render_data"]["label"], "签到")
        self.assertEqual(first["action"]["data"], "充能面包", "填进输入框的是 data")
        self.assertEqual(first["action"]["type"], 2)

    def test_no_keyboard_leaves_a_lone_button_on_its_own_row(self) -> None:
        """多排键盘里，每一排至少两个按钮。

        单开一排只放一个按钮看着很空（补签曾经自己占一排，实测反馈过），
        而且白白多占一行屏幕。
        """
        keyboards = {
            "kb_signin": kb_signin(),
            "kb_menu": kb_menu(),
            "kb_profile": kb_profile(),
        }
        for name, kb in keyboards.items():
            rows = kb["content"]["rows"]
            if len(rows) < 2:
                continue
            for index, row in enumerate(rows, 1):
                self.assertGreaterEqual(
                    len(row["buttons"]), 2, f"{name} 第 {index} 排只有 1 个按钮"
                )

    def test_makeup_keyboard_offers_signin(self) -> None:
        """补签成功也要给「签到」按钮 —— 补完签顺手把今天的也签了。"""
        first = kb_makeup()["content"]["rows"][0]["buttons"][0]
        self.assertEqual(first["render_data"]["label"], "签到")
        self.assertEqual(first["action"]["data"], "充能面包")
        self.assertEqual(first["action"]["type"], 2)

    def test_makeup_keyboard_has_no_dead_makeup_button(self) -> None:
        """刚补完签，再放一个「补签」按钮是死的：只能补昨天，而且刚补过，
        点下去只会得到"不需要补签"。所以这个键盘里不放它。"""
        labels = [
            b["render_data"]["label"]
            for row in kb_makeup()["content"]["rows"]
            for b in row["buttons"]
        ]
        self.assertNotIn("补签", labels)
        self.assertIn("签到", labels)
        self.assertIn("我的面包", labels)

    def test_profile_keyboard_leads_with_signin(self) -> None:
        """实测反馈：我的面包页第一个按钮该是「签到」，不是「补签」。"""
        first = kb_profile()["content"]["rows"][0]["buttons"][0]
        self.assertEqual(first["render_data"]["label"], "签到")
        self.assertEqual(first["action"]["type"], 2)

    def test_profile_keyboard_keeps_makeup_and_both_boards(self) -> None:
        labels = [
            b["render_data"]["label"]
            for row in kb_profile()["content"]["rows"]
            for b in row["buttons"]
        ]
        self.assertEqual(labels[0], "签到")
        self.assertIn("补签", labels)
        self.assertIn("面包排行榜", labels)
        self.assertIn("签到排行榜", labels)
        self.assertEqual(len(labels), 4, "加了签到之后共四个按钮")

    def test_every_command_button_inserts_a_parseable_command(self) -> None:
        """type=2 按钮点击后是把 action.data 填进输入框，所以 **data 必须是
        解析器认得的命令**。标签可以为了好看而不同于 data（如标签「签到」、
        data「充能面包」），但 data 错了按钮就是死的。"""
        from chargebread import commands

        keyboards = {
            "kb_signin": kb_signin(),
            "kb_menu": kb_menu(),
            "kb_profile": kb_profile(),
            "kb_board": kb_board("group", "bread"),
        }
        for name, kb in keyboards.items():
            for row in kb["content"]["rows"]:
                for btn in row["buttons"]:
                    if btn["action"]["type"] != 2:
                        continue
                    data = btn["action"]["data"]
                    parsed = commands.parse(data)
                    self.assertNotEqual(
                        parsed.name, commands.UNKNOWN, f"{name} 里的按钮 data 解析不了: {data!r}"
                    )


class SigninTextTest(unittest.TestCase):
    def test_contains_rank_bread_and_image(self) -> None:
        text = signin_text(
            nickname="小明", rank=3, bread=16, streak=5,
            total_bread=142, total_today=17, image_url=IMAGE,
        )
        self.assertIn("小明", text)
        self.assertIn("3", text, "要告诉他是今天第几个签到的")
        self.assertIn("16", text, "要告诉他拿了多少面包")
        self.assertIn(IMAGE, text, "要带上充能面包的图片")

    def test_image_is_on_its_own_line(self) -> None:
        """实测坑：图片前不换行的话，文字和图片会挤在一起。"""
        text = signin_text(
            nickname="小明", rank=1, bread=15, streak=1,
            total_bread=15, total_today=1, image_url=IMAGE,
        )
        image_line = [ln for ln in text.split("\n") if IMAGE in ln][0]
        self.assertTrue(image_line.strip().startswith("!"), "图片应单独占一行")
        idx = text.split("\n").index(image_line)
        self.assertTrue(text.split("\n")[idx - 1].strip() == "", "图片前应有空行")

    def test_first_place_is_celebrated(self) -> None:
        first = signin_text(
            nickname="小明", rank=1, bread=15, streak=1,
            total_bread=15, total_today=1, image_url=IMAGE,
        )
        other = signin_text(
            nickname="小明", rank=8, bread=11, streak=1,
            total_bread=11, total_today=9, image_url=IMAGE,
        )
        self.assertIn("🥇", first)
        self.assertNotIn("🥇", other)

    def test_medal_goes_at_the_end_of_the_sentence(self) -> None:
        """实测建议：奖牌放句尾读着更像一句话，'今天第3个签到🥉'。"""
        text = signin_text(
            nickname="smile", rank=3, bread=16, streak=5,
            total_bread=142, total_today=17, image_url=IMAGE,
        )
        self.assertIn("**smile** 今天第3个签到🥉", text)

    def test_late_ranks_do_not_get_a_stray_ordinal(self) -> None:
        """第 4 名以后没有奖牌，但**也不能把 "8." 这种序号混进句子里** ——
        原来的实现会给第 8 名渲染出「今天第 8. **8** 个签到」。"""
        text = signin_text(
            nickname="smile", rank=8, bread=11, streak=1,
            total_bread=11, total_today=9, image_url=IMAGE,
        )
        self.assertIn("**smile** 今天第8个签到", text)
        self.assertNotIn("8.", text)

    def test_streak_shown_only_when_meaningful(self) -> None:
        with_streak = signin_text(
            nickname="小明", rank=2, bread=14, streak=9,
            total_bread=100, total_today=5, image_url=IMAGE,
        )
        self.assertIn("9", with_streak)
        self.assertIn("连签", with_streak)

    def test_no_tables_or_code_fences(self) -> None:
        """群聊 markdown 不支持表格和代码块，谁写进去谁就是坏消息。"""
        for text in (
            signin_text(nickname="小明", rank=1, bread=15, streak=1, total_bread=15, total_today=1, image_url=IMAGE),
            already_text(nickname="小明", rank=2, bread=12, streak=3, total_bread=50),
            makeup_text(target_date="2026-09-20", bread=9, streak=8, total_bread=59, cards_balance=0),
            board_text(
                title="面包排行榜", entries=entries(("a", "甲", 30)), scope="group",
                scope_label="本群", value_suffix=" 个面包", me=None,
            ),
            profile_text(
                nickname="小明", rank=3, total_bread=142, total_today=17, streak=5,
                best_streak=11, signin_count=48, cards=1, days_to_card=2,
            ),
            menu_text(),
        ):
            self.assertNotIn("|", text, "不要用表格")
            self.assertNotIn("```", text, "不要用代码块")


class AlreadySignedTest(unittest.TestCase):
    def test_tells_him_he_already_signed_and_his_rank(self) -> None:
        text = already_text(nickname="小明", rank=2, bread=14, streak=4, total_bread=88)
        self.assertIn("小明", text)
        self.assertIn("2", text)
        self.assertIn("14", text)


class BoardTest(unittest.TestCase):
    def test_top_three_get_medals_others_get_numbers(self) -> None:
        text = board_text(
            title="面包排行榜",
            entries=entries(("a", "甲", 30), ("b", "乙", 20), ("c", "丙", 10), ("d", "丁", 5)),
            scope="group", scope_label="本群", value_suffix=" 个面包", me=None,
        )
        self.assertIn("🥇", text)
        self.assertIn("🥈", text)
        self.assertIn("🥉", text)
        self.assertIn("4", text, "第 4 名用数字")
        self.assertIn("丁", text)

    def test_rank_prefix_is_number_plus_medal(self) -> None:
        """实测建议：序号与奖牌并存。

        原来前三名只有奖牌、第 4 名突然变成 "4."，视觉上断裂；
        "1、🥇" 既能一眼数出名次，又保留奖牌。
        """
        text = board_text(
            title="面包排行榜",
            entries=entries(
                ("a", "Hoshino iChiKa", 15),
                ("b", "Tascota", 14),
                ("c", "smile", 13),
                ("d", "Hesitate_P", 11),
            ),
            scope="group", scope_label="本群", value_suffix=" 个🍞", me=None,
        )
        self.assertIn("1、🥇 **Hoshino iChiKa** — 15 个🍞", text)
        self.assertIn("2、🥈 **Tascota** — 14 个🍞", text)
        self.assertIn("3、🥉 **smile** — 13 个🍞", text)
        self.assertIn("4、 **Hesitate_P** — 11 个🍞", text)

    def test_no_line_looks_like_a_markdown_list_item(self) -> None:
        """榜单的每一行都不能长得像 markdown 有序列表项。

        `数字.` + 空格是 CommonMark 的列表标记，渲染器会**自动重新编号**，
        并在列表被打断时从头开始。曾经 `1.🥇 `（无空格）与 `4. `（有空格）混用，
        于是同一个榜单一半是段落、一半是列表，不同客户端显示完全不同：
        安卓端后半段从 1 重编，另一个客户端合成 1–7（实测反馈）。
        """
        import re

        text = board_text(
            title="面包排行榜",
            entries=entries(
                ("a", "甲", 30), ("b", "乙", 20), ("c", "丙", 10),
                ("d", "丁", 8), ("e", "戊", 5), ("f", "己", 3),
            ),
            scope="group", scope_label="本群", value_suffix=" 个🍞",
            me=MeStanding("z", "自己", 1, 9),
        )
        for line in text.split("\n"):
            self.assertIsNone(
                re.match(r"^\s*\d+[.)]\s", line),
                f"这行会被渲染器当列表项重新编号: {line!r}",
            )

    def test_signin_board_lines_are_not_list_items_either(self) -> None:
        import re

        text = board_text(
            title="签到排行榜",
            entries=[
                BoardEntry("a", "甲", 1, signed_at="2026-09-21T08:03:12+08:00"),
                BoardEntry("b", "乙", 2, signed_at="2026-09-21T08:05:47+08:00"),
            ],
            scope="group", scope_label="今天 · 本群", value_suffix="", me=None, tz=TZ,
        )
        for line in text.split("\n"):
            self.assertIsNone(re.match(r"^\s*\d+[.)]\s", line), repr(line))


    def test_shows_scope_label(self) -> None:
        group = board_text(title="面包排行榜", entries=entries(("a", "甲", 1)), scope="group", scope_label="本群", value_suffix=" 个面包", me=None)
        alls = board_text(title="面包排行榜", entries=entries(("a", "甲", 1)), scope="all", scope_label="全部", value_suffix=" 个面包", me=None)
        self.assertIn("本群", group)
        self.assertIn("全部", alls)

    def test_appends_me_when_not_in_top_list(self) -> None:
        """自己不在榜上时，底部单列一行——比翻页重要得多。

        格式用「第 N 名 ·」而不是名次徽章：徽章的 "47." 接在"你："后面读着断。
        """
        text = board_text(
            title="面包排行榜",
            entries=entries(("a", "甲", 30), ("b", "乙", 20)),
            scope="all", scope_label="全部", value_suffix=" 个面包",
            me=MeStanding(user_id="z", nickname="小明", value=7, rank=47),
        )
        self.assertIn("你：第 47 名 · **小明** — 7 个面包", text)

    def test_does_not_duplicate_me_when_already_listed(self) -> None:
        text = board_text(
            title="面包排行榜",
            entries=entries(("z", "小明", 30), ("b", "乙", 20)),
            scope="all", scope_label="全部", value_suffix=" 个面包",
            me=MeStanding(user_id="z", nickname="小明", value=30, rank=1),
        )
        self.assertEqual(text.count("小明"), 1, "已在榜上就不该再单列一次")

    def test_empty_board_says_so(self) -> None:
        text = board_text(title="签到排行榜", entries=[], scope="group", scope_label="本群", value_suffix="", me=None)
        self.assertTrue(any(k in text for k in ("还没有", "暂无", "没人")), "空榜要有友好文案")

    def test_signin_board_shows_clock_time_instead_of_order_number(self) -> None:
        """签到顺序用**签到时刻**表达，比"第几个"清楚，也免去本群/全部序号不一致的困惑。"""
        text = board_text(
            title="签到排行榜",
            entries=[
                BoardEntry("a", "Hoshino iChiKa", 1, signed_at="2026-09-21T08:03:12+08:00"),
                BoardEntry("b", "Tascota", 2, signed_at="2026-09-21T08:05:47+08:00"),
                BoardEntry("c", "smile", 3, signed_at="2026-09-21T09:12:31+08:00"),
            ],
            scope="all",
            scope_label="今天 · 全部",
            value_suffix="",
            me=None,
        )
        self.assertIn("08:03:12", text)
        self.assertIn("08:05:47", text)
        self.assertIn("09:12:31", text)
        self.assertNotIn("第 1 个", text)

    def test_clock_time_is_rendered_in_bot_timezone(self) -> None:
        """事件时间戳带 +08:00，展示要按机器人时区，不能直接切字符串。"""
        text = board_text(
            title="签到排行榜",
            entries=[BoardEntry("a", "甲", 1, signed_at="2026-09-21T00:03:12+00:00")],
            scope="all",
            scope_label="今天 · 全部",
            value_suffix="",
            me=None,
            tz=TZ,
        )
        self.assertIn("08:03:12", text, "UTC 00:03:12 应显示为北京时间 08:03:12")

    def test_bread_board_keeps_quantity_suffix(self) -> None:
        text = board_text(
            title="面包排行榜",
            entries=entries(("a", "甲", 318)),
            scope="group",
            scope_label="本群",
            value_suffix=" 个🍞",
            me=None,
        )
        self.assertIn("318 个🍞", text)

    def test_signin_board_falls_back_readably_without_a_time(self) -> None:
        """迁移前的旧记录没有 signin_at，兜底不能显示成光秃秃的 "2"。

        没有时间又没有单位时退回「第N个」—— 不如时刻清楚，但至少读得懂。
        """
        text = board_text(
            title="签到排行榜",
            entries=[BoardEntry("a", "甲", 2, signed_at=None)],
            scope="all",
            scope_label="今天 · 全部",
            value_suffix="",
            me=None,
        )
        self.assertNotIn("— 2\n", text, "不该出现裸数字")
        self.assertIn("第2个", text)


class ProfileTest(unittest.TestCase):
    def test_shows_everything_asked_for(self) -> None:
        text = profile_text(
            nickname="小明", rank=3, total_bread=142, total_today=17, streak=5,
            best_streak=11, signin_count=48, cards=1, days_to_card=2,
        )
        for expect in ("小明", "142", "3", "5", "11", "48", "1"):
            self.assertIn(expect, text)


class MarkdownStructureAuditTest(unittest.TestCase):
    """所有对外消息都要能被不同客户端**一致**渲染。

    实测教训（两张客户端截图对比）：`数字.` + 空格是 CommonMark 的有序列表
    标记，渲染器会给它**自动重新编号**，而且在列表被空行打断时从头开始。
    我们曾把 `1.🥇 `（无空格，不算列表项）与 `4. `（有空格，算列表项）混用，
    于是同一个榜单一半是段落、一半是列表 —— 安卓端后半段从 1 重编，
    另一个客户端合成 1–7。

    这条审计把"意外结构"这一类问题一次性覆盖到所有消息上，而不只是榜单。
    """

    def _messages(self) -> dict[str, str]:
        return {
            "signin": signin_text(
                nickname="小明", rank=3, bread=16, streak=5,
                total_bread=142, total_today=17, image_url=IMAGE,
            ),
            "already": already_text(
                nickname="小明", rank=8, bread=11, streak=3, total_bread=53
            ),
            "makeup": makeup_text(
                target_date="2026-09-20", bread=9, streak=8,
                total_bread=59, cards_balance=0,
            ),
            "bread_board": board_text(
                title="面包排行榜",
                entries=entries(("a", "甲", 30), ("b", "乙", 20), ("c", "丙", 10), ("d", "丁", 5)),
                scope="group", scope_label="本群", value_suffix=" 个🍞",
                me=MeStanding("z", "自己", 1, 99),
            ),
            "signin_board": board_text(
                title="签到排行榜",
                entries=[
                    BoardEntry("a", "甲", 1, signed_at="2026-09-21T08:03:12+08:00"),
                    BoardEntry("b", "乙", 2, signed_at="2026-09-21T08:05:47+08:00"),
                ],
                scope="all", scope_label="今天 · 全部", value_suffix="",
                me=None, tz=TZ,
            ),
            "signin_board_empty": board_text(
                title="签到排行榜", entries=[], scope="group",
                scope_label="今天 · 本群", value_suffix="", me=None,
            ),
            "profile": profile_text(
                nickname="小明", rank=3, total_bread=142, total_today=17, streak=5,
                best_streak=11, signin_count=48, cards=1, days_to_card=2,
            ),
            "profile_unsigned": profile_text(
                nickname="小明", rank=0, total_bread=0, total_today=0, streak=0,
                best_streak=0, signin_count=0, cards=0, days_to_card=7,
            ),
            "menu": menu_text(),
            "need_signin": need_signin_text(),
            "welcome": welcome_text(image_url=IMAGE),
            "welcome_with_mention": welcome_text(
                mention=mention_tag("FE003FAF76C4817251FDC128A16753BB"), image_url=IMAGE
            ),
        }

    def test_no_ordered_list_markers_anywhere(self) -> None:
        """`数字.` 或 `数字)` 加空格会触发渲染器自动重新编号 —— 一条都不许有。"""
        import re

        for name, text in self._messages().items():
            for line in text.split("\n"):
                self.assertIsNone(
                    re.match(r"^\s*\d+[.)]\s", line),
                    f"{name} 里这行会被当列表项重编号: {line!r}",
                )

    def test_no_thematic_breaks_or_code_fences(self) -> None:
        """`***` / `---` / `___` 是分割线，围栏是代码块 —— 群聊 markdown 里
        我们没验过它们怎么渲染，而平台的支持列表里也没有代码块。"""
        for name, text in self._messages().items():
            for line in text.split("\n"):
                stripped = line.strip()
                self.assertNotIn("```", stripped, f"{name}: {line!r}")
                self.assertNotIn("~~~", stripped, f"{name}: {line!r}")
                self.assertFalse(
                    stripped and len(set(stripped)) == 1 and stripped[0] in "*-_",
                    f"{name} 里这行是分割线: {line!r}",
                )

    def test_no_blockquotes(self) -> None:
        for name, text in self._messages().items():
            for line in text.split("\n"):
                self.assertFalse(line.lstrip().startswith(">"), f"{name}: {line!r}")

    def test_bullets_only_where_intended(self) -> None:
        """`- ` 我们是有意用的（签到明细、个人页、菜单），但要确认它没跑到
        榜单里去 —— 榜单靠序号，不该混进项目符号。"""
        messages = self._messages()
        for name in ("bread_board", "signin_board"):
            for line in messages[name].split("\n"):
                self.assertFalse(
                    line.lstrip().startswith("- "), f"{name} 里不该有项目符号: {line!r}"
                )

    def test_headings_appear_only_as_the_first_lines(self) -> None:
        """标题只该出现在消息开头的 # / ##，正文里不该冒出 # 。"""
        for name, text in self._messages().items():
            lines = text.split("\n")
            for index, line in enumerate(lines):
                if line.startswith("#"):
                    self.assertLess(index, 2, f"{name} 第 {index} 行才是标题: {line!r}")


class WelcomeTest(unittest.TestCase):
    """新成员入群的欢迎语。"""

    def test_contains_the_bread_image(self) -> None:
        text = welcome_text(image_url=IMAGE)
        self.assertIn(IMAGE, text)

    def test_image_sits_on_its_own_line(self) -> None:
        lines = welcome_text(image_url=IMAGE).split("\n")
        image_line = [ln for ln in lines if IMAGE in ln][0]
        self.assertTrue(image_line.strip().startswith("!"))
        self.assertEqual(lines[lines.index(image_line) - 1].strip(), "", "图片前要有空行")

    def test_tells_them_how_to_sign_in(self) -> None:
        text = welcome_text(image_url=IMAGE)
        self.assertIn("充能面包", text)

    def test_wording_matches_what_was_agreed(self) -> None:
        """实测确认 @ 生效后定稿的文案。

        只有一条说明 —— 新成员不会读长文，说清"发什么"就够了。
        """
        text = welcome_text(mention=mention_tag("FE003FAF"), image_url=IMAGE)
        self.assertIn("# 🍞 欢迎新成员", text)
        self.assertIn('<qqbot-at-user id="FE003FAF" /> 恭喜，充能面包。', text)
        self.assertIn("发 充能面包 签到，每天领面包", text)
        bullets = [ln for ln in text.split("\n") if ln.startswith("- ")]
        self.assertEqual(len(bullets), 1, "定稿只留一条说明")

    def test_without_mention_the_sentence_still_reads(self) -> None:
        """退回路径里没有 @，句子要能独立成立。"""
        text = welcome_text(image_url=IMAGE)
        self.assertIn("恭喜，充能面包。", text)
        self.assertFalse(text.split("\n")[2].startswith(" "), "不该留下前导空格")

    def test_mention_uses_the_documented_group_syntax(self) -> None:
        """官方"文本交互"页：`<qqbot-at-user id="" />`，群聊可用且支持 markdown。

        旧写法 `<@userid>` 官方标记"即将弃用"，所以用新格式。
        """
        from chargebread.render import mention_tag

        self.assertEqual(
            mention_tag("FE003FAF76C4817251FDC128A16753BB"),
            '<qqbot-at-user id="FE003FAF76C4817251FDC128A16753BB" />',
        )

    def test_empty_openid_yields_no_mention(self) -> None:
        from chargebread.render import mention_tag

        self.assertEqual(mention_tag(""), "")

    def test_mention_is_optional(self) -> None:
        self.assertNotIn("<qqbot-at-user", welcome_text(image_url=IMAGE))
        self.assertIn(
            "<qqbot-at-user",
            welcome_text(mention=mention_tag("ABC123"), image_url=IMAGE),
        )

    def test_mention_sits_at_the_very_front(self) -> None:
        """@ 要顶在最前面，否则客户端可能不把它当提及。"""
        text = welcome_text(mention=mention_tag("ABC123"), image_url=IMAGE)
        body = [ln for ln in text.split("\n") if "<qqbot-at-user" in ln]
        self.assertTrue(body, "找不到提及所在的正文行")
        self.assertTrue(body[0].lstrip().startswith("<qqbot-at-user"), body[0])

    def test_welcome_keyboard_offers_signin_and_help(self) -> None:
        """新成员最需要的是"怎么签到"和"规则是什么"，不是看榜。"""
        labels = [
            b["render_data"]["label"]
            for row in kb_welcome()["content"]["rows"]
            for b in row["buttons"]
        ]
        self.assertEqual(labels, ["签到", "帮助"])

    def test_welcome_has_no_markdown_structure_hazards(self) -> None:
        import re

        from chargebread.render import mention_tag

        for text in (
            welcome_text(image_url=IMAGE),
            welcome_text(mention=mention_tag("ABC123"), image_url=IMAGE),
        ):
            for line in text.split("\n"):
                self.assertIsNone(re.match(r"^\s*\d+[.)]\s", line), repr(line))
                self.assertFalse(line.lstrip().startswith(">"), repr(line))


class MenuTest(unittest.TestCase):
    def test_lists_every_command(self) -> None:
        text = menu_text()
        for cmd in ("充能面包", "面包排行榜", "签到排行榜", "我的面包", "补签"):
            self.assertIn(cmd, text)


class SafeNameTest(unittest.TestCase):
    """昵称是**用户可控**的，直接拼进 markdown 会出问题。"""

    def test_plain_name_is_untouched(self) -> None:
        self.assertEqual(safe_name("Hoshino iChiKa"), "Hoshino iChiKa")

    def test_emoji_in_name_survives(self) -> None:
        self.assertEqual(safe_name("小明🍞"), "小明🍞")

    def test_underscore_in_name_survives(self) -> None:
        """下划线是用户名常见字符，删了会把真实昵称改坏。

        保留它最坏只是名字显示成斜体 —— 纯外观，注入不了东西，也拆不坏
        我们用 ** 做的加粗包裹（单个符号配不成对）。
        """
        self.assertEqual(safe_name("Hesitate_P"), "Hesitate_P")

    def test_tilde_in_name_survives(self) -> None:
        """波浪号同理：`小明~` 是很常见的昵称写法。

        单个 ~ 配不成删除线，而且不影响我们的 ** 包裹。
        """
        self.assertEqual(safe_name("小明~"), "小明~")

    def test_asterisk_is_still_stripped(self) -> None:
        """星号必须删：昵称里的 * 会和包裹用的 ** 配对错乱，把加粗拆坏。"""
        self.assertNotIn("*", safe_name("a**b"))

    def test_markdown_emphasis_cannot_leak_into_our_layout(self) -> None:
        """昵称里的 ** 会把我们自己的强调标记搞乱。"""
        cleaned = safe_name("**假粗体**")
        self.assertNotIn("*", cleaned)
        self.assertIn("假粗体", cleaned)

    def test_newline_in_name_cannot_break_line_structure(self) -> None:
        """榜单是一行一条，昵称里带换行会撑破结构。"""
        cleaned = safe_name("小明\n1.🥇 假的\n")
        self.assertNotIn("\n", cleaned)

    def test_image_injection_is_neutralised(self) -> None:
        """昵称里塞 ![](url) 能往消息里注入图片。"""
        cleaned = safe_name("![](https://evil.example/x.png)")
        self.assertNotIn("![", cleaned)
        self.assertNotIn("](", cleaned)

    def test_link_injection_is_neutralised(self) -> None:
        cleaned = safe_name("[点我](https://evil.example)")
        self.assertNotIn("](", cleaned)

    def test_zero_width_space_is_removed(self) -> None:
        """零宽空格是平台文档里"强制换行"的手法，不该让它出现在昵称里。"""
        self.assertNotIn("\u200b", safe_name("小\u200b明"))

    def test_other_invisible_and_bidi_chars_are_removed(self) -> None:
        """审查指出：只清 U+200B 不够 —— 还有零宽连接符和双向覆盖符，
        它们能让名字看起来是空的，或者把后面的文字方向翻转。"""
        cleaned = safe_name("小\u200c\u200d\u2060\ufeff明\u202e")
        self.assertEqual(cleaned, "小明")
        for ch in ("\u200c", "\u200d", "\u2060", "\ufeff", "\u202e"):
            self.assertNotIn(ch, cleaned)

    def test_name_that_is_only_invisible_falls_back(self) -> None:
        self.assertEqual(safe_name("\u200b\u200c\ufeff"), "面包学徒")

    def test_empty_or_whitespace_name_falls_back(self) -> None:
        self.assertEqual(safe_name(""), "面包学徒")
        self.assertEqual(safe_name("   "), "面包学徒")
        self.assertEqual(safe_name("***"), "面包学徒")

    def test_overlong_name_is_truncated(self) -> None:
        cleaned = safe_name("超" * 100)
        self.assertLessEqual(len(cleaned), 25)
        self.assertTrue(cleaned.endswith("…"))

    def test_board_applies_it_to_entries_and_me(self) -> None:
        text = board_text(
            title="面包排行榜",
            entries=[BoardEntry("a", "**侵入者**", 5)],
            scope="group", scope_label="本群", value_suffix=" 个🍞",
            me=MeStanding("z", "**另一个**", 1, 9),
        )
        self.assertIn("**侵入者**", text, "我们的强调里只该有净化后的名字")
        self.assertNotIn("****", text, "昵称自带的星号不该泄漏进来")

    def test_signin_message_applies_it(self) -> None:
        """昵称里塞图片/换行，不能真的注入进签到消息。"""
        text = signin_text(
            nickname="小明\n![](https://evil.example/x.png)", rank=1, bread=15, streak=1,
            total_bread=15, total_today=1, image_url=IMAGE,
        )
        self.assertNotIn("](https://evil.example", text, "昵称里的图片不该被注入")
        self.assertIn("小明", text)
        self.assertIn(IMAGE, text, "我们自己的面包图仍要在")


if __name__ == "__main__":
    unittest.main(verbosity=2)
