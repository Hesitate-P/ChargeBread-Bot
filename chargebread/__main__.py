"""入口：python -m chargebread

把配置、存储、HTTP 客户端、机器人逻辑、长连接串起来，并处理优雅退出。

也兼一次性管理命令（装/卸指令面板）—— 面板是**对外可见**的副作用，
不该在每次启动时悄悄发生，所以做成显式调用：

    python -m chargebread --install-panel
    python -m chargebread --uninstall-panel

关于信号：容器收到 docker stop 会发 SIGTERM。这里把它转成 gateway.stop()，
让长连接在 1 秒轮询粒度内结束（网关的接收循环会定期检查停止标志），
而不是干等接收阻塞到被 SIGKILL。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from datetime import datetime

from . import __version__, panel
from .api import Api
from .bot import Bot
from .config import Config, ConfigError
from .db import Database
from .gateway import Gateway, GatewayFatal

# 实测确认：群聊相关 intent 无需申请，全部可连。
# 1<<24 群成员变动 | 1<<25 群@消息与单聊 | 1<<26 按钮回调
INTENTS = (1 << 24) | (1 << 25) | (1 << 26)

log = logging.getLogger("chargebread")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


async def amain() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_level)
    log.info("充能面包 bot v%s 启动", __version__)
    log.info("时区 %s，每日 %d 点重置，数据库 %s", config.tz.key, config.reset_hour, config.db_path)
    if config.allowed_groups:
        log.info("只服务 %d 个指定群", len(config.allowed_groups))

    db = Database(config.db_path)
    pruned = db.prune_seen_events(datetime.now(config.tz).isoformat())
    if pruned:
        log.info("已清理 %d 条过期的幂等记录", pruned)
    api = Api(config)
    bot = Bot(config, db, api)
    gateway = Gateway(api, intents=INTENTS)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, gateway.stop)

    keeper = asyncio.create_task(api.run_token_keeper())
    try:
        await gateway.run(bot.handle)
    except GatewayFatal as exc:
        log.error("致命错误，无法继续：%s", exc)
        log.error("这通常需要在 q.qq.com 检查机器人状态与事件订阅配置")
        return 1
    finally:
        api.stop_token_keeper()
        keeper.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await keeper
        await api.close()
        db.close()
    log.info("已退出")
    return 0


async def panel_command(args: argparse.Namespace) -> int:
    """一次性装/卸指令面板，然后退出（不启动机器人）。"""
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level)

    api = Api(config)
    try:
        if args.install_panel:
            print(await panel.install(api, config))
        else:
            removed = await panel.uninstall(api)
            print(f"已删除 {removed} 个充能面包指令面板" if removed else "没有找到需要删除的面板")
    except Exception as exc:  # noqa: BLE001 - 管理命令要把失败原因说清楚
        print(f"操作失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await api.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m chargebread",
        description="充能面包 —— QQ 群聊每日签到机器人",
    )
    p.add_argument(
        "--install-panel",
        action="store_true",
        help="安装/更新指令面板后退出（不启动机器人）",
    )
    p.add_argument(
        "--uninstall-panel",
        action="store_true",
        help="删除本机器人的指令面板后退出",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.install_panel or args.uninstall_panel:
            return asyncio.run(panel_command(args))
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
