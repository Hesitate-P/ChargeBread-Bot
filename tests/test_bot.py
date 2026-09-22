"""bot.py 的期望契约（TDD：先写测试，再实现）。

这一层是把事件翻译成"签到/榜单/个人页"的动作。api 用假实现注入，
所以断言的是**发出去什么内容、应答和回复的顺序对不对**。

几条容易出错、值得钉住的：
  · 按钮回调必须**先应答再回复** —— 3 秒窗口，回复可能慢（要查数据库）。
  · 回复按钮回调要用 `event_id`（外层信封 id），不是 `msg_id`。
  · 机器人自己发的消息要忽略，否则会自己跟自己对话。
  · 平台会重复推送同一个事件，同一条消息只能处理一次。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread import commands, game  # noqa: E402
from chargebread.api import ApiError  # noqa: E402
from chargebread.bot import Bot  # noqa: E402
from chargebread.config import Config  # noqa: E402
from chargebread.db import Database  # noqa: E402
from chargebread.gateway import GatewayEvent  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
UID = "AAAA1111BBBB2222CCCC3333DDDD4444"
UID2 = "BBBB1111BBBB2222CCCC3333DDDD4444"
GROUP = "E4A877464EB83C1447BD828C6CD89E34"
GROUP2 = "9AD4ECB24CB5E6EB3805240505E6EDB5"
IMAGE = "https://img.example/ChargeBread.png"
NOW = datetime(2026, 9, 21, 21, 0, tzinfo=TZ)


def setUpModule() -> None:
    """静音 bot 的 logger。

    生产里"按钮应答失败"就该 log.exception 大声报出来（用户客户端会转圈），
    但测试里会往 stderr 吐 traceback 弄脏输出。只关日志，不改行为。
    """
    import logging

    logging.getLogger("chargebread.bot").setLevel(logging.CRITICAL)


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.acks: list[str] = []
        self.ack_error: Exception | None = None
        # 可编排的发送错误：每次 send_markdown 先从这里弹一个，非 None 就抛
        self.send_errors: list[Exception | None] = []
        self._seq: dict[str, int] = {}

    def next_seq(self, key: str) -> int:
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

    async def send_markdown(
        self, group_openid, content, *, keyboard=None, msg_id=None, event_id=None, msg_seq=1
    ):
        if self.send_errors:
            error = self.send_errors.pop(0)
            if error is not None:
                raise error
        self.sent.append(
            {
                "group": group_openid,
                "content": content,
                "keyboard": keyboard,
                "msg_id": msg_id,
                "event_id": event_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "sent"}

    async def ack_interaction(self, interaction_id: str) -> None:
        if self.ack_error is not None:
            raise self.ack_error
        self.acks.append(interaction_id)


def make_config(**overrides) -> Config:
    base = dict(
        app_id="1",
        app_secret="s",
        image_url=IMAGE,
        db_path=Path("/tmp/does-not-matter.sqlite3"),
        tz=TZ,
        reset_hour=0,
    )
    base.update(overrides)
    return Config(**base)


def group_event(
    content: str,
    *,
    user_id: str = UID,
    nickname: str = "小明",
    group: str = GROUP,
    message_id: str = "ROBOT1.0_msg",
    envelope: str = "GROUP_AT_MESSAGE_CREATE:env-1",
    is_bot: bool = False,
    name: str = "GROUP_AT_MESSAGE_CREATE",
) -> GatewayEvent:
    return GatewayEvent(
        name=name,
        message_id=message_id,
        envelope_id=envelope,
        data={
            "id": message_id,
            "content": content,
            "group_openid": group,
            "author": {
                "id": user_id,
                "member_openid": user_id,
                "username": nickname,
                "bot": is_bot,
                "member_role": "member",
            },
        },
        seq=2,
        raw={},
    )


def interaction_event(
    button_data: str,
    *,
    user_id: str = UID,
    group: str = GROUP,
    interaction_id: str = "844c370e-2f8c-4c53-861c-8067f57ba7f6",
    type_: int = 11,
) -> GatewayEvent:
    return GatewayEvent(
        name="INTERACTION_CREATE",
        message_id=interaction_id,
        envelope_id=f"INTERACTION_CREATE:{interaction_id}",
        data={
            "id": interaction_id,
            "type": type_,
            "scene": "group",
            "chat_type": 1,
            "group_openid": group,
            "group_member_openid": user_id,
            "data": {"type": type_, "resolved": {"button_data": button_data, "button_id": "btn0"}},
        },
        seq=3,
        raw={},
    )


def member_add_event(
    *,
    member_id: str = UID2,
    group: str = GROUP,
    envelope: str = "GROUP_MEMBER_ADD:env-1",
    timestamp: int = 1789996359,
) -> GatewayEvent:
    """新成员入群事件。

    注意这个事件的 `d` 里**没有 `id`、也没有昵称** —— 真实载荷只有这四个字段
    （探针抓到的原文如此），所以 message_id 是空串，只能靠外层信封 id。
    """
    return GatewayEvent(
        name="GROUP_MEMBER_ADD",
        message_id="",
        envelope_id=envelope,
        data={
            "group_openid": group,
            "member_openid": member_id,
            "timestamp": timestamp,
        },
        seq=4,
        raw={},
    )


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.sqlite3")
        self.api = FakeApi()
        self.bot = Bot(make_config(), self.db, self.api, clock=lambda: NOW)
        # 默认把主测试用户登记成"已签到" —— 按钮回调对未建档用户走另一条分支。
        # 登记在**昨天**，这样"今天的签到"对测试仍是干净的第一次。
        game.sign_in(
            self.db,
            user_id=UID,
            nickname="小明",
            group_openid=GROUP,
            now=datetime(2026, 9, 20, 21, 0, tzinfo=TZ),
            tz=TZ,
        )
        self.api.sent.clear()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    @property
    def last(self) -> dict:
        return self.api.sent[-1]


class SigninTest(BotTestCase):
    async def test_signin_replies_with_image_and_rank(self) -> None:
        await self.bot.handle(group_event(" 充能面包 "))
        self.assertEqual(len(self.api.sent), 1, "图文是一条消息（图片内嵌在 markdown 里）")
        self.assertIn(IMAGE, self.last["content"])
        self.assertIn("小明", self.last["content"])
        self.assertIn("第1个签到", self.last["content"], "要告诉他是今天第几个")
        self.assertIsNotNone(self.last["keyboard"])
        self.assertEqual(self.last["msg_id"], "ROBOT1.0_msg")
        self.assertIsNone(self.last["event_id"])

    async def test_second_signin_same_day_says_already(self) -> None:
        await self.bot.handle(group_event("/充能面包"))
        await self.bot.handle(group_event("充能面包", message_id="ROBOT1.0_msg2", envelope="e2"))
        self.assertEqual(len(self.api.sent), 2)
        self.assertIn("已经签过", self.last["content"])
        self.assertEqual(self.db.count_signins("2026-09-21"), 1)

    async def test_signin_in_another_group_still_counts_as_already(self) -> None:
        """全局一天一次：在别的群签过，这个群也不能再签。"""
        await self.bot.handle(group_event("/充能面包", group=GROUP))
        await self.bot.handle(
            group_event("/充能面包", group=GROUP2, message_id="m2", envelope="e2")
        )
        self.assertIn("已经签过", self.last["content"])
        self.assertEqual(self.db.count_signins("2026-09-21"), 1)

    async def test_signin_by_second_user_gets_rank_two(self) -> None:
        await self.bot.handle(group_event("/充能面包"))
        await self.bot.handle(
            group_event("/充能面包", user_id=UID2, nickname="阿强", message_id="m2", envelope="e2")
        )
        self.assertIn("第2个签到", self.last["content"])
        self.assertIn("阿强", self.last["content"])


class WelcomeTest(BotTestCase):
    """新成员入群的欢迎语。

    这个事件三处与常规不同，测试要把它们都钉住：
      · `d` 里没有 `id` → 只能拿外层信封 id 当 event_id
      · `d` 里没有昵称 → 用 openid 提及，名字由客户端渲染
      · 文档说 event_id 不支持这个事件 → 要有退回主动消息的路
    """

    async def test_sends_welcome_with_mention_image_and_buttons(self) -> None:
        await self.bot.handle(member_add_event())
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn(IMAGE, self.last["content"])
        self.assertIn("充能面包", self.last["content"])
        self.assertIn(
            f'<qqbot-at-user id="{UID2}" />', self.last["content"], "要 @ 新成员"
        )
        labels = [
            b["render_data"]["label"]
            for row in self.last["keyboard"]["content"]["rows"]
            for b in row["buttons"]
        ]
        self.assertEqual(labels, ["签到", "帮助"])

    async def test_uses_envelope_id_as_the_passive_anchor(self) -> None:
        """这个事件没有 d.id，只能拿外层信封 id 当 event_id。"""
        await self.bot.handle(member_add_event())
        self.assertEqual(self.last["event_id"], "GROUP_MEMBER_ADD:env-1")
        self.assertIsNone(self.last["msg_id"])

    async def test_duplicate_delivery_sends_one_welcome(self) -> None:
        event = member_add_event()
        await self.bot.handle(event)
        await self.bot.handle(event)
        self.assertEqual(len(self.api.sent), 1)

    async def test_respects_group_allowlist(self) -> None:
        bot = Bot(
            make_config(allowed_groups=frozenset({GROUP})), self.db, self.api, clock=lambda: NOW
        )
        await bot.handle(member_add_event(group=GROUP2))
        self.assertEqual(self.api.sent, [])

    async def test_can_be_disabled(self) -> None:
        bot = Bot(make_config(welcome_enabled=False), self.db, self.api, clock=lambda: NOW)
        await bot.handle(member_add_event())
        self.assertEqual(self.api.sent, [])

    async def test_missing_member_id_is_ignored(self) -> None:
        await self.bot.handle(member_add_event(member_id=""))
        self.assertEqual(self.api.sent, [])

    async def test_missing_group_id_is_ignored(self) -> None:
        await self.bot.handle(member_add_event(group=""))
        self.assertEqual(self.api.sent, [])

    async def test_falls_back_to_proactive_when_reply_is_refused(self) -> None:
        """文档说 event_id 只支持三种事件。若平台真拒绝这个事件，
        退回主动消息 —— 而且 **@ 要保住**（失败原因跟 @ 无关）。"""
        self.api.send_errors = [
            ApiError(40034027, "该事件不支持回复消息"),   # 带 @ 的被动回复
            ApiError(40034027, "该事件不支持回复消息"),   # 去掉 @ 的被动回复
            None,                                        # 主动消息成功
        ]
        await self.bot.handle(member_add_event())
        self.assertEqual(len(self.api.sent), 1)
        self.assertIsNone(self.last["event_id"], "主动消息不带 event_id")
        self.assertIn("<qqbot-at-user", self.last["content"], "@ 不该因为退回主动而丢掉")

    async def test_drops_the_mention_when_the_platform_rejects_it(self) -> None:
        """@ 标签是我们动态拼进 content 的，社区反馈这种传参可能被拒。
        那种情况下脱掉 @ 再发一次 —— 欢迎语本身比 @ 重要。"""
        self.api.send_errors = [ApiError(40034124, "markdown消息参数错误"), None]
        await self.bot.handle(member_add_event())
        self.assertEqual(len(self.api.sent), 1, "去掉 @ 之后应该发成功")
        self.assertNotIn("<qqbot-at-user", self.last["content"])
        self.assertIn(IMAGE, self.last["content"], "欢迎语本体不能丢")

    async def test_proactive_failure_does_not_raise(self) -> None:
        """两条被动路都被拒、主动又没开权限时，只记日志，不能把事件处理炸掉。"""
        self.api.send_errors = [
            ApiError(40034027, "该事件不支持回复消息"),
            ApiError(40034027, "该事件不支持回复消息"),
            ApiError(40034105, "主动消息发送失败，无权限"),
        ]
        await self.bot.handle(member_add_event())   # 不应抛出
        self.assertEqual(self.api.sent, [])

    async def test_unexpected_error_propagates_so_redelivery_can_retry(self) -> None:
        self.api.send_errors = [ApiError(50055001, "消息发送异常，请稍后重试")]
        with self.assertRaises(ApiError):
            await self.bot.handle(member_add_event())

    async def test_failed_welcome_is_unmarked_for_redelivery(self) -> None:
        self.api.send_errors = [ApiError(50055001, "消息发送异常")]
        with self.assertRaises(ApiError):
            await self.bot.handle(member_add_event())
        # 事件不该被记为已处理，否则平台重投也救不回来
        self.assertTrue(
            self.db.mark_event_seen("GROUP_MEMBER_ADD:env-1", NOW.isoformat()),
            "失败的欢迎语必须撤销幂等标记",
        )


class MenuTest(BotTestCase):
    async def test_bare_at_shows_menu(self) -> None:
        await self.bot.handle(group_event(""))
        self.assertIn("充能面包", self.last["content"])
        self.assertIn("签到", self.last["content"])

    async def test_help_shows_menu(self) -> None:
        await self.bot.handle(group_event("/帮助"))
        self.assertIn("面包排行榜", self.last["content"])

    async def test_unknown_command_shows_menu_not_silence(self) -> None:
        """看不懂就发菜单 —— 沉默比乱答更让人困惑。"""
        await self.bot.handle(group_event("今天天气不错"))
        self.assertIn("充能面包", self.last["content"])


class BoardTest(BotTestCase):
    async def _seed(self) -> None:
        await self.bot.handle(group_event("/充能面包"))
        await self.bot.handle(
            group_event("/充能面包", user_id=UID2, nickname="阿强", message_id="m2", envelope="e2")
        )

    async def test_bread_board_defaults_to_group_scope(self) -> None:
        await self._seed()
        await self.bot.handle(group_event("/面包排行榜", message_id="m3", envelope="e3"))
        self.assertIn("本群", self.last["content"])
        self.assertIn("小明", self.last["content"])
        self.assertIn("阿强", self.last["content"])
        self.assertIsNotNone(self.last["keyboard"], "排行榜上要挂切榜按钮")

    async def test_bread_board_all_scope(self) -> None:
        await self._seed()
        await self.bot.handle(group_event("/面包排行榜 全部", message_id="m3", envelope="e3"))
        self.assertIn("全部", self.last["content"])
        self.assertIn("统计范围", self.last["content"])

    async def test_signin_board_shows_todays_order(self) -> None:
        await self._seed()
        await self.bot.handle(group_event("/签到排行榜", message_id="m3", envelope="e3"))
        self.assertIn("签到排行榜", self.last["content"])
        self.assertIn("小明", self.last["content"])

    async def test_signin_board_tells_me_if_i_havent_signed_today(self) -> None:
        """面包榜对没签到的人也给"你"行，签到榜不该沉默 —— 审查指出不一致。"""
        await self.bot.handle(group_event("/签到排行榜", user_id=UID2, nickname="阿强"))
        self.assertIn("你：", self.last["content"])
        self.assertIn("今天还没签到", self.last["content"])


class ProfileTest(BotTestCase):
    async def test_profile_shows_totals_and_streak(self) -> None:
        await self.bot.handle(group_event("/充能面包"))
        await self.bot.handle(group_event("/我的面包", message_id="m2", envelope="e2"))
        self.assertIn("小明", self.last["content"])
        self.assertIn("连签", self.last["content"])
        self.assertIn("累计", self.last["content"])

    async def test_profile_before_any_signin_is_graceful(self) -> None:
        await self.bot.handle(group_event("/我的面包"))
        self.assertIn("小明", self.last["content"])
        self.assertIn("还没签到", self.last["content"])


class MakeupTest(BotTestCase):
    async def test_makeup_without_card_explains_how_to_get_one(self) -> None:
        """setUp 已让主用户昨天签过，所以换个没签到的人来测"无卡"。"""
        await self.bot.handle(group_event("/补签", user_id=UID2, nickname="阿强"))
        self.assertIn("补签卡", self.last["content"])
        self.assertEqual(self.db.signin_count(UID2), 0, "没卡不该产生签到记录")

    async def test_makeup_reply_carries_a_signin_button(self) -> None:
        """补签成功的消息也要能一键签到 —— 补完顺手把今天的签了。"""
        await self.bot.handle(group_event("/补签", user_id=UID2, nickname="阿强"))
        labels = [
            b["render_data"]["label"]
            for row in self.last["keyboard"]["content"]["rows"]
            for b in row["buttons"]
        ]
        self.assertIn("签到", labels)
        self.assertNotIn("补签", labels, "刚补完，再放补签是个死按钮")


class IgnoreTest(BotTestCase):
    async def test_ignores_its_own_messages(self) -> None:
        await self.bot.handle(group_event("充能面包", is_bot=True))
        self.assertEqual(self.api.sent, [])

    async def test_duplicate_delivery_is_handled_once(self) -> None:
        """平台会重复推送同一条消息，第二次不能再发一遍、更不能多签一次。"""
        event = group_event("/充能面包")
        await self.bot.handle(event)
        await self.bot.handle(event)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.db.count_signins("2026-09-21"), 1)

    async def test_ignores_unrelated_events(self) -> None:
        await self.bot.handle(
            GatewayEvent(name="GROUP_MEMBER_ADD", message_id="x", envelope_id="e", data={}, seq=1, raw={})
        )
        self.assertEqual(self.api.sent, [])

    async def test_group_allowlist_is_enforced(self) -> None:
        bot = Bot(make_config(allowed_groups=frozenset({GROUP})), self.db, self.api, clock=lambda: NOW)
        await bot.handle(group_event("/充能面包", group=GROUP2))
        self.assertEqual(self.api.sent, [])


class InteractionTest(BotTestCase):
    async def test_acks_before_replying(self) -> None:
        """3 秒窗口：必须先应答，再去查库回复。"""
        order: list[str] = []
        original_ack = self.api.ack_interaction
        original_send = self.api.send_markdown

        async def ack(iid):
            order.append("ack")
            await original_ack(iid)

        async def send(*a, **kw):
            order.append("send")
            return await original_send(*a, **kw)

        self.api.ack_interaction = ack
        self.api.send_markdown = send

        await self.bot.handle(interaction_event("board:all:bread"))
        self.assertEqual(order[0], "ack", "应答必须在回复之前")
        self.assertEqual(self.api.acks, ["844c370e-2f8c-4c53-861c-8067f57ba7f6"])

    async def test_reply_uses_envelope_id_not_message_id(self) -> None:
        await self.bot.handle(interaction_event("board:group:bread"))
        self.assertEqual(
            self.last["event_id"], "INTERACTION_CREATE:844c370e-2f8c-4c53-861c-8067f57ba7f6"
        )
        self.assertIsNone(self.last["msg_id"], "用 event_id 时不能再带 msg_id")

    async def test_button_can_trigger_signin(self) -> None:
        await self.bot.handle(interaction_event("board:group:bread"))
        self.assertIn("面包排行榜", self.last["content"])

    async def test_ack_failure_still_replies(self) -> None:
        """应答失败不该把回复也搭进去 —— 用户至少该看到结果。"""
        self.api.ack_error = RuntimeError("ack 超时")
        await self.bot.handle(interaction_event("board:group:bread"))
        self.assertEqual(self.api.acks, [])
        self.assertEqual(len(self.api.sent), 1)

    async def test_slow_ack_does_not_delay_the_reply(self) -> None:
        """应答慢不能拖住回复 —— 两个是互相独立的 API 调用，谁也别等谁。"""
        import asyncio

        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_ack(iid):
            started.set()
            await release.wait()

        self.api.ack_interaction = slow_ack
        task = asyncio.create_task(
            self.bot.handle(interaction_event("board:group:bread"))
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            for _ in range(100):
                if self.api.sent:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(self.api.sent), 1, "应答还挂着时，回复就该已经发出")
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=2)

    async def test_non_button_interactions_are_ignored(self) -> None:
        await self.bot.handle(interaction_event("board:group:bread", type_=13))
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.api.acks, [], "非按钮回调不需要应答")

    async def test_acks_even_when_it_cannot_reply(self) -> None:
        """缺群上下文的回调（例如单聊）回复不了，但**应答必须发**，
        否则用户客户端一直转圈。"""
        event = interaction_event("board:group:bread")
        event.data.pop("group_openid")
        await self.bot.handle(event)
        for _ in range(100):
            if self.api.acks:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.api.acks, ["844c370e-2f8c-4c53-861c-8067f57ba7f6"])
        self.assertEqual(self.api.sent, [], "回复不了就不该发消息")

    async def test_unregistered_user_is_asked_to_sign_in_first(self) -> None:
        """按钮回调不带昵称，未建档用户只能显示成占位名 —— 先引导签到建档。"""
        await self.bot.handle(
            interaction_event("board:group:bread", user_id=UID2)
        )
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("充能面包", self.last["content"], "要告诉他怎么签到")
        self.assertNotIn("面包排行榜", self.last["content"], "不该直接给榜单")

    async def test_unregistered_user_does_not_get_a_profile_either(self) -> None:
        await self.bot.handle(interaction_event("board:group:bread", user_id=UID2))
        self.assertNotIn("我的面包", self.last["content"])

    async def test_registered_user_still_gets_the_board(self) -> None:
        await self.bot.handle(interaction_event("board:group:bread"))
        self.assertIn("面包排行榜", self.last["content"])

    async def test_unknown_button_payload_shows_menu(self) -> None:
        await self.bot.handle(interaction_event("garbage-payload"))
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("充能面包", self.last["content"])


class RenameTest(BotTestCase):
    async def test_rename_changes_the_display_name(self) -> None:
        await self.bot.handle(group_event("/改名 面包大王"))
        self.assertIn("面包大王", self.last["content"])
        self.assertEqual(self.db.get_user(UID).nickname, "面包大王")

    async def test_custom_name_survives_later_events(self) -> None:
        """自设名字之后，事件里的 QQ 昵称不该再覆盖它 —— 这是"昵称变化"的答案。"""
        await self.bot.handle(group_event("/改名 面包大王"))
        await self.bot.handle(group_event("/充能面包", message_id="m2", envelope="e2"))
        self.assertIn("面包大王", self.last["content"])
        self.assertNotIn("小明", self.last["content"])

    async def test_rename_strips_markdown_symbols(self) -> None:
        await self.bot.handle(group_event("/改名 **坏**名字"))
        stored = self.db.get_user(UID).nickname
        self.assertNotIn("*", stored)
        self.assertIn("坏", stored)

    async def test_rename_without_argument_shows_usage(self) -> None:
        await self.bot.handle(group_event("/改名"))
        self.assertIn("用法", self.last["content"])

    async def test_rename_default_restores_qq_nickname(self) -> None:
        await self.bot.handle(group_event("/改名 面包大王"))
        await self.bot.handle(group_event("/改名 默认", message_id="m2", envelope="e2"))
        self.assertFalse(self.db.get_user(UID).nickname_set)
        # 之后事件里的 QQ 昵称重新生效
        await self.bot.handle(group_event("/充能面包", message_id="m3", envelope="e3"))
        self.assertIn("小明", self.last["content"])

    async def test_menu_mentions_rename(self) -> None:
        await self.bot.handle(group_event("/帮助"))
        self.assertIn("改名", self.last["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
