"""把领域结果渲染成 QQ 群聊能显示的 markdown 与按钮。

平台能力是实测确认过的，这里的每条格式约束都有依据：
  · 只支持 `#` 和 `##` 两级标题；**不支持表格、不支持代码块** —— 所以这里
    一律用列表排版，绝不出现竖线和围栏。
  · 图片语法 `![alt #Wpx #Hpx](url)`，图片 URL 由开放平台下载转存，
    实测不需要域名报备；但**图片前必须换行**，否则文字会和图片挤在一起。
  · 按钮 label 最多 10 个字、最多 5 行 × 每行 5 个，超限报 40034029。
    markdown 内容是必填的，不能只发按钮。
  · 命令类按钮用 action.type=2（替用户把命令填进输入框），
    交互类按钮用 type=1（回调到我们这边）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import BoardEntry
from .rules import STREAK_BONUS_CAP_DAYS, streak_bonus

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
BREAD = "🍞"
IMAGE_ALT = "充能面包"
IMAGE_SIZE = "#160px #160px"

# 昵称净化：只去掉**能注入内容**或**能破坏我们自己的结构**的字符。
#
# 去掉的：
#   []()!<>  可以拼成图片/链接，把东西注入进我们的消息
#   *        昵称里的 * 会和我们包裹用的 ** 配对错乱，把加粗拆坏
#   `#|      行内代码/标题/表格的结构符，平台支持与否都不该从昵称里冒出来
#
# 特意**保留**的：
#   _ 和 ~   都是用户名里的常见字符（`Hesitate_P`、`小明~`），删了会把真实昵称
#            改坏。它们最多让名字显示成斜体或带删除线 —— 纯外观，注入不了
#            任何东西，也拆不坏 ** 包裹（单个符号配不成对）。
#            外观风险的代价远小于改坏名字。
_NAME_DROP = str.maketrans({c: None for c in "*`#[]()!|<>\\"})

# 零宽与双向控制符：不可见但能制造"名字是空的""文字方向翻转"这类乱象
# （U+200B 零宽空格还是平台文档里"强制换行"的手法），一律去掉。
_INVISIBLE = {
    "\u200b",  # ZERO WIDTH SPACE
    "\u200c",  # ZERO WIDTH NON-JOINER
    "\u200d",  # ZERO WIDTH JOINER
    "\u2060",  # WORD JOINER
    "\ufeff",  # ZERO WIDTH NO-BREAK SPACE (BOM)
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",  # 双向覆盖/嵌入
    "\u2066", "\u2067", "\u2068", "\u2069",  # 隔离控制符
}

NAME_FALLBACK = "面包学徒"
NAME_MAX_LEN = 24


def safe_name(name: str, *, fallback: str = NAME_FALLBACK, max_len: int = NAME_MAX_LEN) -> str:
    """把用户昵称净化成能安全嵌进 markdown 的文本。

    昵称是**用户可控**的输入，而它会被拼进我们自己的 markdown 结构里
    （`**{nickname}**`、榜单每一行）。所以这里：
      · 去掉有结构含义的符号，防止注入图片/链接/强调
      · 折叠换行与连续空白，防止撑破"一行一条"的榜单
      · 去掉零宽/双向控制符，防止"隐形名字"和文字方向翻转
      · 限长，防止超长昵称把一行挤爆
    """
    if not name:
        return fallback
    cleaned = name.translate(_NAME_DROP)
    for ch in _INVISIBLE:
        cleaned = cleaned.replace(ch, "")
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip()
    if not cleaned:
        return fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + "…"
    return cleaned



@dataclass(frozen=True)
class MeStanding:
    """请求者在榜单之外时的自身成绩，用于底部单列一行。"""

    user_id: str
    nickname: str
    value: int
    rank: int
    signed_at: str | None = None
    # 不走"名次/数值"展示时的替代文案，如签到榜上的"今天还没签到"
    note: str | None = None


def format_clock(signed_at: str | None, tz: ZoneInfo | None = None) -> str:
    """把事件时间戳格式化成 HH:MM:SS。

    时间戳带时区偏移（实测是 +08:00），所以要**按机器人时区换算**再取时分秒，
    不能直接切字符串 —— 换个偏移量就全错了。
    解不出来就返回空串，让调用方退回显示数值。
    """
    if not signed_at:
        return ""
    try:
        moment = datetime.fromisoformat(signed_at)
    except (TypeError, ValueError):
        return ""
    if tz is not None:
        moment = moment.astimezone(tz)
    return moment.strftime("%H:%M:%S")


def _value_text(entry_value: int, signed_at: str | None, suffix: str, tz: ZoneInfo | None) -> str:
    """有签到时刻就显示时刻，否则退回"数值 + 后缀"。"""
    clock = format_clock(signed_at, tz)
    if clock:
        return clock
    if suffix:
        return f"{entry_value}{suffix}"
    # 签到榜的旧记录（迁移前没有 signin_at）既没时间也没单位：
    # 退回「第N个」，总比显示一个光秃秃的数字强。
    return f"第{entry_value}个"


def _streak_line(streak: int) -> str:
    bonus = streak_bonus(streak)
    if bonus <= 0:
        return f"当前连签 **{streak}** 天"
    if streak >= STREAK_BONUS_CAP_DAYS:
        return f"连签 **{streak}** 天 · 加成已封顶 +{bonus:.0%}"
    return f"连签 **{streak}** 天 · 加成 +{bonus:.0%}"


def _rank_medal(rank: int) -> str:
    """句尾奖牌。**只有前三名有** —— 第 4 名起返回空串。

    之前这里复用了 `_badge()`，于是第 4 名会渲染成
    「今天第 4. **4** 个签到」：一个序号徽章插进句子里，还和后面的数字重复，
    读起来毫无意义（实测反馈）。
    """
    return MEDALS.get(rank, "")


def _badge(rank: int) -> str:
    """列表里的名次前缀：**序号 + 奖牌**，例如 `1、🥇`。

    分隔符用中文顿号 `、` 而**不是 ASCII 句点**：`数字.` + 空格是 CommonMark 的
    有序列表标记，渲染器会给它**自动重新编号**（还会在列表被任何空行打断时
    从头开始）。曾经写成 `1.🥇 `（无空格）与 `4. `（有空格）混用，结果同一个
    榜单里前三行是普通段落、后面是 ordered list，不同客户端显示完全不同 ——
    安卓端后半段从 1 重新编号，另一个客户端合成 1–7（实测反馈，两张截图）。
    `、` 对 markdown 没有任何含义，所以谁来渲染都是我们写的那个数字。
    """
    return f"{rank}、{MEDALS.get(rank, '')}"


def signin_text(
    *,
    nickname: str,
    rank: int,
    bread: int,
    streak: int,
    total_bread: int,
    total_today: int,
    image_url: str,
) -> str:
    """签到成功的回复。图片单独占一行，前面留空行。"""
    head = f"# {BREAD} 充能完成"
    if rank == 1:
        head = f"# {BREAD} 充能完成 · 今日第一！"

    lines = [
        head,
        "",
        f"**{safe_name(nickname)}** 今天第{rank}个签到{_rank_medal(rank)}",
        "",
        f"- 获得 **{bread}** 个面包 {BREAD}",
        f"- {_streak_line(streak)}",
        f"- 累计 **{total_bread}** 个面包 · 今天已有 **{total_today}** 人签到",
        "",
        f"![{IMAGE_ALT} {IMAGE_SIZE}]({image_url})",
    ]
    return "\n".join(lines)


def already_text(
    *, nickname: str, rank: int, bread: int, streak: int, total_bread: int
) -> str:
    """今天已经签过时的回复。不重复发面包，但要告诉他今天的成绩。"""
    return "\n".join(
        [
            f"# {BREAD} 今天已经签过啦",
            "",
            f"**{safe_name(nickname)}** 今天第{rank}个签到{_rank_medal(rank)}，拿了 **{bread}** 个面包",
            "",
            f"- {_streak_line(streak)}",
            f"- 累计 **{total_bread}** 个面包",
            "",
            f"明天再来，零点刷新 {BREAD}",
        ]
    )


def makeup_text(
    *, target_date: str, bread: int, streak: int, total_bread: int, cards_balance: int
) -> str:
    return "\n".join(
        [
            f"# {BREAD} 补签成功",
            "",
            f"补回了 **{target_date}** 的签到，面包 +**{bread}** 个",
            "",
            f"- {_streak_line(streak)}",
            f"- 累计 **{total_bread}** 个面包",
            f"- 补签卡剩余 **{cards_balance}** 张",
            "",
            "补签不计入当日排名，只为把连签接上。",
        ]
    )


def board_text(
    *,
    title: str,
    entries: list[BoardEntry],
    scope: str,
    scope_label: str,
    value_suffix: str,
    me: MeStanding | None = None,
    tz: ZoneInfo | None = None,
) -> str:
    """排行榜。前三名用奖牌，自己不在榜上时底部单列一行。

    条目带 `signed_at` 时显示签到时刻（HH:MM:SS），否则显示"数值 + 后缀"。
    """
    lines = [f"# {BREAD} {title}", f"## {scope_label}", ""]

    if not entries:
        lines.append("还没有人签到，你可以是第一个 " + BREAD)
    else:
        for index, entry in enumerate(entries, 1):
            value = _value_text(entry.value, entry.signed_at, value_suffix, tz)
            lines.append(f"{_badge(index)} **{safe_name(entry.nickname)}** — {value}")

    listed = {entry.user_id for entry in entries}
    if me is not None and me.user_id not in listed:
        if me.note:
            lines += ["", f"你：**{safe_name(me.nickname)}** — {me.note}"]
        else:
            who = f"第 {me.rank} 名 · " if me.rank > 0 else ""
            value = _value_text(me.value, me.signed_at, value_suffix, tz)
            lines += ["", f"你：{who}**{safe_name(me.nickname)}** — {value}"]

    if scope == "all":
        lines += ["", "_统计范围：机器人服务的所有群_"]
    return "\n".join(lines)


def profile_text(
    *,
    nickname: str,
    rank: int,
    total_bread: int,
    total_today: int,
    streak: int,
    best_streak: int,
    signin_count: int,
    cards: int,
    days_to_card: int,
) -> str:
    rank_line = f"今日排名：第 **{rank}** 名（今天已有 {total_today} 人签到）" if rank else "今日还没签到"
    card_line = (
        f"补签卡：**{cards}** 张"
        if cards
        else f"补签卡：**0** 张（再连签 {days_to_card} 天得下一张）"
    )
    return "\n".join(
        [
            f"# {BREAD} 我的面包",
            "",
            f"**{safe_name(nickname)}**",
            "",
            f"- 累计面包：**{total_bread}** 个",
            f"- {rank_line}",
            f"- {_streak_line(streak)}",
            f"- 历史最高连签：**{best_streak}** 天",
            f"- 累计签到：**{signin_count}** 天",
            f"- {card_line}",
        ]
    )


def need_signin_text() -> str:
    """按钮来自尚未建档的用户时的统一提醒。

    按钮回调事件里**没有昵称**（只有 group_member_openid），所以未建档的用户
    只能显示成"面包学徒#a3f9"这种占位名 —— 榜单和个人页上会冒出一个并不存在
    的身份。先引导他签到一次：签到那条消息带着真实昵称，档案就建起来了。
    """
    return "\n".join(
        [
            f"# {BREAD} 先签到，再用按钮",
            "",
            "我还不知道你是谁 —— 按钮回调里不带昵称，只有你先说过话，",
            "我才能把你的名字和面包数记下来。",
            "",
            "在群里 @我 发送 **充能面包**，签一次到就好：",
            "",
            "- 签到会记下你的昵称，之后按钮就认得你了",
            f"- 顺便领当天的面包 {BREAD}",
        ]
    )


def menu_text() -> str:
    return "\n".join(
        [
            f"# {BREAD} 充能面包",
            "",
            "每天签到领面包，零点刷新。全球每人每天只能签一次，抢第一个有额外加成。",
            "",
            "- /充能面包 — 今日签到，领取面包",
            "- /面包排行榜 本群 或 /面包排行榜 全部 — 面包总数榜",
            "- /签到排行榜 本群 或 /签到排行榜 全部 — 今日签到顺序榜",
            "- /我的面包 — 个人详情",
            "- /补签 — 用补签卡补回昨天，接上连签",
            "- /改名 新名字 — 换个显示名",
            "",
            "也可以直接点下面的按钮。",
        ]
    )


# ---- 按钮 --------------------------------------------------------------------
def _button(label: str, action_type: int, data: str, style: int, index: int) -> dict:
    return {
        "id": f"btn{index}",
        "render_data": {"label": label, "visited_label": label, "style": style},
        "action": {
            "type": action_type,
            "permission": {"type": 2},  # 2 = 所有人可点
            "data": data,
            "unsupport_tips": "请升级 QQ 版本后使用按钮",
        },
    }


def _keyboard(rows: list[list[tuple[str, int, str, int]]]) -> dict:
    out_rows = []
    index = 0
    for row in rows:
        buttons = []
        for label, action_type, data, style in row:
            buttons.append(_button(label, action_type, data, style, index))
            index += 1
        out_rows.append({"buttons": buttons})
    return {"content": {"rows": out_rows}}


STYLE_GRAY = 0
STYLE_BLUE = 1
CMD = 2  # 指令按钮：替用户把命令填进输入框
CALLBACK = 1  # 回调按钮：把 data 发回我们的后端


def kb_signin() -> dict:
    return _keyboard(
        [
            [
                ("我的面包", CMD, "我的面包", STYLE_BLUE),
                ("面包排行榜", CMD, "面包排行榜", STYLE_GRAY),
                ("签到排行榜", CMD, "签到排行榜", STYLE_GRAY),
            ],
            [("补签", CMD, "补签", STYLE_GRAY)],
        ]
    )


def kb_menu() -> dict:
    return _keyboard(
        [
            [
                ("签到", CMD, "充能面包", STYLE_BLUE),
                ("我的面包", CMD, "我的面包", STYLE_GRAY),
            ],
            [
                ("面包排行榜", CMD, "面包排行榜", STYLE_GRAY),
                ("签到排行榜", CMD, "签到排行榜", STYLE_GRAY),
            ],
            [
                ("补签", CMD, "补签", STYLE_GRAY),
                ("帮助", CMD, "帮助", STYLE_GRAY),
                ("改名", CMD, "改名 ", STYLE_GRAY),
            ],
        ]
    )


def kb_profile() -> dict:
    """个人页的按钮：第一排是**动作**（签到在前 —— 实测反馈第一个该是签到
    而不是补签），第二排是**看榜**。两排各两个，手机上点着不挤。"""
    return _keyboard(
        [
            [
                # 标签叫「签到」比「充能面包」好懂；data 才是真正填进输入框的命令
                ("签到", CMD, "充能面包", STYLE_BLUE),
                ("补签", CMD, "补签", STYLE_GRAY),
            ],
            [
                ("面包排行榜", CMD, "面包排行榜", STYLE_GRAY),
                ("签到排行榜", CMD, "签到排行榜", STYLE_GRAY),
            ],
        ]
    )


def kb_board(scope: str, kind: str) -> dict:
    """排行榜上的按钮：切换统计范围、切换榜单类型、刷新。"""
    other_scope = "all" if scope == "group" else "group"
    other_scope_label = "全部榜" if other_scope == "all" else "本群榜"
    other_kind = "signin" if kind == "bread" else "bread"
    other_kind_label = "签到榜" if other_kind == "signin" else "面包榜"
    return _keyboard(
        [
            [
                (other_scope_label, CALLBACK, f"board:{other_scope}:{kind}", STYLE_BLUE),
                (other_kind_label, CALLBACK, f"board:{scope}:{other_kind}", STYLE_GRAY),
                ("刷新", CALLBACK, f"board:{scope}:{kind}", STYLE_GRAY),
            ]
        ]
    )
