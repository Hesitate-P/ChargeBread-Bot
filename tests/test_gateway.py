"""gateway.py 的期望契约（TDD：先写测试，再实现）。

WS 连接做成可注入的，测试直接喂帧序列，断言协议行为。

几个刻意的设计：
  · 事件对象**同时携带两个 id 并分开命名**（`message_id` / `envelope_id`）。
    探针阶段真的把这两个搞混过 —— 应答要用 `d.id`，被动回复的 event_id 要用
    外层信封 id，混用报 40034025。名字分开就没法再混。
  · 4013/4014 是**永久性**配置问题（intent 非法 / intent 无权限），
    重连一万次也没用，所以直接抛出致命错误而不是无脑退避重试。
  · 有 session 时优先用 op6 Resume（服务端会补发漏掉的事件），
    收到 op9 无效 session 再退回 op2 Identify。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.config import Config  # noqa: E402
from chargebread.gateway import Gateway, GatewayEvent, GatewayFatal  # noqa: E402

WS_URL = "wss://api.sgroup.qq.com/websocket"


def setUpModule() -> None:
    """静音 gateway 的 logger。

    生产里 handler 抛异常就该 log.exception 报出来，那是正确行为；
    但测试里会往 stderr 吐 traceback，把测试输出弄脏。所以这里只关日志，不改行为。
    """
    import logging

    logging.getLogger("chargebread.gateway").setLevel(logging.CRITICAL)


def make_config() -> Config:
    return Config(
        app_id="1905655911",
        app_secret="s",
        image_url="https://img.example/a.png",
        db_path=Path("/tmp/x.sqlite3"),
        tz=ZoneInfo("Asia/Shanghai"),
    )


class FakeWs:
    def __init__(
        self, frames: list[str | None], close_code: int | None = None, hold: float = 0.0
    ) -> None:
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False
        self.close_code = close_code
        # 帧读完后先停留这么久再断开，好让心跳有机会触发（否则毫秒内就结束了）
        self.hold = hold

    async def receive(self) -> str | None:
        if self.frames:
            return self.frames.pop(0)
        if self.hold > 0:
            await asyncio.sleep(self.hold)
            self.hold = 0.0
            return None
        return None  # 连接结束

    async def send(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, connections: list[FakeWs]) -> None:
        self.connections = list(connections)
        self.urls: list[str] = []

    async def connect(self, url: str) -> FakeWs:
        self.urls.append(url)
        if self.connections:
            return self.connections.pop(0)
        return FakeWs([])


class FakeApi:
    def __init__(self, token: str = "T") -> None:
        self._token = token
        self.calls = 0

    async def ensure_token(self) -> str:
        self.calls += 1
        return self._token

    async def gateway_url(self) -> str:
        return WS_URL


def hello(interval_ms: int = 30) -> str:
    return json.dumps({"op": 10, "d": {"heartbeat_interval": interval_ms}})


def dispatch(name: str, data: dict, seq: int = 1, envelope_id: str | None = None) -> str:
    return json.dumps(
        {
            "op": 0,
            "s": seq,
            "t": name,
            "id": envelope_id or f"{name}:envelope-{seq}",
            "d": data,
        }
    )


def ready(session_id: str = "sess-1") -> str:
    return dispatch("READY", {"session_id": session_id, "user": {"id": "1"}}, seq=1)


class IdentifyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.collected: list[GatewayEvent] = []

    async def _run_once(self, frames: list[str | None], **kwargs) -> FakeWs:
        ws = FakeWs(frames)
        connector = FakeConnector([ws])
        gw = Gateway(FakeApi(), intents=12345, connector=connector, backoff_base=0.01, **kwargs)
        await gw.run(self._collect, max_connections=1)
        return ws

    async def _collect(self, event: GatewayEvent) -> None:
        self.collected.append(event)

    async def test_identify_is_sent_after_hello(self) -> None:
        ws = await self._run_once([hello(), ready()])
        identify = [p for p in ws.sent if p.get("op") == 2]
        self.assertEqual(len(identify), 1)
        d = identify[0]["d"]
        self.assertEqual(d["token"], "QQBot T", "token 必须带 QQBot 前缀")
        self.assertEqual(d["intents"], 12345)
        self.assertEqual(d["shard"], [0, 1])

    async def test_hello_interval_drives_heartbeat(self) -> None:
        """心跳间隔取自 Hello 帧，不是写死的 45 秒。"""
        ws = FakeWs([hello(interval_ms=20), ready()], hold=0.12)
        connector = FakeConnector([ws])
        gw = Gateway(FakeApi(), intents=1, connector=connector, backoff_base=0.01)
        await gw.run(self._collect, max_connections=1)
        beats = [p for p in ws.sent if p.get("op") == 1]
        self.assertGreaterEqual(len(beats), 2, "20ms 间隔、停留 120ms，应发出多次 op1")

    async def test_heartbeat_carries_last_seq(self) -> None:
        ws = FakeWs(
            [hello(interval_ms=20), dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m1"}, seq=7)],
            hold=0.12,
        )
        connector = FakeConnector([ws])
        gw = Gateway(FakeApi(), intents=1, connector=connector, backoff_base=0.01)
        await gw.run(self._collect, max_connections=1)
        beats = [p for p in ws.sent if p.get("op") == 1]
        self.assertTrue(beats)
        self.assertEqual(beats[-1]["d"], 7, "心跳要带上最后收到的 seq")


class EventTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.collected: list[GatewayEvent] = []

    async def _collect(self, event: GatewayEvent) -> None:
        self.collected.append(event)

    async def _run(self, frames: list[str | None]) -> None:
        gw = Gateway(
            FakeApi(), intents=1, connector=FakeConnector([FakeWs(frames)]), backoff_base=0.01
        )
        await gw.run(self._collect, max_connections=1)

    async def test_event_exposes_both_ids_separately(self) -> None:
        """应答用 message_id，被动回复用 envelope_id —— 名字必须分开，混了就报错。"""
        await self._run(
            [
                hello(),
                dispatch(
                    "GROUP_AT_MESSAGE_CREATE",
                    {"id": "ROBOT1.0_msgid", "content": " 充能面包 ", "group_openid": "G1"},
                    seq=2,
                    envelope_id="GROUP_AT_MESSAGE_CREATE:abc",
                ),
            ]
        )
        event = self.collected[-1]
        self.assertEqual(event.name, "GROUP_AT_MESSAGE_CREATE")
        self.assertEqual(event.message_id, "ROBOT1.0_msgid")
        self.assertEqual(event.envelope_id, "GROUP_AT_MESSAGE_CREATE:abc")
        self.assertEqual(event.data["content"], " 充能面包 ")

    async def test_interaction_event_also_carries_both_ids(self) -> None:
        await self._run(
            [
                hello(),
                dispatch(
                    "INTERACTION_CREATE",
                    {"id": "844c370e-uuid", "type": 11, "group_openid": "G1"},
                    seq=3,
                    envelope_id="INTERACTION_CREATE:844c370e-uuid",
                ),
            ]
        )
        event = self.collected[-1]
        self.assertEqual(event.message_id, "844c370e-uuid")
        self.assertEqual(event.envelope_id, "INTERACTION_CREATE:844c370e-uuid")

    async def test_ready_is_not_handed_to_handler(self) -> None:
        """READY 是连接握手，不是业务事件。"""
        await self._run([hello(), ready()])
        self.assertEqual([e.name for e in self.collected], [])

    async def test_events_arrive_in_order(self) -> None:
        await self._run(
            [
                hello(),
                dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m1"}, seq=2),
                dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m2"}, seq=3),
                dispatch("INTERACTION_CREATE", {"id": "i1"}, seq=4),
            ]
        )
        self.assertEqual(
            [e.message_id for e in self.collected], ["m1", "m2", "i1"]
        )

    async def test_unknown_op_does_not_crash(self) -> None:
        await self._run(
            [
                hello(),
                json.dumps({"op": 99, "d": {"weird": True}}),
                dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m1"}, seq=2),
            ]
        )
        self.assertEqual([e.message_id for e in self.collected], ["m1"])

    async def test_malformed_frame_does_not_crash(self) -> None:
        await self._run(
            [hello(), "{not json", dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m1"}, seq=2)]
        )
        self.assertEqual([e.message_id for e in self.collected], ["m1"])


class ReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.collected: list[GatewayEvent] = []

    async def _collect(self, event: GatewayEvent) -> None:
        self.collected.append(event)

    async def test_reconnects_after_connection_ends(self) -> None:
        first = FakeWs([hello(), dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m1"}, seq=2)])
        second = FakeWs([hello(), dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m2"}, seq=3)])
        connector = FakeConnector([first, second])
        gw = Gateway(FakeApi(), intents=1, connector=connector, backoff_base=0.01)
        await gw.run(self._collect, max_connections=2)
        self.assertEqual([e.message_id for e in self.collected], ["m1", "m2"])
        self.assertEqual(connector.urls, [WS_URL, WS_URL])

    async def test_resume_used_when_session_known(self) -> None:
        """有 session 时优先 Resume，让服务端补发断线期间漏掉的事件。"""
        first = FakeWs([hello(), ready("sess-42"), dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m1"}, seq=5)])
        second = FakeWs([hello(), json.dumps({"op": 0, "t": "RESUMED", "s": 6, "d": {}})])
        gw = Gateway(FakeApi(), intents=1, connector=FakeConnector([first, second]), backoff_base=0.01)
        await gw.run(self._collect, max_connections=2)
        resume = [p for p in second.sent if p.get("op") == 6]
        self.assertEqual(len(resume), 1, "第二次连接应该用 op6 Resume")
        self.assertEqual(resume[0]["d"]["session_id"], "sess-42")
        self.assertEqual(resume[0]["d"]["seq"], 5)

    async def test_invalid_session_falls_back_to_identify(self) -> None:
        first = FakeWs([hello(), ready("sess-9"), dispatch("X", {"id": "m1"}, seq=5)])
        second = FakeWs([hello(), json.dumps({"op": 9, "d": False}), json.dumps({"op": 10, "d": {"heartbeat_interval": 30}})])
        gw = Gateway(FakeApi(), intents=1, connector=FakeConnector([first, second]), backoff_base=0.01)
        await gw.run(self._collect, max_connections=2)
        identify = [p for p in second.sent if p.get("op") == 2]
        self.assertGreaterEqual(len(identify), 1, "session 失效后应退回 Identify")

    async def test_intent_permission_close_is_fatal(self) -> None:
        """4014 = intent 无权限，是配置问题，重连没意义。"""
        ws = FakeWs([hello()], close_code=4014)
        gw = Gateway(FakeApi(), intents=1, connector=FakeConnector([ws]), backoff_base=0.01)
        with self.assertRaises(GatewayFatal) as ctx:
            await gw.run(self._collect, max_connections=3)
        self.assertIn("4014", str(ctx.exception))

    async def test_invalid_intent_close_is_fatal(self) -> None:
        ws = FakeWs([hello()], close_code=4013)
        gw = Gateway(FakeApi(), intents=1, connector=FakeConnector([ws]), backoff_base=0.01)
        with self.assertRaises(GatewayFatal):
            await gw.run(self._collect, max_connections=3)

    async def test_bot_delisted_close_is_fatal(self) -> None:
        ws = FakeWs([hello()], close_code=4914)
        gw = Gateway(FakeApi(), intents=1, connector=FakeConnector([ws]), backoff_base=0.01)
        with self.assertRaises(GatewayFatal):
            await gw.run(self._collect, max_connections=3)

    async def test_ordinary_close_is_retried_not_fatal(self) -> None:
        """1006 之类的普通断开应该重连，不算致命。"""
        ws1 = FakeWs([hello()], close_code=1006)
        ws2 = FakeWs([hello(), dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m1"}, seq=2)])
        gw = Gateway(FakeApi(), intents=1, connector=FakeConnector([ws1, ws2]), backoff_base=0.01)
        await gw.run(self._collect, max_connections=2)
        self.assertEqual([e.message_id for e in self.collected], ["m1"])


class StopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.collected: list[GatewayEvent] = []

    async def _collect(self, event: GatewayEvent) -> None:
        self.collected.append(event)

    async def test_stop_ends_the_loop(self) -> None:
        ws = FakeWs([hello(), ready()])
        gw = Gateway(FakeApi(), intents=1, connector=FakeConnector([ws]), backoff_base=0.01)

        async def stopper(event: GatewayEvent) -> None:
            self.collected.append(event)

        gw.stop()
        await asyncio.wait_for(gw.run(stopper, max_connections=5), timeout=2)

    async def test_handler_exception_does_not_kill_the_loop(self) -> None:
        """一个事件处理炸了，不能让整个连接断掉。"""
        frames = [
            hello(),
            dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "bad"}, seq=2),
            dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "good"}, seq=3),
        ]
        gw = Gateway(FakeApi(), intents=1, connector=FakeConnector([FakeWs(frames)]), backoff_base=0.01)
        seen: list[str] = []

        async def handler(event: GatewayEvent) -> None:
            if event.message_id == "bad":
                raise RuntimeError("boom")
            seen.append(event.message_id)

        await gw.run(handler, max_connections=1)
        self.assertEqual(seen, ["good"])

    async def test_failed_token_fetch_is_retried_not_fatal(self) -> None:
        class FlakyApi(FakeApi):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            async def ensure_token(self) -> str:
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("token 服务抖动")
                return "T"

        api = FlakyApi()
        ws = FakeWs([hello(), dispatch("GROUP_AT_MESSAGE_CREATE", {"id": "m1"}, seq=2)])
        gw = Gateway(api, intents=1, connector=FakeConnector([ws]), backoff_base=0.01)
        seen: list[str] = []

        async def handler(event: GatewayEvent) -> None:
            seen.append(event.message_id)

        await gw.run(handler, max_connections=2)
        self.assertGreaterEqual(api.attempts, 2)
        self.assertEqual(seen, ["m1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
