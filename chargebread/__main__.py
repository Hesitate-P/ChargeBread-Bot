"""入口：python -m chargebread

把配置、存储、HTTP 客户端、机器人逻辑、长连接串起来，并处理优雅退出。

关于信号：容器收到 docker stop 会发 SIGTERM。这里把它转成 gateway.stop()，
让长连接在 1 秒轮询粒度内结束（网关的接收循环会定期检查停止标志），
而不是干等接收阻塞到被 SIGKILL。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from datetime import datetime

from . import __version__
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


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
