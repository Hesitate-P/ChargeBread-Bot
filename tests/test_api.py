"""api.py 的期望契约（TDD：先写测试，再实现）。

HTTP 传输做成可注入的，所以这些测试断言的是**行为**（什么时候重试、什么时候
刷新 token、请求体长什么样），而不是 aiohttp 的调用细节。

有几条约束是探针阶段踩出来的，不是猜的：
  · token 只剩 97 秒时启动，按钮回调到达时已过期，应答因为现取 token 花了
    10491 ms，超过平台 3 秒上限 —— 所以 token 必须后台预热，关键路径不许等刷新。
  · 应答 `PUT /interactions/{id}` 用 `d.id`；被动回复的 `event_id` 用**外层信封 id**
    （形如 `INTERACTION_CREATE:uuid`）。混用报 40034025。
  · 业务失败走 HTTP 200，必须看 body 里的 code；网络失败才重试。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.api import Api, ApiError, HttpResponse, TransportError  # noqa: E402
from chargebread.config import Config  # noqa: E402

BASE = "https://api.bot.qq.com"


def make_config() -> Config:
    return Config(
        app_id="1905655911",
        app_secret="fake-secret-for-tests",
        image_url="https://img.example/a.png",
        db_path=Path("/tmp/chargebread-test.sqlite3"),
        tz=ZoneInfo("Asia/Shanghai"),
        api_base=BASE,
    )


class FakeTransport:
    """按脚本返回响应，并记录每一次调用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.script: list[object] = []

    def push(self, response: object) -> None:
        self.script.append(response)

    async def request(self, method, url, *, json_body, headers, timeout=None) -> HttpResponse:
        self.calls.append(
            {"method": method, "url": url, "json": json_body, "headers": dict(headers)}
        )
        if not self.script:
            return HttpResponse(200, {"ok": True})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, HttpResponse)
        return item

    @property
    def paths(self) -> list[str]:
        return [c["url"].replace(BASE, "") for c in self.calls]


def token_response(token: str = "TOK", expires_in: int = 7200) -> HttpResponse:
    return HttpResponse(200, {"access_token": token, "expires_in": expires_in})


class TokenTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.api = Api(make_config(), transport=self.transport)

    async def test_fetches_token_from_documented_endpoint(self) -> None:
        self.transport.push(token_response())
        await self.api.ensure_token()
        call = self.transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{BASE}/app/getAppAccessToken")
        self.assertEqual(call["json"]["appId"], "1905655911")
        self.assertEqual(call["json"]["clientSecret"], "fake-secret-for-tests")

    async def test_token_is_cached_between_calls(self) -> None:
        self.transport.push(token_response())
        await self.api.ensure_token()
        await self.api.ensure_token()
        self.assertEqual(self.transport.paths.count("/app/getAppAccessToken"), 1)

    async def test_token_refreshed_when_near_expiry(self) -> None:
        """剩 60 秒就该续，不能让关键路径撞上过期。"""
        self.transport.push(token_response("OLD", expires_in=30))
        self.transport.push(token_response("NEW", expires_in=7200))
        await self.api.ensure_token()
        await self.api.ensure_token()
        self.assertEqual(self.transport.paths.count("/app/getAppAccessToken"), 2)

    async def test_auth_header_uses_qqbot_scheme(self) -> None:
        self.transport.push(token_response("ABC"))
        await self.api.get("/users/@me")
        auth = [c["headers"].get("Authorization") for c in self.transport.calls if "Authorization" in c["headers"]]
        self.assertEqual(auth, ["QQBot ABC"])

    async def test_business_error_raises_api_error_with_code(self) -> None:
        """业务失败是 HTTP 200 + body 里的 code，必须原样暴露出来。"""
        self.transport.push(HttpResponse(200, {"code": 100016, "message": "invalid appid or secret"}))
        with self.assertRaises(ApiError) as ctx:
            await self.api.ensure_token()
        self.assertEqual(ctx.exception.code, 100016)
        self.assertIn("invalid appid", str(ctx.exception))

    async def test_business_error_is_not_retried(self) -> None:
        """凭据错了重试多少次都一样，不该浪费请求。"""
        self.transport.push(HttpResponse(200, {"code": 11253, "message": "应用无接口访问权限"}))
        with self.assertRaises(ApiError):
            await self.api.get("/v2/groups/G/members/M")
        self.assertEqual(len(self.transport.calls), 1, "业务错误只该请求一次")

    async def test_network_error_is_retried_then_succeeds(self) -> None:
        self.transport.push(token_response())
        self.transport.push(TransportError("boom"))
        self.transport.push(HttpResponse(200, {"id": "msg1"}))
        out = await self.api.get("/users/@me")
        self.assertEqual(out["id"], "msg1")
        self.assertEqual(self.transport.paths.count("/users/@me"), 2)

    async def test_network_error_gives_up_after_retries(self) -> None:
        self.transport.push(token_response())
        for _ in range(5):
            self.transport.push(TransportError("still down"))
        with self.assertRaises(TransportError):
            await self.api.get("/users/@me")


class InteractionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.api = Api(make_config(), transport=self.transport)
        self.transport.push(token_response())

    async def test_ack_targets_interaction_endpoint_with_code_zero(self) -> None:
        """平台要求 3 秒内应答，body 是 {"code": 0}。"""
        await self.api.ack_interaction("844c370e-2f8c-4c11-9f00-abcdef123456")
        call = self.transport.calls[-1]
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(call["url"], f"{BASE}/interactions/844c370e-2f8c-4c11-9f00-abcdef123456")
        self.assertEqual(call["json"], {"code": 0})

    async def test_ack_failure_is_raised_but_not_fatal_by_default(self) -> None:
        """应答失败要能上报，调用方决定是否吞掉 —— 但默认要抛，免得静默转圈。"""
        self.transport.push(HttpResponse(200, {"code": 630003, "message": "AppID与interaction不匹配"}))
        with self.assertRaises(ApiError) as ctx:
            await self.api.ack_interaction("abc")
        self.assertEqual(ctx.exception.code, 630003)

    async def test_ack_has_no_retry_by_default(self) -> None:
        """3 秒窗口里重试没有意义，还可能撞上"同一 id 只能应答一次"。"""
        self.transport.push(TransportError("slow"))
        with self.assertRaises(TransportError):
            await self.api.ack_interaction("abc")
        ack_calls = [c for c in self.transport.calls if "/interactions/" in c["url"]]
        self.assertEqual(len(ack_calls), 1)


class SendMessageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.api = Api(make_config(), transport=self.transport)
        self.transport.push(token_response())

    async def test_markdown_with_buttons_and_msg_anchor(self) -> None:
        await self.api.send_markdown(
            "G1",
            "# 标题\n正文",
            keyboard={"content": {"rows": []}},
            msg_id="ROBOT1.0_xxx",
            msg_seq=2,
        )
        call = self.transport.calls[-1]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{BASE}/v2/groups/G1/messages")
        self.assertEqual(call["json"]["msg_type"], 2)
        self.assertEqual(call["json"]["markdown"]["content"], "# 标题\n正文")
        self.assertEqual(call["json"]["msg_id"], "ROBOT1.0_xxx")
        self.assertEqual(call["json"]["msg_seq"], 2)

    async def test_event_id_reply_omits_msg_id(self) -> None:
        """按钮回调的回复走 event_id，此时不能带 msg_id。"""
        await self.api.send_markdown(
            "G1", "# hi", event_id="INTERACTION_CREATE:uuid-here", msg_seq=1
        )
        body = self.transport.calls[-1]["json"]
        self.assertEqual(body["event_id"], "INTERACTION_CREATE:uuid-here")
        self.assertNotIn("msg_id", body)

    async def test_msg_id_reply_omits_event_id(self) -> None:
        await self.api.send_markdown("G1", "# hi", msg_id="M1", msg_seq=1)
        body = self.transport.calls[-1]["json"]
        self.assertNotIn("event_id", body)

    async def test_send_message_error_surfaces_platform_code(self) -> None:
        self.transport.push(HttpResponse(200, {"code": 40034029, "message": "内联键盘行/列超限"}))
        with self.assertRaises(ApiError) as ctx:
            await self.api.send_markdown("G1", "# hi", msg_id="M", msg_seq=1)
        self.assertEqual(ctx.exception.code, 40034029)

    async def test_dedup_code_is_treated_as_success_for_sends(self) -> None:
        """40054005「消息被去重」说明消息**此前已经送达**。

        第一次请求实际到达服务端但响应丢了 → 重试拿回 40054005。若把它当错误，
        上层会撤销事件标记、等平台重投、再用**新的 msg_seq** 发一遍，
        用户就会看到两条一样的回复。
        """
        self.transport.push(HttpResponse(200, {"code": 40054005, "message": "消息被去重"}))
        out = await self.api.send_markdown("G1", "# hi", msg_id="M", msg_seq=1)
        self.assertEqual(out["code"], 40054005)

    async def test_dedup_code_is_still_an_error_for_non_send_calls(self) -> None:
        """只有发消息才把去重码当成功 —— 其它接口拿到它仍是异常。"""
        self.transport.push(HttpResponse(200, {"code": 40054005, "message": "消息被去重"}))
        with self.assertRaises(ApiError):
            await self.api.get("/v2/whatever")

    async def test_dedup_after_transport_retry_does_not_raise(self) -> None:
        """连起来看：发送超时 → 重试 → 收到去重码 → 整体算成功。

        这正是 P1 场景：不能因为一次网络抖动就让用户收到两条回复。
        """
        self.transport.push(TransportError("响应丢了"))
        self.transport.push(HttpResponse(200, {"code": 40054005, "message": "消息被去重"}))
        out = await self.api.send_markdown("G1", "# hi", msg_id="M", msg_seq=1)
        self.assertEqual(out["code"], 40054005)
        self.assertEqual(
            len([p for p in self.transport.paths if p.endswith("/messages")]), 2
        )


class MsgSeqTest(unittest.IsolatedAsyncioTestCase):
    """同一条 msg_id 的多次回复必须用递增且不重复的 msg_seq，
    重复会报 40054005 消息被去重。"""

    async def asyncSetUp(self) -> None:
        self.api = Api(make_config(), transport=FakeTransport())

    async def test_seq_starts_at_one_and_increments(self) -> None:
        self.assertEqual(self.api.next_seq("M1"), 1)
        self.assertEqual(self.api.next_seq("M1"), 2)
        self.assertEqual(self.api.next_seq("M1"), 3)

    async def test_seq_is_tracked_per_message(self) -> None:
        self.assertEqual(self.api.next_seq("M1"), 1)
        self.assertEqual(self.api.next_seq("M2"), 1)
        self.assertEqual(self.api.next_seq("M1"), 2)

    async def test_seq_zero_is_never_returned(self) -> None:
        for _ in range(6):
            self.assertGreaterEqual(self.api.next_seq("M9"), 1)


