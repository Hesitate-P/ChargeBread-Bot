"""指令面板的契约（TDD：先写测试，再实现）。

平台约束来自文档（POST /v2/panels）：
  · scope 可选 c2c / group / channel / dm；channel 与 dm **只能** 全局配置
  · target_type 可选 all / specific；specific 仅 c2c 与 group 可用
  · user_openids / group_openids 每次最多 20 个，且仅 specific 时有效
  · panel.items 最多 20 个；remark 最多 255 字符且不对用户展示
  · PanelItem.name 最多 14 字符（约 7 个中文），desc 最多 30 字符（约 15 个中文）
  · PanelItem.type 只有 command 与 link；type=command 点击后
    **内容会填入聊天输入框**（用户仍需自己发送），type=link 才跳浏览器

所以下面把「名称长度」「项目上限」「全部是 command」「payload 形状」都钉住 ——
这些超限会被平台以 40030013 / 40030016 直接拒绝。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.api import Api, HttpResponse, ApiError  # noqa: E402
from chargebread.config import Config  # noqa: E402
from chargebread import panel  # noqa: E402

BASE = "https://api.bot.qq.com"


def setUpModule() -> None:
    """静音 panel 的 logger。

    生产里自动同步失败就该 log.warning(exc_info=True) 报出来 —— 那可能意味着
    面板没装上。但测试里会往 stderr 吐 traceback 弄脏输出。只关日志，不改行为。
    """
    import logging

    logging.getLogger("chargebread.panel").setLevel(logging.CRITICAL)


# 平台的字数口径：一个汉字算 2 个单位（文档「14 个字符，约 7 个中文汉字」）
NAME_MAX_UNITS = 14
DESC_MAX_UNITS = 30
MAX_ITEMS = 20


def make_config(
    allowed_groups: frozenset[str] = frozenset(), *, panel_autosync: bool = True
) -> Config:
    return Config(
        app_id="1905655911",
        app_secret="s",
        image_url="https://img.example/a.png",
        db_path=Path("/tmp/x.sqlite3"),
        tz=ZoneInfo("Asia/Shanghai"),
        api_base=BASE,
        allowed_groups=allowed_groups,
        panel_autosync=panel_autosync,
    )


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.script: list[object] = []

    def push(self, response: object) -> None:
        self.script.append(response)

    async def request(self, method, url, *, json_body, headers, timeout=None) -> HttpResponse:
        self.calls.append({"method": method, "url": url, "json": json_body})
        if not self.script:
            return HttpResponse(200, {})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, HttpResponse)
        return item

    @property
    def paths(self) -> list[str]:
        return [c["url"].replace(BASE, "") for c in self.calls]


def token_response() -> HttpResponse:
    return HttpResponse(200, {"access_token": "T", "expires_in": 7200})


class TextUnitsTest(unittest.TestCase):
    """平台的字数口径 —— 真机二分出来的，和直觉不一样。

    `30013 超出数量限制` 实际是"长度超限"，而且一个汉字算 2 个单位。
    实测边界：7 个汉字通过，8 个汉字被拒。
    """

    def test_cjk_counts_as_two_units(self) -> None:
        self.assertEqual(panel.text_units("面包排行榜"), 10)
        self.assertEqual(panel.text_units("abc"), 3)

    def test_seven_cjk_fits_and_eight_does_not(self) -> None:
        seven = [{"name": "面包排行榜全部", "desc": "d", "type": "command"}]
        panel.validate_items(seven)  # 7 汉字 = 14 单位，正好卡在上限

        eight = [{"name": "面包排行榜本群榜", "desc": "d", "type": "command"}]
        with self.assertRaises(ValueError) as ctx:
            panel.validate_items(eight)
        # 报错要把"多少单位、上限多少"说清楚，否则又要在真机上二分
        self.assertIn("16 个单位", str(ctx.exception))
        self.assertIn("超过上限 14", str(ctx.exception))

    def test_space_was_not_the_problem(self) -> None:
        """曾经以为是不许有空格 —— 实测 7 汉字无空格通过、8 汉字无空格被拒，
        所以是长度。带空格但只有 5 汉字的名称应该没问题。"""
        panel.validate_items([{"name": "面包榜 全部", "desc": "d", "type": "command"}])

    def test_overlong_desc_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            panel.validate_items(
                [{"name": "签到", "desc": "描" * 16, "type": "command"}]
            )

    def test_too_many_items_is_rejected(self) -> None:
        items = [{"name": "签到", "desc": "d", "type": "command"} for _ in range(21)]
        with self.assertRaises(ValueError):
            panel.validate_items(items)


class PanelItemsTest(unittest.TestCase):
    def test_every_item_is_a_command_item(self) -> None:
        """我们只做指令项：type=link 需要跳转 URL，而面板里的链接由用户浏览器
        打开，我们没有任何该放上去的地址。"""
        for item in panel.panel_items():
            self.assertEqual(item["type"], "command", item)

    def test_names_fit_the_platform_limit(self) -> None:
        for item in panel.panel_items():
            units = panel.text_units(item["name"])
            self.assertLessEqual(
                units, NAME_MAX_UNITS, f"name 超限会被拒: {item['name']!r} = {units} 单位"
            )

    def test_descriptions_fit_the_platform_limit(self) -> None:
        for item in panel.panel_items():
            units = panel.text_units(item["desc"])
            self.assertLessEqual(units, DESC_MAX_UNITS, f"desc 超限: {item['desc']!r}")

    def test_no_item_embeds_a_scope_argument(self) -> None:
        """面板项没法表达参数，而带参数的写法（如「面包排行榜 全部」）正好
        撞上 7 汉字上限。所以面板里不放这类项。"""
        for item in panel.panel_items():
            self.assertNotIn(" ", item["name"], item["name"])

    def test_the_whole_item_list_validates(self) -> None:
        panel.validate_items(panel.panel_items())

    def test_item_count_is_within_limit(self) -> None:
        self.assertLessEqual(len(panel.panel_items()), MAX_ITEMS)

    def test_panel_covers_all_user_facing_commands(self) -> None:
        names = {item["name"] for item in panel.panel_items()}
        for expected in (
            "充能面包",
            "面包排行榜",
            "签到排行榜",
            "今日充能指数",
            "我的面包",
            "补签",
            "帮助",
        ):
            self.assertIn(expected, names)

    def test_names_are_the_bare_words_our_parser_accepts(self) -> None:
        """type=command 点击后是把内容填进输入框，用户还要自己发送。

        所以名称必须是我们解析器认得的写法 —— 带不带斜杠都认，但不能是
        「/充能面包 本群」这种需要额外语境的东西。
        """
        from chargebread import commands

        for item in panel.panel_items():
            parsed = commands.parse(item["name"])
            self.assertNotEqual(parsed.name, commands.UNKNOWN, item["name"])


class PanelSpecTest(unittest.TestCase):
    def test_scope_is_group(self) -> None:
        spec = panel.build_spec(make_config())
        self.assertEqual(spec.scope, "group")

    def test_defaults_to_all_groups(self) -> None:
        spec = panel.build_spec(make_config())
        self.assertEqual(spec.target_type, "all")
        self.assertEqual(spec.group_openids, ())

    def test_uses_specific_targets_when_groups_are_whitelisted(self) -> None:
        spec = panel.build_spec(make_config(frozenset({"G2", "G1"})))
        self.assertEqual(spec.target_type, "specific")
        self.assertEqual(spec.group_openids, ("G1", "G2"), "应排序，便于比对是否变化")

    def test_too_many_whitelisted_groups_is_rejected_loudly(self) -> None:
        """specific 每次最多 20 个 openid。与其悄悄退回 all（那会让面板出现在
        我们不服务的群里），不如报错让运维知道。"""
        many = frozenset(f"G{i:03d}" for i in range(21))
        with self.assertRaises(ValueError) as ctx:
            panel.build_spec(make_config(many))
        self.assertIn("20", str(ctx.exception))

    def test_exactly_twenty_groups_is_allowed(self) -> None:
        twenty = frozenset(f"G{i:03d}" for i in range(20))
        spec = panel.build_spec(make_config(twenty))
        self.assertEqual(spec.target_type, "specific")
        self.assertEqual(len(spec.group_openids), 20)


class PanelPayloadTest(unittest.TestCase):
    def test_payload_shape_matches_the_documented_body(self) -> None:
        payload = panel.to_payload(panel.build_spec(make_config()))
        self.assertEqual(payload["scope"], "group")
        self.assertEqual(payload["target_type"], "all")
        self.assertIn("panel", payload, "面板内容要嵌在 panel 字段下")
        self.assertIn("items", payload["panel"])
        self.assertIn("remark", payload["panel"])

    def test_remark_is_present_small_and_not_user_visible(self) -> None:
        """remark 是我们的身份标记，重装时靠它认出自己那个面板。"""
        payload = panel.to_payload(panel.build_spec(make_config()))
        remark = payload["panel"]["remark"]
        self.assertTrue(remark)
        self.assertLessEqual(len(remark), 255)

    def test_specific_payload_carries_group_openids(self) -> None:
        payload = panel.to_payload(panel.build_spec(make_config(frozenset({"G1"}))))
        self.assertEqual(payload["target_type"], "specific")
        self.assertEqual(payload["group_openids"], ["G1"])
        self.assertNotIn("user_openids", payload, "群里装面板不该带单聊 openid")


class ListPanelsTest(unittest.IsolatedAsyncioTestCase):
    """列表接口的真实契约 —— 我一开始按端点清单猜错了，真机报了 30011。

    响应字段是 `records` 而不是 `panels`：读错就永远认为"没有面板"，
    于是每次安装都新建一个，一路攒到 20 个上限。
    """

    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.api = Api(make_config(), transport=self.transport)
        self.transport.push(token_response())

    async def test_scope_is_sent_as_a_required_query_param(self) -> None:
        self.transport.push(HttpResponse(200, {"records": [], "is_end": True}))
        await self.api.list_panels()
        self.assertIn("/v2/panels?scope=group", self.transport.paths)

    async def test_reads_the_records_field(self) -> None:
        self.transport.push(
            HttpResponse(200, {"records": [{"panel_id": "p1"}], "is_end": True})
        )
        panels = await self.api.list_panels()
        self.assertEqual([p["panel_id"] for p in panels], ["p1"])

    async def test_wrong_field_name_would_yield_nothing(self) -> None:
        """把 panels 当字段名会静默拿到空列表 —— 这正是要防的坑。"""
        self.transport.push(HttpResponse(200, {"panels": [{"panel_id": "p1"}]}))
        self.assertEqual(await self.api.list_panels(), [])

    async def test_follows_pagination_until_is_end(self) -> None:
        self.transport.push(
            HttpResponse(200, {"records": [{"panel_id": "p1"}], "next_cursor": "c1", "is_end": False})
        )
        self.transport.push(
            HttpResponse(200, {"records": [{"panel_id": "p2"}], "next_cursor": "", "is_end": True})
        )
        panels = await self.api.list_panels()
        self.assertEqual([p["panel_id"] for p in panels], ["p1", "p2"])
        self.assertIn("/v2/panels?scope=group&cursor=c1", self.transport.paths)

    async def test_stops_when_cursor_is_empty_even_without_is_end(self) -> None:
        self.transport.push(
            HttpResponse(200, {"records": [{"panel_id": "p1"}], "next_cursor": ""})
        )
        self.assertEqual(len(await self.api.list_panels()), 1)

    async def test_illegal_scope_is_rejected_before_any_request(self) -> None:
        with self.assertRaises(ValueError):
            await self.api.list_panels("guild")
        self.assertEqual(
            [c for c in self.transport.calls if "/v2/panels" in c["url"]], []
        )


class InstallTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.api = Api(make_config(), transport=self.transport)
        self.transport.push(token_response())

    async def _install(self, config: Config | None = None) -> str:
        return await panel.install(self.api, config or make_config())

    async def test_creates_when_no_panel_exists(self) -> None:
        self.transport.push(HttpResponse(200, {"records": [], "is_end": True}))
        self.transport.push(HttpResponse(200, {"panel_id": "p_new"}))
        summary = await self._install()
        self.assertIn("p_new", summary)
        panel_methods = [c["method"] for c in self.transport.calls if "/v2/panels" in c["url"]]
        self.assertEqual(panel_methods, ["GET", "POST"], "先查再建")

    async def test_is_idempotent_when_the_existing_panel_matches(self) -> None:
        """内容没变就不该反复删建，白折腾还容易撞上 40030009「操作进行中」。"""
        payload = panel.to_payload(panel.build_spec(make_config()))
        self.transport.push(
            HttpResponse(
                200,
                {
                    "records": [
                        {
                            "panel_id": "p_mine",
                            "scope": "group",
                            "target_type": "all",
                            "panel": {"items": payload["panel"]["items"], "remark": payload["panel"]["remark"]},
                        }
                    ]
                },
            )
        )
        summary = await self._install()
        self.assertIn("已是最新", summary)
        panel_methods = [c["method"] for c in self.transport.calls if "/v2/panels" in c["url"]]
        self.assertEqual(panel_methods, ["GET"], "内容一致时只查一次，不该增删")

    async def test_recreates_when_the_existing_panel_differs(self) -> None:
        """代码里的指令改了就该更新 —— 删掉重建比逐项 diff 简单且不会漏。"""
        self.transport.push(
            HttpResponse(
                200,
                {
                    "records": [
                        {
                            "panel_id": "p_old",
                            "scope": "group",
                            "target_type": "all",
                            "panel": {"items": [{"name": "旧指令", "type": "command"}], "remark": panel.PANEL_REMARK},
                        }
                    ]
                },
            )
        )
        self.transport.push(HttpResponse(200, {}))                      # DELETE
        self.transport.push(HttpResponse(200, {"panel_id": "p_fresh"}))  # POST
        summary = await self._install()
        self.assertIn("p_fresh", summary)
        panel_methods = [c["method"] for c in self.transport.calls if "/v2/panels" in c["url"]]
        self.assertEqual(panel_methods, ["GET", "DELETE", "POST"], "内容变了应删旧建新")

    async def test_ignores_other_developers_panels(self) -> None:
        """remark 不是我们的就不动 —— 别删掉别人（或我们别的功能）的面板。"""
        self.transport.push(
            HttpResponse(
                200,
                {"records": [{"panel_id": "p_other", "panel": {"remark": "something:else"}}], "is_end": True},
            )
        )
        self.transport.push(HttpResponse(200, {"panel_id": "p_new"}))
        await self._install()
        self.assertNotIn("DELETE", [c["method"] for c in self.transport.calls])

    async def test_platform_error_is_surfaced_with_code(self) -> None:
        self.transport.push(HttpResponse(200, {"records": [], "is_end": True}))
        self.transport.push(
            HttpResponse(200, {"code": 40030020, "message": "内容存在安全风险，请修改后重试"})
        )
        with self.assertRaises(ApiError) as ctx:
            await self._install()
        self.assertEqual(ctx.exception.code, 40030020)


class UninstallTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.api = Api(make_config(), transport=self.transport)
        self.transport.push(token_response())

    async def test_deletes_only_our_panels(self) -> None:
        self.transport.push(
            HttpResponse(
                200,
                {
                    "records": [
                        {"panel_id": "p_mine", "panel": {"remark": panel.PANEL_REMARK}},
                        {"panel_id": "p_other", "panel": {"remark": "other"}},
                    ]
                },
            )
        )
        self.transport.push(HttpResponse(200, {}))
        removed = await panel.uninstall(self.api)
        self.assertEqual(removed, 1)
        urls = [c["url"] for c in self.transport.calls if c["method"] == "DELETE"]
        self.assertEqual(urls, [f"{BASE}/v2/panels/p_mine"])

    async def test_nothing_to_do_is_not_an_error(self) -> None:
        self.transport.push(HttpResponse(200, {"records": [], "is_end": True}))
        self.assertEqual(await panel.uninstall(self.api), 0)


class AutosyncTest(unittest.IsolatedAsyncioTestCase):
    """启动时后台把面板同步成代码里的样子。

    不做成阻塞启动的一步：面板接口慢或报错都不该拖住机器人上线。
    内容一致时只查一次、什么都不改；只有内容真变了才删旧建新。
    """

    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.api = Api(make_config(), transport=self.transport)
        self.transport.push(token_response())

    async def test_disabled_by_config_returns_no_task(self) -> None:
        task = panel.start_autosync(self.api, make_config(panel_autosync=False))
        self.assertIsNone(task)
        self.assertEqual(self.transport.calls, [], "关掉之后一个请求都不该发")

    async def test_enabled_returns_a_running_task_and_creates_the_panel(self) -> None:
        self.transport.push(HttpResponse(200, {"records": [], "is_end": True}))
        self.transport.push(HttpResponse(200, {"panel_id": "p_auto"}))
        task = panel.start_autosync(self.api, make_config())
        self.assertIsNotNone(task)
        assert task is not None
        await asyncio.wait_for(task, timeout=2)
        self.assertIn("p_auto", task.result())

    async def test_does_not_block_the_caller(self) -> None:
        """返回即已排入后台：调用方（启动流程）不该等它。"""
        self.transport.push(HttpResponse(200, {"records": [], "is_end": True}))
        self.transport.push(HttpResponse(200, {"panel_id": "p_auto"}))
        task = panel.start_autosync(self.api, make_config())
        assert task is not None
        self.assertFalse(task.done(), "刚返回时不该已经跑完（应是后台任务）")
        await asyncio.wait_for(task, timeout=2)

    async def test_failure_is_swallowed_so_startup_is_not_blocked(self) -> None:
        self.transport.push(ApiError(40030020, "内容存在安全风险"))
        task = panel.start_autosync(self.api, make_config())
        assert task is not None
        # 不该抛出去
        await asyncio.wait_for(task, timeout=2)

    async def test_skips_writes_when_the_panel_already_matches(self) -> None:
        """普通重启只该查一次 —— 这才是"自动同步"不会骚扰用户的原因。"""
        payload = panel.to_payload(panel.build_spec(make_config()))
        self.transport.push(
            HttpResponse(
                200,
                {
                    "records": [
                        {
                            "panel_id": "p_mine",
                            "scope": "group",
                            "target_type": "all",
                            "panel": {
                                "items": payload["panel"]["items"],
                                "remark": payload["panel"]["remark"],
                            },
                        }
                    ],
                    "is_end": True,
                },
            )
        )
        task = panel.start_autosync(self.api, make_config())
        assert task is not None
        await asyncio.wait_for(task, timeout=2)
        panel_methods = [c["method"] for c in self.transport.calls if "/v2/panels" in c["url"]]
        self.assertEqual(panel_methods, ["GET"], "内容一致时只查不写")


if __name__ == "__main__":
    unittest.main(verbosity=2)
