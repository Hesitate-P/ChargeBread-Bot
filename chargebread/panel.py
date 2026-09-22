"""指令面板：把机器人的指令装进 QQ 客户端的指令面板。

平台约束（来自 POST /v2/panels 的字段表）：
  · `scope`：c2c / group / channel / dm；channel 与 dm **只能**全局配置
  · `target_type`：all / specific；specific 仅 c2c 与 group 可用
  · `group_openids` 每次最多 **20** 个，且仅 group + specific 时有效
  · `panel.items` 最多 20 个；`remark` 最多 255 字符且**不对用户展示**
  · `PanelItem.name` 最多 14 字符（约 7 个中文），`desc` 最多 30 字符（约 15 个中文）
  · `PanelItem.type` 只有 command / link

**关键语义**：`type=command` 点击后是「内容填入聊天输入框」，用户仍需自己发送；
只有 `type=link` 才跳浏览器。所以面板项填的是**裸命令词**，正好是我们解析器
认得的写法（带不带斜杠都认）。

`remark` 用作我们的身份标记 —— 重装时靠它认出自己那个面板，不去碰别人的。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .api import Api, ApiError
from .config import Config

log = logging.getLogger("chargebread.panel")

# 面板的身份标记（remark 不对用户展示，只给我们自己辨认）
PANEL_REMARK = "chargebread:commands"

# specific 模式单次最多 20 个目标
MAX_SPECIFIC_TARGETS = 20
# 一个面板最多 20 个元素
MAX_ITEMS = 20

# 平台的**字数口径**：一个中文汉字算 2 个单位。
# 文档写「name 最多 14 个字符，约 7 个中文汉字」——那个"约"就是这个意思，
# 我一开始读成了"14 个汉字"，结果 8 个汉字的面板项被平台拒掉，还报成
# `30013 超出数量限制`（错误信息完全指错方向）。
# 实测边界见 probe/panel_probe.py --boundary：7 汉字通过，8 汉字被拒。
NAME_MAX_UNITS = 14
DESC_MAX_UNITS = 30


def text_units(text: str) -> int:
    """按平台口径算长度：非 ASCII 字符算 2 个单位，其余算 1。"""
    return sum(2 if ord(ch) > 0x7F else 1 for ch in text)


def validate_items(items: list[dict]) -> None:
    """在提交前把超限的内容挡住。

    平台只在创建时报一个含义不清的 30013，不做本地校验的话，
    改一句文案要等到真机安装才发现。
    """
    if len(items) > MAX_ITEMS:
        raise ValueError(f"面板元素 {len(items)} 个，超过上限 {MAX_ITEMS} 个")
    for item in items:
        name, desc = item.get("name", ""), item.get("desc", "")
        if text_units(name) > NAME_MAX_UNITS:
            raise ValueError(
                f"面板元素名称 {name!r} 有 {text_units(name)} 个单位，"
                f"超过上限 {NAME_MAX_UNITS}（约 {NAME_MAX_UNITS // 2} 个汉字）"
            )
        if text_units(desc) > DESC_MAX_UNITS:
            raise ValueError(
                f"面板元素描述 {desc!r} 有 {text_units(desc)} 个单位，"
                f"超过上限 {DESC_MAX_UNITS}（约 {DESC_MAX_UNITS // 2} 个汉字）"
            )


@dataclass(frozen=True)
class PanelSpec:
    scope: str
    target_type: str
    group_openids: tuple[str, ...]
    items: tuple[dict, ...]


def panel_items() -> list[dict]:
    """面板内容。

    每一项的 name 都会**原样填进输入框**，所以必须是解析器认得的写法。

    刻意**不放**「面包排行榜 全部」这类带参数的项：name 上限是 7 个汉字
    （14 个单位），"面包排行榜 全部" 正好 8 个汉字会被平台拒；而且面板也
    表达不了参数，想要全服榜自己补一个「全部」更直接。
    """
    return [
        {"name": "充能面包", "desc": "今日签到，领取面包", "type": "command"},
        {"name": "我的面包", "desc": "个人详情与连签", "type": "command"},
        {"name": "面包排行榜", "desc": "本群面包总数榜", "type": "command"},
        {"name": "签到排行榜", "desc": "本群今日签到顺序", "type": "command"},
        {"name": "今日充能指数", "desc": "看看今天充了几格电", "type": "command"},
        {"name": "补签", "desc": "用补签卡补回昨天", "type": "command"},
        {"name": "帮助", "desc": "显示菜单与全部玩法", "type": "command"},
    ]


def build_spec(config: Config) -> PanelSpec:
    """按配置决定面板的生效范围。

    配了群白名单就只往那些群装（specific）；没配就是全场景（all）。
    """
    groups = tuple(sorted(config.allowed_groups))
    items = tuple(panel_items())
    validate_items(list(items))
    if not groups:
        return PanelSpec("group", "all", (), items)
    if len(groups) > MAX_SPECIFIC_TARGETS:
        raise ValueError(
            f"群白名单里有 {len(groups)} 个群，超过指令面板 specific 模式的单次上限 "
            f"{MAX_SPECIFIC_TARGETS} 个。请分批安装，或者留空 BREAD_ALLOWED_GROUPS "
            f"改用 all 让面板对所有群生效。"
        )
    return PanelSpec("group", "specific", groups, items)


def to_payload(spec: PanelSpec) -> dict:
    """转成 POST /v2/panels 的请求体。"""
    payload: dict = {
        "scope": spec.scope,
        "target_type": spec.target_type,
        "panel": {"items": [dict(item) for item in spec.items], "remark": PANEL_REMARK},
    }
    if spec.target_type == "specific":
        payload["group_openids"] = list(spec.group_openids)
    return payload


def _is_ours(entry: dict) -> bool:
    return (entry.get("panel") or {}).get("remark") == PANEL_REMARK


def _matches(entry: dict, spec: PanelSpec) -> bool:
    """现有面板和我们想装的是否一致（范围 + 每一项）。"""
    if entry.get("scope") != spec.scope or entry.get("target_type") != spec.target_type:
        return False
    existing = (entry.get("panel") or {}).get("items") or []
    if len(existing) != len(spec.items):
        return False
    for got, want in zip(existing, spec.items):
        if (
            got.get("name") != want["name"]
            or got.get("type") != want["type"]
            or got.get("desc") != want["desc"]
        ):
            return False
    return True


def _require_id(entry: dict) -> str:
    panel_id = entry.get("panel_id")
    if not panel_id:
        raise ApiError(None, f"面板列表里有我们的 remark，却缺少 panel_id: {entry!r}")
    return str(panel_id)


async def install(api: Api, config: Config) -> str:
    """安装/更新指令面板，返回一句给人看的结论。

    幂等：内容一致就不动（反复删建既白折腾，又容易撞上 40030009「操作进行中」）；
    不一致才删掉重建 —— 比逐项 diff 简单，也不会漏掉任何改动。
    """
    spec = build_spec(config)
    payload = to_payload(spec)

    mine = [entry for entry in await api.list_panels() if _is_ours(entry)]
    if mine and _matches(mine[0], spec):
        return f"指令面板已是最新（{_require_id(mine[0])}），无需改动，共 {len(spec.items)} 项"

    replaced = ""
    if mine:
        old_id = _require_id(mine[0])
        await api.delete_panel(old_id)
        replaced = f"，已替换旧面板 {old_id}"

    panel_id = await api.create_panel(payload)
    where = "所有群" if spec.target_type == "all" else f"{len(spec.group_openids)} 个指定群"
    return f"已创建指令面板 {panel_id}（生效范围：{where}，共 {len(spec.items)} 项）{replaced}"


async def uninstall(api: Api) -> int:
    """删除我们自己的指令面板，返回删了几个。别的不碰。"""
    removed = 0
    for entry in await api.list_panels():
        if not _is_ours(entry):
            continue
        panel_id = entry.get("panel_id")
        if not panel_id:
            continue
        await api.delete_panel(str(panel_id))
        removed += 1
    return removed


def start_autosync(api: Api, config: Config) -> "asyncio.Task[str] | None":
    """启动时后台把面板同步成代码里的样子；返回任务，关掉则返回 None。

    **不阻塞启动**：面板接口慢或报错都不该拖住机器人上线。所以做成后台任务，
    失败只记日志。

    之所以可以放心自动化：`install()` 是幂等的 —— 内容一致时只发一次 GET、
    什么都不改，只有内容真的变了才删旧建新。而那正是你希望它自动发生的时候。
    """
    if not config.panel_autosync:
        return None
    return asyncio.create_task(_autosync(api, config))


async def _autosync(api: Api, config: Config) -> str:
    try:
        summary = await install(api, config)
    except Exception:  # noqa: BLE001 - 面板问题绝不能挡住机器人运行
        log.warning("指令面板自动同步失败（不影响机器人运行）", exc_info=True)
        return "指令面板自动同步失败"
    log.info("指令面板：%s", summary)
    return summary
