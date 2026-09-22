"""接线层：把网关事件翻译成签到/榜单/个人页的动作。

几条来自实测的硬约束：
  · 按钮回调必须**先应答再回复**。应答窗口只有 3 秒，而回复要查数据库，
    顺序反了就必然超时（探针阶段真的超时过，10.5 秒）。
  · 回复按钮回调用 `event_id`（外层信封 id），普通回复用 `msg_id`，**二选一**。
  · 按钮回调事件里**没有昵称**（只有 group_member_openid），
    所以昵称要从库里读 —— 之前群消息已经把昵称存进去了。
  · 机器人自己发的消息要忽略，否则会自己跟自己对话。
  · 平台会重复推送同一个事件，同一条消息只能处理一次。
  · 看不懂的命令就发菜单：沉默比乱答更让人困惑。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Callable

from . import api, commands, game, render
from .api import ApiError
from .config import Config
from .db import Database
from .gateway import GatewayEvent
from .rules import LEADERBOARD_TOP_N, days_to_next_card

log = logging.getLogger("chargebread.bot")

# 只处理这类按钮回调（INLINE_KEYBOARD）
INTERACTION_MESSAGE_BUTTON = 11

BREAD_BOARD_TITLE = "面包排行榜"
SIGNIN_BOARD_TITLE = "签到排行榜"


def event_moment(data: dict, tz=None) -> datetime | None:
    """取事件自带的 timestamp —— 用户**按下那一刻**，不是我们处理那一刻。

    名次按它算，所以链路卡顿、发送重试、事件重投都不会算错顺序。
    平台实测带 +08:00 偏移，但**不能赌**：没有偏移的朴素时间戳直接 astimezone
    会拿系统时区凑数，部署在 UTC 裸机上游戏日就偏 8 小时 —— 所以无偏移时
    显式挂上机器人时区。解析失败返回 None，调用方退回服务器时间。
    """
    raw = data.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("事件时间戳无法解析：%r", raw)
        return None
    if moment.tzinfo is None and tz is not None:
        moment = moment.replace(tzinfo=tz)
    return moment


class Bot:
    def __init__(
        self,
        config: Config,
        db: Database,
        api: Any,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.api = api
        self._clock = clock or (lambda: datetime.now(config.tz))
        self._pending_acks: set[asyncio.Task] = set()

    # ---- 入口 -------------------------------------------------------------
    async def handle(self, event: GatewayEvent) -> None:
        if event.name == "GROUP_AT_MESSAGE_CREATE":
            await self._on_group_message(event)
        elif event.name == "INTERACTION_CREATE":
            await self._on_interaction(event)
        elif event.name == "GROUP_MEMBER_ADD":
            await self._on_member_add(event)
        else:
            log.debug("忽略事件 %s", event.name)

    # ---- 群消息 -----------------------------------------------------------
    async def _on_group_message(self, event: GatewayEvent) -> None:
        data = event.data
        author = data.get("author") or {}
        if author.get("bot"):
            return  # 别理自己

        group_openid = str(data.get("group_openid") or "")
        user_id = str(author.get("id") or "")
        if not group_openid or not user_id:
            log.warning("群消息缺少 group_openid 或 author.id，已忽略")
            return
        if self.config.allowed_groups and group_openid not in self.config.allowed_groups:
            log.debug("群 %s 不在白名单内，已忽略", group_openid)
            return

        now = self._clock()
        key = event.message_id or event.envelope_id
        if not self.db.mark_event_seen(key, now.isoformat()):
            return

        try:
            self.db.upsert_group(group_openid, None, now.isoformat())
            nickname = str(author.get("username") or "")
            self.db.upsert_user(user_id, "group", nickname, now.isoformat())

            cmd = commands.parse(str(data.get("content") or ""))
            await self._dispatch(
                cmd,
                group_openid=group_openid,
                user_id=user_id,
                nickname=nickname,
                msg_id=event.message_id,
                event_id=None,
                signed_at=event_moment(data, self.config.tz),
            )
        except Exception:
            # 处理失败就撤销"已处理"标记：否则事件被记成处理过，
            # 平台重投也会被丢掉，用户永远收不到回复（实测报的"漏请求"）。
            self.db.unmark_event(key)
            raise

    # ---- 新成员入群 -------------------------------------------------------
    async def _on_member_add(self, event: GatewayEvent) -> None:
        """新成员入群时发欢迎语。

        这个事件三处与常规不同，都得兜住：
          · `d` 里**没有 `id`**，所以没有 msg_id 可用；只能拿**外层信封 id**
            当 `event_id` 做被动回复。文档说 event_id 只支持三种事件、不含
            GROUP_MEMBER_ADD，但实测有其他官方机器人这么用（且未启用主动消息），
            所以先试被动。
          · 万一被动回复被拒（40034027 该事件不支持回复），退回**主动消息** ——
            那条路要群管理员开着"允许主动发送"，失败了只记日志。
          · `d` 里**没有昵称**，所以写不出"@昵称"；但平台支持用 openid 提及
            （`<qqbot-at-user id="" />`），名字由客户端渲染，不需要我们拿到。
        """
        if not self.config.welcome_enabled:
            return
        data = event.data
        group_openid = str(data.get("group_openid") or "")
        member_openid = str(data.get("member_openid") or "")
        if not group_openid or not member_openid:
            log.debug("入群事件缺少群或成员标识，已忽略")
            return
        if self.config.allowed_groups and group_openid not in self.config.allowed_groups:
            log.debug("群 %s 不在白名单内，不发欢迎语", group_openid)
            return

        now = self._clock()
        # 这个事件没有 d.id，用信封 id 兜底；实在没有再拼一个
        key = event.envelope_id or f"GROUP_MEMBER_ADD:{group_openid}:{data.get('timestamp')}"
        if not self.db.mark_event_seen(key, now.isoformat()):
            return

        try:
            self.db.upsert_group(group_openid, None, now.isoformat())
            await self._send_welcome(group_openid, member_openid, event.envelope_id)
        except Exception:
            self.db.unmark_event(key)
            raise

    async def _send_welcome(
        self, group_openid: str, member_openid: str, envelope_id: str
    ) -> None:
        """依次尝试三条路，任何一条成功就结束。

          1) 带 @ 的**被动回复**（首选 —— 官方机器人普遍这么用，且不需要主动消息额度）
          2) 去掉 @ 的被动回复（@ 标签是我们动态拼进 content 的，社区有反馈这种
             传参会被平台拒；欢迎语本身比 @ 重要，不能为它整条丢掉）
          3) **主动消息**（平台不接受回复这个事件时；需要群内开启「允许主动发送」）

        第 3 步会尽量保住 @ —— 如果前面的失败原因跟 @ 无关（只是事件不支持回复），
        @ 本来是好的，不该白丢。
        """
        keyboard = render.kb_welcome()

        async def send(text: str, anchor: str | None) -> None:
            await self.api.send_markdown(
                group_openid, text, keyboard=keyboard, event_id=anchor, msg_seq=1
            )

        with_mention = render.welcome_text(mention=render.mention_tag(member_openid), image_url=self.config.image_url)
        without_mention = render.welcome_text(mention="", image_url=self.config.image_url)

        mention_usable = True
        if envelope_id:
            try:
                await send(with_mention, envelope_id)
                return
            except ApiError as exc:
                if exc.code in api.MARKDOWN_CONTENT_ERRORS:
                    log.warning("带 @ 的欢迎语被平台拒绝（%s），去掉 @ 再试", exc.code)
                    mention_usable = False
                elif exc.code == api.EVENT_REPLY_NOT_SUPPORTED:
                    log.info("平台不支持被动回复入群事件（%s），改走主动消息", exc.code)
                else:
                    raise
            try:
                await send(without_mention, envelope_id)
                return
            except ApiError as exc:
                if exc.code != api.EVENT_REPLY_NOT_SUPPORTED:
                    log.warning("被动回复入群事件失败（%s）", exc.code, exc_info=True)

        try:
            await send(with_mention if mention_usable else without_mention, None)
        except Exception:  # noqa: BLE001 - 欢迎语失败不该影响其它事件
            log.warning(
                "欢迎语没发出去（被动与主动都没成功）。若群内未开启「允许主动发送」，"
                "主动那条必然失败 —— 需要群管理员在机器人资料页打开该开关。",
                exc_info=True,
            )

    # ---- 按钮回调 ---------------------------------------------------------
    async def _on_interaction(self, event: GatewayEvent) -> None:
        data = event.data
        if data.get("type") != INTERACTION_MESSAGE_BUTTON:
            return  # 其它互动类型不需要应答也不需要回复

        # 无论能不能回复，都要**先**应答 —— 否则用户客户端一直转圈。
        # 单聊场景的回调只有 user_openid、没有 group_openid，回复不了，
        # 但应答依然必须发出去。
        self._launch_ack(event.message_id)
        await asyncio.sleep(0)

        user_id = str(data.get("group_member_openid") or "")
        group_openid = str(data.get("group_openid") or "")
        if not group_openid or not user_id:
            log.info("按钮回调缺少群上下文（可能来自单聊），已应答但不回复")
            return

        if self.config.allowed_groups and group_openid not in self.config.allowed_groups:
            return

        now = self._clock()
        key = event.message_id or event.envelope_id
        if not self.db.mark_event_seen(key, now.isoformat()):
            return

        try:
            # 按钮回调里**没有昵称**，未建档的用户只能显示成"面包学徒#a3f9"这种
            # 占位名 —— 榜单和个人页上会冒出一个并不存在的身份。
            # 所以统一先引导他签到建档：签到那条消息带着真实昵称。
            if self.db.signin_count(user_id) == 0:
                log.info("按钮来自尚未签到的用户 %s，先引导签到建档", user_id)
                await self._send(
                    group_openid,
                    render.need_signin_text(),
                    render.kb_menu(),
                    msg_id=None,
                    event_id=event.envelope_id,
                )
                return

            self.db.upsert_group(group_openid, None, now.isoformat())
            # 走到这里必然已有档案（上面的签到次数门槛保证 upsert_user 已跑过；
            # 外键约束也已通过 PRAGMA foreign_keys=ON 真正生效）
            nickname = self.db.get_user(user_id).nickname
            self.db.upsert_user(user_id, "group", nickname, now.isoformat())

            resolved = (data.get("data") or {}).get("resolved") or {}
            cmd = commands.parse_callback(str(resolved.get("button_data") or ""))
            await self._dispatch(
                cmd,
                group_openid=group_openid,
                user_id=user_id,
                nickname=nickname,
                msg_id=None,
                event_id=event.envelope_id,
                signed_at=event_moment(data, self.config.tz),
            )
        except Exception:
            self.db.unmark_event(key)
            raise

    def _launch_ack(self, interaction_id: str) -> None:
        if not interaction_id:
            return
        task = asyncio.create_task(self._ack_quietly(interaction_id))
        self._pending_acks.add(task)
        task.add_done_callback(self._pending_acks.discard)

    async def _ack_quietly(self, interaction_id: str) -> None:
        """应答失败只记日志，绝不往上抛 —— 用户至少该拿到回复。

        注意日志里说"失败"未必真的失败：客户端超时只是我们不等了，
        服务端很可能已经受理（这也是 ACK_TIMEOUT 要比平台窗口宽的原因）。
        """
        try:
            await self.api.ack_interaction(interaction_id)
        except Exception:  # noqa: BLE001
            log.warning(
                "按钮应答未确认 interaction_id=%s（可能只是等超时了，服务端未必没收到）",
                interaction_id,
                exc_info=True,
            )

    # ---- 动作分发 ---------------------------------------------------------
    async def _dispatch(
        self,
        cmd: commands.Command,
        *,
        group_openid: str,
        user_id: str,
        nickname: str,
        msg_id: str | None,
        event_id: str | None,
        signed_at: datetime | None = None,
    ) -> None:
        if cmd.name == commands.SIGNIN:
            text = self._do_signin(group_openid, user_id, nickname, signed_at)
            keyboard = render.kb_signin()
        elif cmd.name == commands.BOARD_BREAD:
            text, keyboard = self._do_bread_board(cmd, group_openid, user_id, nickname)
        elif cmd.name == commands.BOARD_SIGNIN:
            text, keyboard = self._do_signin_board(cmd, group_openid, user_id, nickname)
        elif cmd.name == commands.PROFILE:
            text = self._do_profile(user_id, nickname)
            keyboard = render.kb_profile()
        elif cmd.name == commands.MAKEUP:
            text = self._do_makeup(group_openid, user_id, nickname)
            keyboard = render.kb_makeup()
        elif cmd.name == commands.CHARGE:
            text = self._do_charge(user_id, nickname)
            keyboard = render.kb_charge()
        elif cmd.name == commands.RENAME:
            text = self._do_rename(user_id, cmd.arg)
            keyboard = render.kb_menu()
        else:
            text = render.menu_text()
            keyboard = render.kb_menu()

        await self._send(group_openid, text, keyboard, msg_id=msg_id, event_id=event_id)

    # ---- 各动作 -----------------------------------------------------------
    def _today(self) -> date:
        return game.game_day(self._clock(), self.config.tz, self.config.reset_hour)

    def _streak(self, user_id: str) -> int:
        signed = {
            datetime.strptime(d, "%Y-%m-%d").date()
            for d in self.db.signed_dates(user_id, limit=400)
        }
        return game.current_streak(signed, self._today())

    def _do_signin(
        self,
        group_openid: str,
        user_id: str,
        nickname: str,
        signed_at: datetime | None = None,
    ) -> str:
        outcome = game.sign_in(
            self.db,
            user_id=user_id,
            nickname=nickname,
            group_openid=group_openid,
            now=self._clock(),
            tz=self.config.tz,
            reset_hour=self.config.reset_hour,
            signed_at=signed_at,
        )
        display = self._display_name(user_id, nickname)
        if outcome.status == game.STATUS_OK:
            return render.signin_text(
                nickname=display,
                rank=outcome.rank,
                bread=outcome.bread,
                streak=outcome.streak,
                total_bread=outcome.total_bread,
                total_today=self.db.today_total(self._today().isoformat()),
                image_url=self.config.image_url,
            )
        row = outcome.existing
        return render.already_text(
            nickname=display,
            rank=outcome.rank,
            bread=row.bread if row else outcome.bread,
            streak=outcome.streak,
            total_bread=outcome.total_bread,
        )

    def _do_bread_board(
        self, cmd: commands.Command, group_openid: str, user_id: str, nickname: str
    ) -> tuple[str, dict]:
        scope = cmd.scope or commands.SCOPE_GROUP
        if scope == commands.SCOPE_ALL:
            entries = self.db.board_global(LEADERBOARD_TOP_N)
            rank, _ = self.db.rank_in_global_board(user_id)
            label = "全部"
        else:
            entries = self.db.board_group(group_openid, LEADERBOARD_TOP_N)
            rank, _ = self.db.rank_in_group_board(user_id, group_openid)
            label = "本群"
        me = render.MeStanding(
            user_id=user_id,
            nickname=self._display_name(user_id, nickname),
            value=self.db.total_bread(user_id),
            rank=rank,
        )
        text = render.board_text(
            title=BREAD_BOARD_TITLE,
            entries=entries,
            scope=scope,
            scope_label=label,
            value_suffix=" 个🍞",
            me=me,
            tz=self.config.tz,
        )
        return text, render.kb_board(scope, "bread")

    def _do_signin_board(
        self, cmd: commands.Command, group_openid: str, user_id: str, nickname: str
    ) -> tuple[str, dict]:
        scope = cmd.scope or commands.SCOPE_GROUP
        today_iso = self._today().isoformat()
        if scope == commands.SCOPE_ALL:
            entries = self.db.today_order(today_iso, LEADERBOARD_TOP_N)
            label = "今天 · 全部"
        else:
            entries = self.db.today_order(today_iso, LEADERBOARD_TOP_N, group_openid)
            label = "今天 · 本群"

        # 数值不显示"第几个"，改显示**签到时刻** —— 实测反馈"个"含义不明，
        # 而且时刻能同时消掉"本群顺序 vs 全部序号不一致"的困惑。
        row = self.db.get_signin(today_iso, user_id)
        if row is not None:
            me = render.MeStanding(
                user_id=user_id,
                nickname=self._display_name(user_id, nickname),
                value=row.seq_in_day,
                rank=row.seq_in_day,
                signed_at=row.signin_at or None,
            )
        else:
            # 面包榜对没签到的人也会给"你"行，签到榜不该沉默 —— 说清没签
            me = render.MeStanding(
                user_id=user_id,
                nickname=self._display_name(user_id, nickname),
                value=0,
                rank=0,
                note="今天还没签到",
            )
        text = render.board_text(
            title=SIGNIN_BOARD_TITLE,
            entries=entries,
            scope=scope,
            scope_label=label,
            value_suffix="",
            me=me,
            tz=self.config.tz,
        )
        return text, render.kb_board(scope, "signin")

    def _do_charge(self, user_id: str, nickname: str) -> str:
        """今日充能指数。纯推导，不写库、不发面包、不影响奖励。"""
        today = self._today()
        reading = game.charge_index(user_id, today)
        signed = self.db.get_signin(today.isoformat(), user_id) is not None
        return render.charge_text(
            nickname=self._display_name(user_id, nickname),
            reading=reading,
            signed_today=signed,
        )

    def _do_profile(self, user_id: str, nickname: str) -> str:
        today_iso = self._today().isoformat()
        row = self.db.get_signin(today_iso, user_id)
        cards, _ = self.db.makeup_state(user_id)
        streak = self._streak(user_id)
        return render.profile_text(
            nickname=self._display_name(user_id, nickname),
            rank=row.seq_in_day if row else 0,
            total_bread=self.db.total_bread(user_id),
            total_today=self.db.today_total(today_iso),
            streak=streak,
            best_streak=self.db.best_streak(user_id),
            signin_count=self.db.signin_count(user_id),
            cards=cards,
            days_to_card=days_to_next_card(streak),
            charge=game.charge_index(user_id, self._today()),
        )

    def _do_makeup(self, group_openid: str, user_id: str, nickname: str) -> str:
        outcome = game.make_up(
            self.db,
            user_id=user_id,
            nickname=nickname,
            group_openid=group_openid,
            now=self._clock(),
            tz=self.config.tz,
            reset_hour=self.config.reset_hour,
        )
        if outcome.status == game.STATUS_OK:
            return render.makeup_text(
                target_date=outcome.target_date.isoformat(),
                bread=outcome.bread,
                streak=outcome.streak,
                total_bread=outcome.total_bread,
                cards_balance=outcome.cards_balance,
            )
        if outcome.status == game.STATUS_NO_CARD:
            streak = self._streak(user_id)
            return "\n".join(
                [
                    "# 🍞 没有补签卡",
                    "",
                    "补签卡靠连签攒：**每连签 7 天送 1 张**。",
                    "",
                    f"- 你的当前连签：**{streak}** 天",
                    f"- 再连签 **{days_to_next_card(streak)}** 天就能拿到下一张",
                    "",
                    "补签卡用来补回**昨天**，把断掉的连签接上。",
                ]
            )
        return "\n".join(
            [
                "# 🍞 不需要补签",
                "",
                f"**{outcome.target_date.isoformat()}** 已经签过了，没有断口要补。",
            ]
        )

    def _do_rename(self, user_id: str, arg: str) -> str:
        """改显示名。

        净化在**入参**做（不是只在渲染时做），否则用户输入 `**坏**` 却看到
        `坏`，会觉得是 bug。渲染层仍然会再净化一次，作为纵深防御。
        """
        wanted = render.safe_name(arg, fallback="")
        if not wanted:
            return "\n".join(
                [
                    "# 🍞 改个名字",
                    "",
                    "用法：/改名 你的新名字",
                    "",
                    "例：/改名 面包大王",
                    "",
                    "想改回 QQ 昵称：/改名 默认",
                ]
            )
        if wanted in ("默认", "default"):
            self.db.clear_nickname(user_id, self._clock().isoformat())
            return "\n".join(
                [
                    "# 🍞 已改回 QQ 昵称",
                    "",
                    "下次你说话时，我会用你最新的 QQ 昵称。",
                ]
            )
        self.db.set_nickname(user_id, wanted, self._clock().isoformat())
        return "\n".join(
            [
                "# 🍞 改名成功",
                "",
                f"以后榜单上你叫 **{wanted}**",
                "",
                "想改回去：/改名 默认",
            ]
        )

    def _display_name(self, user_id: str, nickname: str) -> str:
        """展示名以**库中为准**。

        库里那一行已经处理过"自设名字优先于 QQ 昵称"的规则
        （见 db.upsert_user），而事件里的 username 只是原始输入。
        早先这里优先用事件昵称，结果用户改过名之后仍被 QQ 昵称盖掉。
        """
        try:
            return self.db.get_user(user_id).nickname
        except KeyError:
            return nickname

    # ---- 出站 -------------------------------------------------------------
    async def _send(
        self,
        group_openid: str,
        text: str,
        keyboard: dict,
        *,
        msg_id: str | None,
        event_id: str | None,
    ) -> None:
        # 同一条消息/事件的多次回复必须用递增且不重复的 msg_seq
        seq_key = event_id or msg_id
        seq = self.api.next_seq(seq_key) if seq_key else 1
        await self.api.send_markdown(
            group_openid,
            text,
            keyboard=keyboard,
            msg_id=msg_id,
            event_id=event_id,
            msg_seq=seq,
        )
