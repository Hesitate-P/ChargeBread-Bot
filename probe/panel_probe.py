#!/usr/bin/env python3
"""指令面板的 30013「超出数量限制」二分诊断。

背景：按文档，`panel.items` 上限是 20，我们提交 8 项却报 30013。
文档还说错误响应会带 `limit` 字段，实测**没有**。所以要自己二分。

安全约束：
  · 只做 REST 调用，**不建网关连接** —— 生产实例正在跑，同分片双连接会抢事件。
  · 默认用 `target_type=specific` 只投放到指定群，且**每建成一个立刻删除**，
    不在生产群里留东西。

用法：
    python3 probe/panel_probe.py --group <群openid>          # 二分
    python3 probe/panel_probe.py                             # 只列出各场景面板数
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.api import Api  # noqa: E402
from chargebread.config import Config  # noqa: E402

PROBE_REMARK = "chargebread:probe"


async def create(api: Api, cfg: Config, items: list[dict], group: str | None) -> tuple[bool, str]:
    payload: dict = {
        "scope": "group",
        "target_type": "specific" if group else "all",
        "panel": {"items": items, "remark": PROBE_REMARK},
    }
    if group:
        payload["group_openids"] = [group]
    token = await api.ensure_token()
    resp = await api.transport.request(
        "POST",
        f"{cfg.api_base}/v2/panels",
        json_body=payload,
        headers={"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
    )
    body = resp.body
    if "panel_id" in body:
        return True, str(body["panel_id"])
    return False, "code=%s %s" % (body.get("code"), body.get("message"))


async def probe(api: Api, cfg: Config, label: str, items: list[dict], group: str | None) -> bool:
    ok, detail = await create(api, cfg, items, group)
    if ok:
        print(f"  ✅ {label:46s} -> {detail}")
        await api.delete_panel(detail)
        print(f"     {'':46s}    （已立刻删除）")
    else:
        print(f"  ❌ {label:46s} -> {detail}")
    return ok


async def boundary(api: Api, cfg: Config, group: str | None) -> None:
    """分离两个假设：name 的汉字数上限，还是不允许空格。

    建面板限频 10 QPM，所以每次之间停 7 秒 —— 上一轮就是因为连发撞了 100017。
    """
    cases: list[tuple[str, str]] = [
        ("帮助", "显示菜单"),                      # 2 汉字
        ("面包排行榜", "本群面包总数榜"),                # 5 汉字，无空格（已验通过）
        ("面包排行榜全部", "全服面包总数榜"),              # 7 汉字，**无空格** ← 决定性
        ("面包排行榜 全部", "全服面包总数榜"),             # 5+空格+2 ← 已知失败
        ("签到排行榜全部", "全服今日签到顺序"),             # 7 汉字，无空格
        ("面包排行榜本群榜", "本群面包总数榜"),             # 8 汉字，无空格
    ]
    for index, (name, desc) in enumerate(cases):
        if index:
            print("     （等待 7s 避开 10 QPM 限频）")
            await asyncio.sleep(7)
        await probe(
            api,
            cfg,
            f"name={name!r} {len(name)}字 desc={desc!r} {len(desc)}字",
            [{"name": name, "desc": desc, "type": "command"}],
            group,
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description="指令面板 30013 二分诊断")
    parser.add_argument("--group", default="", help="用于 specific 投放的群 openid；留空则用 all")
    parser.add_argument(
        "--boundary",
        action="store_true",
        help="只跑边界用例（分离汉字数上限与空格限制，自带限频节流）",
    )
    args = parser.parse_args()
    group = args.group or None

    cfg = Config.from_env(Path(__file__).resolve().parent.parent / ".env")
    api = Api(cfg)
    try:
        if args.boundary:
            print(f"投放范围: {'指定群 ' + group if group else 'all（所有群）'}")
            print("\n=== 边界用例 ===")
            await boundary(api, cfg, group)
            return 0

        print("=== 各场景现有面板数 ===")
        for scope in ("c2c", "group", "channel", "dm"):
            try:
                records = await api.list_panels(scope)
                print(f"  {scope:8s}: {len(records)} 个")
            except Exception as exc:  # noqa: BLE001
                print(f"  {scope:8s}: 查询失败 {type(exc).__name__}: {exc}")
        print(f"\n投放范围: {'指定群 ' + group if group else 'all（所有群）'}")

        short = [{"name": f"N{i}", "desc": f"d{i}", "type": "command"} for i in range(20)]

        print("\n=== 探针 A：数量是否受限（极短内容，逐项增长）===")
        for n in (1, 8, 20):
            if not await probe(api, cfg, f"{n} 项短内容", short[:n], group):
                break

        print("\n=== 探针 B：长度边界（带限频节流）===")
        await boundary(api, cfg, group)
    finally:
        await api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
