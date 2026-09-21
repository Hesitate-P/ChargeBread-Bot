"""WebSocket 长连接：握手、心跳、事件分发、退避重连。

协议细节全部来自探针实测，不是照文档抄的：
  · 网关地址来自 `GET /gateway`，实际返回 `wss://api.sgroup.qq.com/websocket`
    （文档写的是另一个域名）—— 属于不可信输入，先过 netguard 再连。
  · 握手：收到 op10 Hello（带 heartbeat_interval，毫秒）→ 发 op2 Identify，
    `token` 字段必须带 `QQBot ` 前缀。心跳是 op1，应答 op11。
  · op7 = 服务端要求重连；op9 = session 失效，要清掉 session 重新 Identify。
  · 4013/4014 是 intent 配置问题（非法 / 无权限），重连一万次也没用，
    所以直接抛 GatewayFatal 让运维看见，而不是无脑退避。
  · 事件对象把两个 id **分开命名**（message_id / envelope_id）：
    应答按钮用 d.id，被动回复用外层信封 id，探针阶段混用过一次，报 40034025。

连接器做成可注入的，测试直接喂帧序列。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from .netguard import assert_public_ws_url

log = logging.getLogger("chargebread.gateway")

SHARD_ID = 0
SHARD_TOTAL = 1
DEFAULT_HEARTBEAT_MS = 45000

# 收到帧之间的轮询粒度：让 stop() 在空闲连接上也能及时生效
STOP_POLL_SECONDS = 1.0

FATAL_CLOSE_CODES: dict[int, str] = {
    4013: "无效的 intent —— 请求里带了平台不认的 intent 值",
    4014: "intent 无权限 —— 该事件类型需要先向平台申请",
    4914: "机器人已下架，只允许连沙箱环境",
    4915: "机器人已被封禁",
}

# READY / RESUMED 是连接握手，不是业务事件
_HANDSHAKE_EVENTS = frozenset({"READY", "RESUMED"})

# 队列哨兵：连接结束时投进去，让消费者把剩余事件处理完再退出
_DRAIN = object()


class GatewayFatal(RuntimeError):
    """永久性错误，重连无意义，必须人工处理。"""


class _Reconnect(Exception):
    """内部信号：服务端要求重连（op7）。"""


@dataclass(frozen=True)
class GatewayEvent:
    """一个业务事件。

    `message_id` 与 `envelope_id` 刻意分开命名 —— 它们是两个不同的东西：
      · message_id = `d.id`，按钮应答用它（PUT /interactions/{message_id}）
      · envelope_id = 外层信封 id（形如 `INTERACTION_CREATE:uuid`），
        被动回复的 event_id 用它
    """

    name: str
    message_id: str
    envelope_id: str
    data: dict[str, Any]
    seq: int | None
    raw: dict[str, Any]


class WsConnection(Protocol):
    async def receive(self) -> str | None: ...
    async def send(self, payload: dict[str, Any]) -> None: ...
    async def close(self) -> None: ...


class WsConnector(Protocol):
    async def connect(self, url: str) -> WsConnection: ...


# ---- 默认连接器（唯一碰 aiohttp 的地方，测试可替换）--------------------------
class _AiohttpWs:
    def __init__(self, session: Any, ws: Any) -> None:
        self._session = session
        self._ws = ws

    @property
    def close_code(self) -> int | None:
        return self._ws.close_code

    async def receive(self) -> str | None:
        import aiohttp

        try:
            message = await self._ws.receive()
        except Exception as exc:  # noqa: BLE001 - 网络层任何异常都当断开处理
            log.warning("WS 接收失败，按断开处理: %s: %s", type(exc).__name__, exc)
            return None
        if message.type is aiohttp.WSMsgType.TEXT:
            return str(message.data)
        if message.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        ):
            return None
        return ""  # 二进制/心跳帧：交给上层忽略

    async def send(self, payload: dict[str, Any]) -> None:
        await self._ws.send_json(payload)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close()
        with contextlib.suppress(Exception):
            await self._session.close()


class AiohttpConnector:
    """默认连接器：握手给足时间（到腾讯的链路会抖），总超时不设。"""

    async def connect(self, url: str) -> WsConnection:
        import aiohttp

        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=60, sock_connect=60)
        )
        try:
            ws = await session.ws_connect(url, heartbeat=None)
        except Exception:
            with contextlib.suppress(Exception):
                await session.close()
            raise
        return _AiohttpWs(session, ws)


class Gateway:
    """长连接。`stop()` 是粘性的，实例不可重入 —— 重启请新建一个。"""

    def __init__(
        self,
        api: Any,
        *,
        intents: int,
        connector: WsConnector | None = None,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        user_agent: str = "chargebread",
    ) -> None:
        self.api = api
        self.intents = intents
        self.connector: WsConnector = connector or AiohttpConnector()
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.user_agent = user_agent

        self._stop = asyncio.Event()
        self._session_id: str | None = None
        self._seq: int | None = None

    # ---- 生命周期 ---------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    async def run(
        self,
        handler: Callable[[GatewayEvent], Awaitable[None]],
        *,
        max_connections: int | None = None,
    ) -> None:
        """连到网关，把业务事件交给 handler。断线自动重连，直到 stop()。

        max_connections 只给测试用：连够这么多次就返回，避免死循环。
        """
        connections = 0
        failures = 0

        while not self._stop.is_set():
            if max_connections is not None and connections >= max_connections:
                return

            try:
                token = await self.api.ensure_token()
                url = await self.api.gateway_url()
                assert_public_ws_url(url, require_wss=True)
            except GatewayFatal:
                raise
            except Exception as exc:  # noqa: BLE001 - 取 token / 网关失败要能重试
                failures += 1
                log.warning("准备连接失败（第 %d 次）: %s: %s", failures, type(exc).__name__, exc)
                await self._sleep_backoff(failures)
                continue

            try:
                ws = await self.connector.connect(url)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.warning("连接失败（第 %d 次）: %s: %s", failures, type(exc).__name__, exc)
                await self._sleep_backoff(failures)
                continue

            connections += 1
            failures = 0
            log.info("已连接网关（第 %d 次），intents=%s", connections, self.intents)
            try:
                await self._pump(ws, token, handler)
            except _Reconnect:
                log.info("服务端要求重连")
            finally:
                await ws.close()

    async def _sleep_backoff(self, failures: int) -> None:
        delay = min(self.backoff_max, self.backoff_base * (2 ** max(0, failures - 1)))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)

    # ---- 单次连接 ---------------------------------------------------------
    async def _pump(
        self,
        ws: WsConnection,
        token: str,
        handler: Callable[[GatewayEvent], Awaitable[None]],
    ) -> None:
        # Hello 之前也要能及时停：不设这个的话，一个连上却不说话的服务端
        # 会让 stop() 一直等到 docker 的 SIGKILL。
        #
        # 用「task + wait」而不是 wait_for(receive())：wait_for 超时会**取消**
        # 内层 receive，对真实 WS 连接具破坏性（假实现里也会卡住不返回）。
        # 包装成 task 再等，超时只是我们不等了，接收仍在后台挂着。
        first_task: asyncio.Task[str | None] = asyncio.create_task(ws.receive())
        first: str | None = None
        arrived = False
        while not arrived and not self._stop.is_set():
            done, _ = await asyncio.wait({first_task}, timeout=STOP_POLL_SECONDS)
            if done:
                first = first_task.result()
                arrived = True
        if not arrived:
            first_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await first_task
            return
        if first is None:
            # 连上就断了（不是配置问题）：按普通断开处理，交给外层退避重连
            self._raise_if_fatal(ws)
            return

        hello = self._parse(first)
        if hello is None or hello.get("op") != 10:
            raise GatewayFatal(f"首帧不是 Hello(op10): {str(first)[:120]!r}")

        interval_ms = (hello.get("d") or {}).get("heartbeat_interval") or DEFAULT_HEARTBEAT_MS
        try:
            interval = float(interval_ms) / 1000.0
        except (TypeError, ValueError):
            interval = DEFAULT_HEARTBEAT_MS / 1000.0
        if interval <= 0:
            # 只兜住非正值，**不要**加任意下限 —— 那会让心跳比服务端要求的慢，
            # 实测踩过：max(1.0, 0.02) 把 20ms 抬成 1s，等于不服从服务端指令。
            interval = DEFAULT_HEARTBEAT_MS / 1000.0

        await self._handshake(ws, token)

        heartbeat = asyncio.create_task(self._heartbeat(ws, interval))
        # 收帧与处理解耦：一次慢发送不能把接收循环堵住。
        # 用**单个**消费者保证事件仍按到达顺序处理（顺序对签到名次很重要），
        # 而收帧、心跳不受处理耗时影响。
        queue: asyncio.Queue = asyncio.Queue()
        consumer = asyncio.create_task(self._consume(queue, handler))
        try:
            pending: asyncio.Task[str | None] | None = None
            while not self._stop.is_set():
                if pending is None:
                    pending = asyncio.create_task(ws.receive())
                done, _ = await asyncio.wait({pending}, timeout=STOP_POLL_SECONDS)
                if not done:
                    continue
                frame = pending.result()
                pending = None
                if frame is None:
                    self._raise_if_fatal(ws)
                    return
                payload = self._parse(frame)
                if payload is None:
                    continue
                await self._handle_frame(ws, payload, queue)
        finally:
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            # 放哨兵让消费者把已入队的事件处理完，保证调用方看到的是"全部处理过"
            queue.put_nowait(_DRAIN)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await consumer

    async def _consume(
        self,
        queue: asyncio.Queue,
        handler: Callable[[GatewayEvent], Awaitable[None]],
    ) -> None:
        while True:
            item = await queue.get()
            if item is _DRAIN:
                return
            try:
                await handler(item)
            except Exception:  # noqa: BLE001 - 一个事件处理炸了不能拖垮整个连接
                log.exception("处理事件 %s 失败（已忽略，连接继续）", item.name)

    async def _handshake(self, ws: WsConnection, token: str) -> None:
        if self._session_id and self._seq is not None:
            # 有 session 就 Resume，让服务端补发断线期间漏掉的事件
            await ws.send(
                {
                    "op": 6,
                    "d": {
                        "token": f"QQBot {token}",
                        "session_id": self._session_id,
                        "seq": self._seq,
                    },
                }
            )
        else:
            await self._identify(ws, token)

    async def _identify(self, ws: WsConnection, token: str) -> None:
        await ws.send(
            {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": self.intents,
                    "shard": [SHARD_ID, SHARD_TOTAL],
                    "properties": {
                        "$os": "linux",
                        "$browser": self.user_agent,
                        "$device": self.user_agent,
                    },
                },
            }
        )

    async def _heartbeat(self, ws: WsConnection, interval: float) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            if self._stop.is_set():
                return
            try:
                await ws.send({"op": 1, "d": self._seq})
            except Exception:  # noqa: BLE001 - 心跳发不出去就让接收循环去发现断线
                return

    async def _handle_frame(
        self,
        ws: WsConnection,
        payload: dict[str, Any],
        queue: asyncio.Queue,
    ) -> None:
        op = payload.get("op")
        if payload.get("s") is not None:
            self._seq = payload["s"]

        if op in (10, 11):
            return
        if op == 7:
            raise _Reconnect()
        if op == 9:
            # session 失效：清掉，重新 Identify
            self._session_id = None
            self._seq = None
            token = await self.api.ensure_token()
            await self._identify(ws, token)
            return
        if op != 0:
            log.debug("忽略未知 op=%s", op)
            return

        name = payload.get("t") or ""
        data = payload.get("d")
        if not isinstance(data, dict):
            data = {}
        if name in _HANDSHAKE_EVENTS:
            if name == "READY":
                self._session_id = data.get("session_id")
                user = data.get("user") or {}
                log.info(
                    "READY session_id=%s 机器人=%s",
                    self._session_id,
                    user.get("username") or user.get("id") or "?",
                )
            elif name == "RESUMED":
                log.info("已恢复会话 session_id=%s", self._session_id)
            return

        event = GatewayEvent(
            name=name,
            message_id=str(data.get("id") or ""),
            envelope_id=str(payload.get("id") or ""),
            data=data,
            seq=payload.get("s"),
            raw=payload,
        )
        # 只入队，不在这里 await handler —— 处理耗时不能影响收帧与心跳
        queue.put_nowait(event)

    @staticmethod
    def _parse(frame: str) -> dict[str, Any] | None:
        if not frame:
            return None
        try:
            payload = json.loads(frame)
        except (json.JSONDecodeError, TypeError):
            log.warning("收到无法解析的帧，已忽略: %s", str(frame)[:120])
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _raise_if_fatal(ws: WsConnection) -> None:
        code = getattr(ws, "close_code", None)
        if code in FATAL_CLOSE_CODES:
            raise GatewayFatal(f"连接被关闭 close_code={code}：{FATAL_CLOSE_CODES[code]}")