class PathSegmentSafetyTest(unittest.IsolatedAsyncioTestCase):
    """group_openid / interaction_id 来自**事件**，是不可信输入。

    它们只拼在同一主机上，构不成 SSRF；但未校验的片段能拼出 `../` 之类的东西
    去命中非预期端点。所以进路径前必须校验。
    """

    async def asyncSetUp(self) -> None:
        self.transport = FakeTransport()
        self.api = Api(make_config(), transport=self.transport)
        self.transport.push(token_response())

    async def test_rejects_traversal_in_interaction_id(self) -> None:
        with self.assertRaises(ValueError):
            await self.api.ack_interaction("../../admin/secret")

    async def test_rejects_slash_in_group_openid(self) -> None:
        with self.assertRaises(ValueError):
            await self.api.send_markdown("G1/messages/../../x", "# hi", msg_id="M", msg_seq=1)

    async def test_rejects_empty_id(self) -> None:
        with self.assertRaises(ValueError):
            await self.api.ack_interaction("")

    async def test_rejects_dot_only_segments(self) -> None:
        """`..` 字符集里合法、quote() 也不编码它，会拼出 `/v2/groups/../messages`。

        审查指出：原实现声称挡住了 `../`，但只挡住了含斜杠的形式。
        """
        for bad in ("..", ".", "..."):
            with self.assertRaises(ValueError, msg=f"{bad!r} 应被拒绝"):
                await self.api.ack_interaction(bad)
            with self.assertRaises(ValueError, msg=f"{bad!r} 应被拒绝"):
                await self.api.send_markdown(bad, "# hi", msg_id="M", msg_seq=1)

    async def test_accepts_realistic_ids(self) -> None:
        """真实的 openid 是 32 位十六进制，interaction id 是 uuid。"""
        await self.api.ack_interaction("844c370e-2f8c-4c53-861c-8067f57ba7f6")
        await self.api.send_markdown(
            "E4A877464EB83C1447BD828C6CD89E34", "# hi", msg_id="M", msg_seq=1
        )
        self.assertIn(
            "/v2/groups/E4A877464EB83C1447BD828C6CD89E34/messages", self.transport.paths
        )


class InteractionAckTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_ack_client_timeout_exceeds_platform_window(self) -> None:
        """客户端超时必须**大于**平台的 3 秒应答窗口。

        搞反过一次：客户端 3 秒就放弃等待，而这条链路一个来回可能更久。
        客户端超时并不会撤销服务端已做的处理，于是应答其实成功了、
        我们却记成失败 —— 日志在说谎。
        """
        from chargebread.api import ACK_TIMEOUT, INTERACTION_ACK_MS

        self.assertGreater(
            ACK_TIMEOUT * 1000,
            INTERACTION_ACK_MS,
            "客户端超时要留出网络往返回旋余地，否则必然误报",
        )


class GatewayUrlTest(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_url_is_validated_before_use(self) -> None:
        """网关地址来自接口响应，不能无条件信任 —— 要过 netguard。"""
        transport = FakeTransport()
        transport.push(token_response())
        transport.push(HttpResponse(200, {"url": "wss://api.sgroup.qq.com/websocket"}))
        api = Api(make_config(), transport=transport)
        url = await api.gateway_url()
        self.assertEqual(url, "wss://api.sgroup.qq.com/websocket")

    async def test_internal_gateway_url_is_rejected(self) -> None:
        from chargebread.netguard import UnsafeUrlError

        transport = FakeTransport()
        transport.push(token_response())
        transport.push(HttpResponse(200, {"url": "wss://127.0.0.1/websocket"}))
        api = Api(make_config(), transport=transport)
        with self.assertRaises(UnsafeUrlError):
            await api.gateway_url()

    async def test_plaintext_gateway_url_is_rejected(self) -> None:
        from chargebread.netguard import UnsafeUrlError

        transport = FakeTransport()
        transport.push(token_response())
        transport.push(HttpResponse(200, {"url": "ws://api.sgroup.qq.com/websocket"}))
        api = Api(make_config(), transport=transport)
        with self.assertRaises(UnsafeUrlError):
            await api.gateway_url()


class TokenKeeperTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_refresh_keeps_token_warm(self) -> None:
        """后台续期的心跳：token 快过期时自动换新，且不阻塞任何请求。"""
        transport = FakeTransport()
        transport.push(token_response("T1", expires_in=1))
        transport.push(token_response("T2", expires_in=7200))
        api = Api(make_config(), transport=transport, token_check_interval=0.01)

        await api.ensure_token()
        task = asyncio.create_task(api.run_token_keeper())
        try:
            for _ in range(100):
                await asyncio.sleep(0.01)
                if transport.paths.count("/app/getAppAccessToken") >= 2:
                    break
        finally:
            api.stop_token_keeper()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertGreaterEqual(transport.paths.count("/app/getAppAccessToken"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
