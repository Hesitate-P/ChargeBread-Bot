"""HTTP 客户端：token 管理、发消息、按钮应答。

三条约束是探针实测踩出来的，不是照文档猜的：

1. **token 续期不能在关键路径上。** 那次启动时 token 只剩 97 秒，按钮回调到达时
   已过期，应答因为现取 token 花了 10491 ms，超过平台 3 秒上限直接失败。
   所以 token_keeper 在后台提前换新，`ensure_token()` 平时只是读缓存。

2. **业务失败走 HTTP 200。** 必须看 body 里的 `code`，不能只看 HTTP 状态码。
   业务错误重试没有意义（凭据错了试一百次还是错），所以只对网络类异常重试。

3. **按钮应答不能重试。** `PUT /interactions/{id}` 只有 3 秒窗口，且同一个
   interaction_id 只能应答一次 —— 重试可能撞上"重复操作"，还可能挤掉一次
   本来能成功的应答。所以应答是"一次机会"。

传输层做成可注入的（`Transport` 协议），测试塞假实现就能断言行为，
不需要 mock aiohttp 的内部调用。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

from .config import Config
from .netguard import assert_public_ws_url

# 平台限制
PASSIVE_REPLY_MAX = 5      # 每条消息最多回复 5 次
INTERACTION_ACK_MS = 3000  # 平台给客户端的应答窗口

# 应答请求的**客户端**超时。注意它和上面那个窗口是两件事：
# 搞反过一次 —— 客户端设成 3 秒就放弃等待，而这条链路一个来回可能更久。
# 客户端超时并不会撤销服务端已做的处理，于是应答其实成功了、我们却记成失败，
# 日志在说谎。所以要留出网络往返回旋余地把结果等回来。
ACK_TIMEOUT = 10.0

# token 提前多久续期
TOKEN_REFRESH_MARGIN = 300.0
TOKEN_KEEPER_INTERVAL = 60.0

# 网络类异常的重试次数（业务错误不重试）
DEFAULT_ATTEMPTS = 3
RETRY_BACKOFF = 0.3

# 发消息的超时。**必须远小于传输层的 30 秒**：实测这条链路会抖，
# 一次卡住的发送会把整个事件处理堵住，期间所有人收不到回复、
# 被动回复的 5 分钟窗口还在流逝。宁可快速失败也不需要长挂。
SEND_TIMEOUT = 10.0

log = logging.getLogger("chargebread.api")

# 路径片段白名单。group_openid / interaction_id 都来自**事件**，是不可信输入：
# 它们只拼在同一主机上，构不成 SSRF，但未校验的片段能拼出 `../` 去命中非预期端点。
# 纯点号段（`..`、`.`）单独拒绝 —— 点在白名单字符集里，`quote()` 也不会编码它。
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _segment(value: str, what: str) -> str:
    """校验并编码一个路径片段。非法就抛 ValueError，绝不拼进 URL。"""
    if (
        not isinstance(value, str)
        or not _SAFE_SEGMENT.match(value)
        or value.strip(".") == ""
    ):
        raise ValueError(f"{what} 含非法字符，拒绝用于请求路径: {value!r}")
    return quote(value, safe="")

# next_seq 缓存上限，防止长跑之后无限增长
SEQ_CACHE_LIMIT = 512


class TransportError(RuntimeError):
    """网络层失败 —— 可重试。"""


class ApiError(RuntimeError):
    """平台返回的业务错误 —— 不可重试，带平台的错误码。"""

    def __init__(
        self,
        code: int | None,
        message: str,
        *,
        err_code: int | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.err_code = err_code
        self.http_status = http_status


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: dict[str, Any]


class Transport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float | None = None,
    ) -> HttpResponse: ...


@dataclass
class _TokenState:
    value: str | None = None
    expires_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_fresh(self, margin: float = TOKEN_REFRESH_MARGIN) -> bool:
        return bool(self.value) and time.monotonic() < self.expires_at - margin


class AiohttpTransport:
    """默认传输层。只在这里碰 aiohttp，方便替换与测试。"""

    def __init__(self) -> None:
        self._session: Any = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> Any:
        import aiohttp

        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30, connect=15)
                )
            return self._session

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float | None = None,
    ) -> HttpResponse:
        import aiohttp

        session = await self._get_session()
        try:
            kwargs: dict[str, Any] = {"headers": headers, "allow_redirects": False}
            if json_body is not None:
                kwargs["json"] = json_body
            if timeout is not None:
                kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
            async with session.request(method, url, **kwargs) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001 - 非 JSON 响应也要能把状态带回去
                    body = {"_raw": (await resp.text())[:500]}
                if not isinstance(body, dict):
                    body = {"_body": body}
                return HttpResponse(resp.status, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class Api:
    def __init__(
        self,
        config: Config,
        *,
        transport: Transport | None = None,
        token_check_interval: float = TOKEN_KEEPER_INTERVAL,
    ) -> None:
        self.config = config
        self.base = config.api_base.rstrip("/")
        self.transport: Transport = transport or AiohttpTransport()
        self._token = _TokenState()
        self._seq: dict[str, int] = {}
        self._seq_order: list[str] = []
        self._token_check_interval = token_check_interval
        self._keeper_stop = asyncio.Event()

    # ---- token ------------------------------------------------------------
    async def fetch_token(self) -> str:
        """取新 token。业务失败抛 ApiError，网络失败抛 TransportError。"""
        response = await self.transport.request(
            "POST",
            f"{self.base}/app/getAppAccessToken",
            json_body={"appId": self.config.app_id, "clientSecret": self.config.app_secret},
            headers={"Content-Type": "application/json"},
        )
        body = response.body
        token = body.get("access_token")
        if not token:
            raise ApiError(
                body.get("code"),
                body.get("message") or "取 token 失败",
                err_code=body.get("err_code"),
                http_status=response.status,
            )
        expires_in = float(body.get("expires_in") or 0)
        self._token.value = str(token)
        self._token.expires_at = time.monotonic() + expires_in
        return str(token)

    async def ensure_token(self) -> str:
        """读缓存；只有确实快过期才去换。加锁避免并发重复取。"""
        if self._token.is_fresh():
            assert self._token.value is not None
            return self._token.value
        async with self._token.lock:
            if self._token.is_fresh():
                assert self._token.value is not None
                return self._token.value
            return await self.fetch_token()

    def stop_token_keeper(self) -> None:
        self._keeper_stop.set()

    async def run_token_keeper(self) -> None:
        """后台预热 token，保证关键路径（尤其是 3 秒应答）永远不用等刷新。"""
        while not self._keeper_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._keeper_stop.wait(), timeout=self._token_check_interval
                )
                return
            except asyncio.TimeoutError:
                pass
            if not self._token.is_fresh():
                try:
                    await self.fetch_token()
                except Exception:  # noqa: BLE001 - 续期失败下轮再试，但必须让运维看见
                    log.warning("token 后台续期失败，将在下轮重试", exc_info=True)

    # ---- 通用请求 ---------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        timeout: float | None = None,
        dedup_code_as_success: bool = False,
    ) -> dict[str, Any]:
        """发一次 API 请求。业务失败抛 ApiError，网络失败重试后抛 TransportError。

        `dedup_code_as_success`：发消息专用。第一次请求若实际到达了服务端而
        **响应**丢了，重试会收到 40054005「消息被去重」—— 这恰恰证明消息
        已经落地。此时必须当成功处理，否则上层会把事件撤销标记、等平台重投，
        然后用**新的 msg_seq** 再发一遍，用户就看到两条一样的回复。
        """
        token = await self.ensure_token()
        url = f"{self.base}{path}"
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}

        last: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = await self.transport.request(
                    method, url, json_body=payload, headers=headers, timeout=timeout
                )
            except TransportError as exc:
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise
            body = response.body
            code = body.get("code")
            if dedup_code_as_success and code == 40054005:
                log.info("平台返回 40054005（消息被去重）：消息此前已送达，按成功处理")
                return body
            # 业务失败：HTTP 可能是 200，必须看 code。不重试。
            if code not in (None, 0):
                raise ApiError(
                    code,
                    body.get("message") or body.get("msg") or "请求失败",
                    err_code=body.get("err_code"),
                    http_status=response.status,
                )
            return body
        raise last if last else TransportError("请求失败")

    async def get(self, path: str) -> dict[str, Any]:
        return await self.request("GET", path)

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", path, payload)

    async def gateway_url(self) -> str:
        """取网关地址并校验。

        地址来自接口响应，属于不可信输入 —— 实测返回的是
        `wss://api.sgroup.qq.com/websocket`，与文档写的域名并不一致，
        所以更不能盲目连过去。
        """
        body = await self.get("/gateway")
        url = body.get("url")
        if not url or not isinstance(url, str):
            raise ApiError(None, f"网关响应里没有 url: {body!r}")
        return assert_public_ws_url(url, require_wss=True)

    # ---- 发消息 -----------------------------------------------------------
    def next_seq(self, msg_id: str) -> int:
        """同一条 msg_id 的多次回复必须用递增且不重复的 msg_seq。

        重复会报 40054005「消息被去重」；而群聊每个 msg_id 最多回复 5 次。
        """
        current = self._seq.get(msg_id, 0) + 1
        if msg_id not in self._seq:
            self._seq_order.append(msg_id)
            if len(self._seq_order) > SEQ_CACHE_LIMIT:
                oldest = self._seq_order.pop(0)
                self._seq.pop(oldest, None)
        self._seq[msg_id] = current
        return current

    async def send_markdown(
        self,
        group_openid: str,
        content: str,
        *,
        keyboard: dict[str, Any] | None = None,
        msg_id: str | None = None,
        event_id: str | None = None,
        msg_seq: int = 1,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "msg_type": 2,
            "markdown": {"content": content},
            "msg_seq": msg_seq,
        }
        if keyboard is not None:
            payload["keyboard"] = keyboard
        # msg_id 与 event_id 二选一，绝不能同时出现
        if event_id:
            payload["event_id"] = event_id
        elif msg_id:
            payload["msg_id"] = msg_id
        return await self.request(
            "POST",
            f"/v2/groups/{_segment(group_openid, 'group_openid')}/messages",
            payload,
            timeout=SEND_TIMEOUT,
            dedup_code_as_success=True,
        )

    # ---- 按钮应答 ---------------------------------------------------------
    async def ack_interaction(self, interaction_id: str) -> None:
        """3 秒内应答，否则用户客户端一直转圈。一次机会，不重试。

        客户端超时用 ACK_TIMEOUT（比平台的 3 秒窗口宽），否则会误报失败 ——
        超时只是我们不等了，服务端很可能已经受理。
        """
        await self.request(
            "PUT",
            f"/interactions/{_segment(interaction_id, 'interaction_id')}",
            {"code": 0},
            attempts=1,
            timeout=ACK_TIMEOUT,
        )

    async def close(self) -> None:
        self.stop_token_keeper()
        close = getattr(self.transport, "close", None)
        if close is not None:
            await close()
