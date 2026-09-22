"""命令解析：把群消息文本和按钮回调都翻译成同一个 Command。

参数策略是用户拍定的"宽进"：同义词全认，打错字**静默回落默认**（本群）。
娱乐机器人不该因为一个错别字把人挡在门外。

两个真实世界的细节来自实测：
  · 群里 @机器人 之后 content 是 `' 充能面包 '` —— @ 前缀被平台剥掉了，
    但前后空格留着，所以必须先 strip，否则永远匹配不上。
  · 手机输入法容易打出全角斜杠 `／`，要认。

按钮回调（`board:<scope>:<kind>`）也走这里解析，这样按钮和命令不会长成两套逻辑。
回调是我们自己写进按钮的 data，不需要容错，格式不对就是 UNKNOWN。
"""

from __future__ import annotations

from dataclasses import dataclass

# 命令名
SIGNIN = "signin"
BOARD_BREAD = "board_bread"
BOARD_SIGNIN = "board_signin"
PROFILE = "profile"
MAKEUP = "makeup"
CHARGE = "charge"
RENAME = "rename"
HELP = "help"
UNKNOWN = "unknown"

# 榜单统计范围
SCOPE_GROUP = "group"
SCOPE_ALL = "all"

BOARD_COMMANDS = (BOARD_BREAD, BOARD_SIGNIN)

# 别名 → 命令名。键统一小写后比较，所以 ASCII 别名写小写即可。
COMMAND_ALIASES: dict[str, str] = {
    "充能面包": SIGNIN,
    "面包排行榜": BOARD_BREAD,
    "面包榜": BOARD_BREAD,
    "签到排行榜": BOARD_SIGNIN,
    "签到榜": BOARD_SIGNIN,
    "我的面包": PROFILE,
    "补签": MAKEUP,
    # 只做这一个命令名，不要别名：「今日充能」「充能指数」「指数」都不认。
    # 少一个入口就少一个歧义源（尤其"充能"同时是"充能面包"的前缀）。
    "今日充能指数": CHARGE,
    "改名": RENAME,
    "改昵称": RENAME,
    "setnick": RENAME,
    "帮助": HELP,
    "help": HELP,
    "菜单": HELP,
}

# 别名 → 统计范围
SCOPE_ALIASES: dict[str, str] = {
    "本群": SCOPE_GROUP,
    "群内": SCOPE_GROUP,
    "本群榜": SCOPE_GROUP,
    "group": SCOPE_GROUP,
    "全部": SCOPE_ALL,
    "全服": SCOPE_ALL,
    "全部群": SCOPE_ALL,
    "所有": SCOPE_ALL,
    "all": SCOPE_ALL,
}

CALLBACK_PREFIX = "board"
CALLBACK_KINDS = {"bread": BOARD_BREAD, "signin": BOARD_SIGNIN}


@dataclass(frozen=True)
class Command:
    name: str
    scope: str | None
    raw: str
    # 命令后面的自由文本（改名用）。榜单命令用 scope，不用这个。
    arg: str = ""


def _normalize(text: str) -> str:
    """去掉首尾空白，并把全角斜杠归一成半角。"""
    return (text or "").replace("／", "/").strip()


def _strip_slash(token: str) -> str:
    return token[1:] if token.startswith("/") else token


def _match_command(head: str) -> tuple[str, str]:
    """返回 (命令名, 粘连在命令后面的参数)。找不到返回 (UNKNOWN, "")。"""
    low = head.lower()
    if low in COMMAND_ALIASES:
        return COMMAND_ALIASES[low], ""
    # 容错：命令和参数之间漏了空格，例如「面包排行榜全部」
    for alias in sorted(COMMAND_ALIASES, key=len, reverse=True):
        if low.startswith(alias):
            return COMMAND_ALIASES[alias], head[len(alias):]
    return UNKNOWN, ""


def _match_scope(token: str) -> str:
    """认得的词按词表走，认不得的一律回落默认（本群）。"""
    return SCOPE_ALIASES.get((token or "").strip().lower(), SCOPE_GROUP)


def parse(text: str) -> Command:
    """解析一条群消息文本。空内容（空 @）等于帮助。"""
    raw = _normalize(text)
    if not raw:
        return Command(HELP, None, raw)

    parts = raw.split(maxsplit=1)
    name, glued_arg = _match_command(_strip_slash(parts[0]))
    if name == UNKNOWN:
        return Command(UNKNOWN, None, raw)
    rest = parts[1].strip() if len(parts) > 1 else ""

    scope = None
    if name in BOARD_COMMANDS:
        first = glued_arg or (rest.split()[0] if rest else "")
        scope = _match_scope(first)
    arg = f"{glued_arg} {rest}".strip() if glued_arg else rest
    return Command(name, scope, raw, arg)


def parse_callback(data: str) -> Command:
    """解析按钮回调带上来的 data，格式固定为 board:<scope>:<kind>。"""
    raw = (data or "").strip()
    parts = raw.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return Command(UNKNOWN, None, raw)
    scope, kind = parts[1], parts[2]
    if scope not in (SCOPE_GROUP, SCOPE_ALL):
        return Command(UNKNOWN, None, raw)
    name = CALLBACK_KINDS.get(kind)
    if name is None:
        return Command(UNKNOWN, None, raw)
    return Command(name, scope, raw)
