#!/usr/bin/env python3
"""
充能面包 Bot — QQ 群聊平台能力探针

存在的唯一理由：用真实凭据回答几个决定架构的问题，而不是靠社区传闻。

  1. 群消息事件的 author 里到底有没有 union_openid / username，是不是真的有值？
     （决定「全部榜」和「固定昵称」能不能做）
  2. 群聊相关 intents（1<<24 / 1<<25 / 1<<26）是否需要单独申请？
     （不授权时网关会以 4014 关闭连接，这直接决定 bot 能不能收到消息）
  3. 群成员信息接口是否白名单制（错误码 11253）？
     （决定能不能自动拿到昵称，以及 union_openid 是否只能从那里取）
  4. 群基本信息 / 机器人群内状态接口是否可用？

用法：
    python3 probe/probe.py                 # 完整探测（HTTP 接口 + intent 授权 + 监听）
    python3 probe/probe.py --intents-only  # 只测 intent 授权，秒出结果
    python3 probe/probe.py --listen 300    # 监听 300 秒等群消息
    python3 probe/probe.py --allow-send    # 允许往群里发测试消息

所有原始响应都会落到 probe/logs/ 下，方便回头看证据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.netguard import UnsafeUrlError, assert_public_ws_url  # noqa: E402

try:  # aiohttp >= 3.10 用 ClientWSTimeout，老版本仍是裸 float
    from aiohttp import ClientWSTimeout

    _WS_TIMEOUT: object = ClientWSTimeout(ws_close=20)
except ImportError:  # pragma: no cover
    _WS_TIMEOUT = 20

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "probe" / "logs"
CST = timezone(timedelta(hours=8))

DEFAULT_API_BASE = "https://api.bot.qq.com"

# ---- intents（官方文档 event-emit 页）-----------------------------------------
GUILDS = 1 << 0  # 频道：默认有权限
GUILD_MEMBERS = 1 << 1  # 频道成员：默认有权限
GROUP_MEMBER_EVENT = 1 << 24  # 群成员变动
GROUP_AND_C2C_EVENT = 1 << 25  # 群@消息 + 单聊消息
INTERACTION = 1 << 26  # 按钮/交互回调
PUBLIC_GUILD_MESSAGES = 1 << 30  # 频道公开消息：默认有权限

INTENT_CANDIDATES: list[tuple[str, int]] = [
    ("只有默认的频道 intent（对照组）", GUILDS | PUBLIC_GUILD_MESSAGES),
    ("群@消息 + 单聊 (1<<25)", GROUP_AND_C2C_EVENT),
    ("群@消息 + 按钮回调 (1<<25|1<<26)", GROUP_AND_C2C_EVENT | INTERACTION),
    (
        "群@消息 + 按钮回调 + 群成员变动 (1<<24|1<<25|1<<26)",
        GROUP_MEMBER_EVENT | GROUP_AND_C2C_EVENT | INTERACTION,
    ),
    (
        "出厂全量 (1<<0|1<<30|1<<24|1<<25|1<<26)",
        GUILDS | PUBLIC_GUILD_MESSAGES | GROUP_MEMBER_EVENT | GROUP_AND_C2C_EVENT | INTERACTION,
    ),
]

DOC_EXAMPLE_IMAGE = (
    "https://resource5-1255303497.cos.ap-guangzhou.myqcloud.com"
    "/abcmouse_word_watch/markdown/building.png"
)

# 只关心这些事件，其余也照dump但标记为 other
FOCUS_EVENTS = {
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",
    "C2C_MESSAGE_CREATE",
    "INTERACTION_CREATE",
    "GROUP_ADD_ROBOT",
    "GROUP_DEL_ROBOT",
    "GROUP_MEMBER_ADD",
    "GROUP_MEMBER_REMOVE",
    "GROUP_MSG_RECEIVE",
    "GROUP_MSG_REJECT",
    "GROUP_JOIN_REQUEST",
}


# ---- 小工具 ------------------------------------------------------------------
def now_tag() -> str:
    return datetime.now(CST).strftime("%H:%M:%S")


def stamp() -> str:
    return datetime.now(CST).strftime("%Y%m%d-%H%M%S")


def hdr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def sub(title: str) -> None:
    print(f"\n--- {title} ---")


def dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False)


def save_log(name: str, payload) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{stamp()}_{name}.json"
    path.write_text(
        payload if isinstance(payload, str) else dump_json(payload), encoding="utf-8"
    )
    return path


def load_env(path: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not path.exists():
        return cfg
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip().strip("'\"")
    return cfg


def describe_author(author: dict | None) -> dict:
    """把 author 对象里我们关心的字段单独拎出来说人话。"""
    author = author or {}
    username = author.get("username")
    union_openid = author.get("union_openid")
    union_account = author.get("union_user_account")
    return {
        "字段全集": sorted(author.keys()),
        "member_openid": author.get("member_openid"),
        "user_openid": author.get("user_openid"),
        "username": username,
        "username_有值": bool(username),
        "union_openid": union_openid,
        "union_openid_有值": bool(union_openid),
        "union_user_account": union_account,
        "union_user_account_有值": bool(union_account),
        "member_role": author.get("member_role"),
    }


# ---- API 客户端 --------------------------------------------------------------
class Api:
    def __init__(self, base: str, app_id: str, secret: str) -> None:
        self.base = base.rstrip("/")
        self.app_id = app_id
        self.secret = secret
        self.token: str | None = None
        self.expires_at: float = 0.0
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "Api":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25),
            headers={"User-Agent": "chargebread-probe/0.1"},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        assert self._session is not None, "Api 必须在 async with 里使用"
        return self._session

    async def fetch_token(self, attempts: int = 3) -> dict:
        """POST /app/getAppAccessToken —— 注意业务失败也是 HTTP 200。

        网络抖动很常见（尤其 WSL2），所以带退避重试。
        """
        url = f"{self.base}/app/getAppAccessToken"
        body = {"appId": self.app_id, "clientSecret": self.secret}
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self.session.post(url, json=body) as resp:
                    data = await resp.json(content_type=None)
                    status = resp.status
                if not isinstance(data, dict) or "access_token" not in data:
                    # 业务错误不重试：凭据问题重试多少次都一样
                    raise RuntimeError(f"取 token 失败 (HTTP {status}): {dump_json(data)}")
                self.token = data["access_token"]
                self.expires_at = time.time() + float(data.get("expires_in", 7200))
                return {"http_status": status, "expires_in": data.get("expires_in")}
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络类异常才重试
                last = exc
                if attempt < attempts:
                    wait = 1.5 * attempt
                    print(f"    第 {attempt} 次失败（{type(exc).__name__}: {exc!r}），{wait:.1f}s 后重试")
                    await asyncio.sleep(wait)
        raise RuntimeError(f"取 token 连续 {attempts} 次失败: {type(last).__name__}: {last!r}")

    async def request(self, method: str, path: str, **kwargs) -> dict:
        """带鉴权的请求，把 HTTP 状态和响应体一起返回，方便看错误码。"""
        if self.token is None or time.time() > self.expires_at - 120:
            await self.fetch_token()
        url = path if path.startswith("http") else f"{self.base}{path}"
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"QQBot {self.token}"
        async with self.session.request(method, url, headers=headers, **kwargs) as resp:
            raw = await resp.text()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"_raw_text": raw[:2000]}
            return {"http_status": resp.status, "body": parsed}

    async def get(self, path: str) -> dict:
        return await self.request("GET", path)

    async def post(self, path: str, payload: dict) -> dict:
        return await self.request("POST", path, json=payload)


def report_api_result(label: str, result: dict, expect_ok: bool = False) -> bool:
    """打印一个接口探测结果，返回是否"成功拿到数据"。"""
    status = result["http_status"]
    body = result["body"]
    code = body.get("code") if isinstance(body, dict) else None
    ok = status == 200 and (code in (0, None))
    mark = "✅" if ok else "❌"
    print(f"{mark} {label}  (HTTP {status}{', code=' + str(code) if code else ''})")
    if not ok and isinstance(body, dict):
        msg = body.get("message") or body.get("err_msg") or body.get("_raw_text")
        if msg:
            print(f"     message: {msg}")
        err = body.get("err_code")
        if err:
            print(f"     err_code: {err}")
        if code == 11253:
            print("     ⛔ 11253 = 该接口仅白名单机器人可用，需要向平台申请")
    elif ok:
        print(f"     {dump_json(body)[:1500]}")
    return ok


# ---- WebSocket ---------------------------------------------------------------
async def identify_probe(
    session: aiohttp.ClientSession,
    ws_url: str,
    token: str,
    intents: int,
    wait_seconds: float = 6.0,
) -> dict:
    """用给定 intents 连一次网关，看能不能拿到 READY。"""
    outcome: dict = {
        "intents": intents,
        "intents_hex": hex(intents),
        "connected": False,
        "ready": False,
        "session_id": None,
        "close_code": None,
        "error": None,
        "hello": None,
    }
    ws = None
    try:
        ws = await session.ws_connect(ws_url, timeout=_WS_TIMEOUT, heartbeat=None)
        outcome["connected"] = True

        first = await asyncio.wait_for(ws.receive(), timeout=15)
        if first.type is not aiohttp.WSMsgType.TEXT:
            outcome["error"] = f"首帧不是文本: {first.type}"
            return outcome
        hello = json.loads(first.data)
        outcome["hello"] = hello
        if hello.get("op") != 10:
            outcome["error"] = f"首帧 op 不是 10(Hello)，而是 {hello.get('op')}"
            return outcome

        await ws.send_json(
            {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": intents,
                    "shard": [0, 1],
                    "properties": {
                        "$os": "linux",
                        "$browser": "chargebread-probe",
                        "$device": "chargebread-probe",
                    },
                },
            }
        )

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if msg.type is aiohttp.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                op = payload.get("op")
                if op == 0 and payload.get("t") == "READY":
                    data = payload.get("d") or {}
                    outcome["ready"] = True
                    outcome["session_id"] = data.get("session_id")
                    outcome["ready_user"] = data.get("user")
                    break
                if op in (7, 9):
                    outcome["error"] = f"探测期间收到 op {op}（{payload}）"
                    break
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                break
            elif msg.type is aiohttp.WSMsgType.ERROR:
                outcome["error"] = f"WS 错误: {ws.exception()}"
                break
    except Exception as exc:  # noqa: BLE001 - 探针要把任何异常都记下来
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if ws is not None:
            outcome["close_code"] = ws.close_code
            if not ws.closed:
                await ws.close()
    return outcome


def explain_close_code(code: int | None) -> str:
    table = {
        4001: "无效的 opcode",
        4002: "无效的 payload",
        4006: "无效的 session",
        4007: "seq 错误",
        4008: "发送过快（限流）",
        4009: "连接过期",
        4010: "无效的 shard",
        4011: "分片过多",
        4012: "无效的版本",
        4013: "⛔ 无效的 intent —— intents 参数本身不合法",
        4014: "⛔ intent 无权限 —— 这个 intent 需要先向平台申请",
        4900: "内部错误",
        4914: "机器人已下架（仅沙箱）",
        4915: "机器人已被封禁",
    }
    if code is None:
        return ""
    return table.get(code, "未知关闭码")


async def probe_intents(session: aiohttp.ClientSession, ws_url: str, token: str) -> tuple[int, list[dict]]:
    sub("intent 授权探测（逐个组合连网，看哪一组被拒）")
    results: list[dict] = []
    best: int | None = None
    for label, intents in INTENT_CANDIDATES:
        outcome = await identify_probe(session, ws_url, token, intents)
        outcome["label"] = label
        results.append(outcome)
        if outcome["ready"]:
            best = intents
            print(f"✅ {label}")
            print(f"     intents={intents} ({hex(intents)})  session_id={outcome['session_id']}")
        else:
            close = outcome["close_code"]
            why = explain_close_code(close)
            print(f"❌ {label}")
            print(f"     intents={intents} ({hex(intents)})")
            print(f"     close_code={close} {why}")
            if outcome["error"]:
                print(f"     详情: {outcome['error']}")
        await asyncio.sleep(1.0)
    save_log("intent_probe", results)
    return (best or 0), results


async def listen(
    session: aiohttp.ClientSession,
    ws_url: str,
    token: str,
    intents: int,
    seconds: float,
    api: Api,
    allow_send: bool,
    url_test: bool = False,
    image_url: str = DOC_EXAMPLE_IMAGE,
) -> dict:
    """监听事件，原样 dump。抓到一个群消息就顺手把成员相关接口全打一遍。"""
    sub(f"监听事件（intents={intents} / {hex(intents)}），最多 {int(seconds)} 秒")
    print("现在去群里 @ 一下机器人，或者发一条消息。")

    seen: dict[str, int] = {}
    raw_events: list[dict] = []
    event_file: Path | None = None
    ready_user = None
    probed_for: set[tuple[str, str]] = set()
    sent_to: set[str] = set()
    session_id: str | None = None
    last_seq: int | None = None
    deadline = time.monotonic() + seconds

    ws = await session.ws_connect(ws_url, timeout=_WS_TIMEOUT, heartbeat=None)
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=15)
        hello = json.loads(first.data)
        interval = float((hello.get("d") or {}).get("heartbeat_interval", 45000)) / 1000.0
        print(f"Hello 收到，心跳间隔 {interval:.0f}s")

        await ws.send_json(
            {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": intents,
                    "shard": [0, 1],
                    "properties": {
                        "$os": "linux",
                        "$browser": "chargebread-probe",
                        "$device": "chargebread-probe",
                    },
                },
            }
        )

        async def heartbeat() -> None:
            while not ws.closed:
                await asyncio.sleep(interval)
                if ws.closed:
                    return
                try:
                    await ws.send_json({"op": 1, "d": last_seq})
                except Exception:  # noqa: BLE001
                    return

        hb_task = asyncio.create_task(heartbeat())
        keeper_stop = asyncio.Event()
        keeper_task = asyncio.create_task(token_keeper(api, keeper_stop))

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=max(0.1, remaining))
            except asyncio.TimeoutError:
                break

            if msg.type is not aiohttp.WSMsgType.TEXT:
                if msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    close = ws.close_code
                    print(f"\n⚠️  连接关闭 close_code={close} {explain_close_code(close)}")
                    break
                if msg.type is aiohttp.WSMsgType.ERROR:
                    print(f"\n⚠️  WS 错误: {ws.exception()}")
                    break
                continue

            payload = json.loads(msg.data)
            op = payload.get("op")
            if payload.get("s") is not None:
                last_seq = payload["s"]

            if op == 11:
                continue
            if op == 10:
                continue
            if op in (7, 9):
                print(f"\n⚠️  收到 op {op}（{'重连' if op == 7 else '无效 session'}），本探针不自动恢复")
                break
            if op != 0:
                print(f"[{now_tag()}] 未处理的 op={op}: {dump_json(payload)[:300]}")
                continue

            event = payload.get("t") or "UNKNOWN"
            data = payload.get("d") or {}
            seen[event] = seen.get(event, 0) + 1

            if event == "READY":
                ready_user = data.get("user")
                session_id = data.get("session_id")
                print(f"\n[{now_tag()}] ✅ READY")
                print(f"     机器人: {dump_json(ready_user)}")
                continue

            raw_events.append(payload)
            if event_file is None:
                event_file = save_log("events", [])
            event_file.write_text(dump_json(raw_events), encoding="utf-8")

            if event in FOCUS_EVENTS:
                print(f"\n[{now_tag()}] 📨 {event}")
            else:
                print(f"\n[{now_tag()}] · {event}")

            if event in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
                author = data.get("author") or {}
                group_openid = data.get("group_openid")
                member_openid = author.get("member_openid")
                print(f"     content     : {data.get('content')!r}")
                print(f"     message_type: {data.get('message_type')}  id={data.get('id')}")
                print(f"     group_openid: {group_openid}")
                print(f"     author      : {dump_json(describe_author(author))}")
                if data.get("mentions"):
                    print(f"     mentions    : {dump_json(data['mentions'])}")
                if data.get("attachments"):
                    print(f"     attachments : {dump_json(data['attachments'])}")
                print(f"     d 顶层字段  : {sorted(data.keys())}")

                key = (group_openid or "", member_openid or "")
                if key not in probed_for and group_openid and member_openid:
                    probed_for.add(key)
                    await probe_member_endpoints(api, group_openid, member_openid)
                if allow_send and group_openid and group_openid not in sent_to:
                    sent_to.add(group_openid)
                    if url_test:
                        await send_url_tests(api, group_openid, data, image_url)
                    else:
                        await send_probe_messages(api, group_openid, data)

            elif event == "C2C_MESSAGE_CREATE":
                author = data.get("author") or {}
                print(f"     content: {data.get('content')!r}")
                print(f"     author : {dump_json(describe_author(author))}")
                print(f"     d 顶层字段: {sorted(data.keys())}")

            elif event == "INTERACTION_CREATE":
                print(f"     type={data.get('type')} scene={data.get('scene')} chat_type={data.get('chat_type')}")
                print(f"     resolved={dump_json((data.get('data') or {}).get('resolved'))}")
                print(f"     d 顶层字段: {sorted(data.keys())}")
                await handle_interaction(api, payload)

            else:
                print(f"     d = {dump_json(data)[:800]}")

        hb_task.cancel()
        keeper_stop.set()
        keeper_task.cancel()
    finally:
        if not ws.closed:
            await ws.close()

    summary = {
        "intents": intents,
        "ready_user": ready_user,
        "session_id": session_id,
        "event_counts": seen,
        "events_captured": len(raw_events),
        "event_log": str(event_file) if event_file else None,
    }
    save_log("listen_summary", summary)
    return summary


async def probe_member_endpoints(api: Api, group_openid: str, member_openid: str) -> None:
    """抓到群消息后，把所有可能给出昵称/头像/跨群身份的接口打一遍。"""
    hdr("成员 / 群信息接口探测（决定昵称能不能自动拿）")
    print(f"group_openid = {group_openid}")
    print(f"member_openid = {member_openid}")

    results: dict[str, dict] = {}

    sub("1) 单个群成员信息 —— 是否返回 username / union_openid / avatar")
    r = await api.get(f"/v2/groups/{group_openid}/members/{member_openid}")
    results["member_detail"] = r
    report_api_result("GET /v2/groups/{group}/members/{member}", r)
    if r["http_status"] == 200 and isinstance(r["body"], dict) and "code" not in r["body"]:
        keys = sorted(r["body"].keys())
        print(f"     ⭐ 返回字段: {keys}")
        print(f"     avatar 字段存在? {'avatar' in r['body']}")
        print(f"     username = {r['body'].get('username')!r}")
        print(f"     union_openid = {r['body'].get('union_openid')!r}")

    sub("2) 群成员列表")
    r = await api.get(f"/v2/groups/{group_openid}/members")
    results["member_list"] = r
    report_api_result("GET /v2/groups/{group}/members", r)

    sub("3) 群基本信息")
    r = await api.get(f"/v2/groups/{group_openid}/info")
    results["group_info"] = r
    report_api_result("GET /v2/groups/{group}/info", r)

    sub("4) 机器人在本群的状态")
    r = await api.get(f"/v2/groups/{group_openid}/bot_state")
    results["bot_state"] = r
    report_api_result("GET /v2/groups/{group}/bot_state", r)

    sub("5) 机器人自身信息（/users/@me，看 avatar 字段长什么样）")
    r = await api.get("/users/@me")
    results["me"] = r
    report_api_result("GET /users/@me", r)

    save_log("member_endpoints", results)


async def handle_interaction(api: Api, envelope: dict) -> None:
    """按钮回调必须 3 秒内应答，否则用户客户端会一直转圈。

    实测踩到的两个坑（2026-09-21）：
      1. 应答路径上不能有 token 刷新 —— 那次因为启动时 token 只剩 97 秒，
         应答前触发刷新，耗时 10491 ms 直接超时。所以 listen 里加了
         token_keeper 后台预热，见下方。
      2. 两个 id 不是同一个东西：
           PUT /interactions/{id}  用 d.id              （无前缀的 uuid）
           被动回复的 event_id      用外层信封 id        （形如 INTERACTION_CREATE:uuid）
         传错会得到 40034025 请求参数event_id无效。
    """
    data = envelope.get("d") or {}
    interaction_id = data.get("id")
    envelope_id = envelope.get("id")
    if not interaction_id:
        print("     ⚠️ 回调里没有 id，无法应答")
        return
    resolved = (data.get("data") or {}).get("resolved") or {}

    sub(f"按钮回调应答 PUT /interactions/{interaction_id[:16]}…")
    t0 = time.monotonic()
    r = await api.request("PUT", f"/interactions/{interaction_id}", json={"code": 0})
    elapsed = time.monotonic() - t0
    report_api_result("PUT /interactions/{interaction_id}  {'code':0}", r)
    print(f"     应答耗时 {elapsed * 1000:.0f} ms（上限 3000 ms）"
          f"{'  ✅ 在时限内' if elapsed < 3 else '  ⛔ 超时了！'}")
    save_log("interaction_ack", r)

    group_openid = data.get("group_openid")
    if not group_openid:
        return
    sub("用 event_id 被动回复（用外层信封 id，不是 d.id）")
    print(f"     event_id = {envelope_id}")
    md = (
        "# 回调已收到 ✅\n"
        f"按钮数据: `{resolved.get('button_data') or '(无)'}`\n"
        f"button_id: `{resolved.get('button_id') or '(无)'}`\n\n"
        "应答与回复都通了，按钮方案可行。"
    )
    r2 = await api.post(
        f"/v2/groups/{group_openid}/messages",
        {"msg_type": 2, "markdown": {"content": md}, "event_id": envelope_id, "msg_seq": 1},
    )
    report_api_result("POST /v2/groups/{group}/messages 用 event_id 回复", r2)
    save_log("interaction_reply", r2)


async def token_keeper(api: Api, stop: asyncio.Event) -> None:
    """后台预热 token，保证关键路径（尤其是 3 秒应答）永远不需要等刷新。

    实测教训：token 剩 97 秒时启动，3 分钟后按钮回调到达，应答因为要
    现取 token 而耗时 10.5 秒，超过 3 秒上限直接失败。
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
            return
        except asyncio.TimeoutError:
            pass
        if time.time() > api.expires_at - 300:
            try:
                await api.fetch_token()
                print(f"[{now_tag()}] 🔑 token 已后台续期")
            except Exception as exc:  # noqa: BLE001
                print(f"[{now_tag()}] ⚠️ token 续期失败，稍后重试: {type(exc).__name__}: {exc!r}")


async def send_url_tests(api: Api, group_openid: str, event_data: dict, image_url: str) -> None:
    """验证 markdown 内嵌图片是否需要 URL 报备，并分离失败原因。

    四条实验共用一个 msg_id（被动窗口上限 5 条，用 1/2/3/4）。
    设计成能区分三种失败：
      · URL 门禁拦下所有 URL  → 超链接实验也会失败
      · 只有你的图床失败      → 官方示例图那条会成功（说明是端口/防盗链问题）
      · 图片抓不到            → 强校验那条报 40034004
    """
    hdr("URL / markdown 内嵌图片实验")
    print(f"你的图床    : {image_url}")
    print(f"官方示例图  : {DOC_EXAMPLE_IMAGE}")
    msg_id = event_data.get("id")
    path = f"/v2/groups/{group_openid}/messages"

    sub("实验 1／4 —— 文字 + 内嵌图（你的图床，默认参数）【目标形态】")
    md1 = (
        "# 充能面包 · 图床测试\n"
        "这是一段文字，下面应该出现一张面包图。\n\n"
        f"![充能面包 #160px #160px]({image_url})"
    )
    r1 = await api.post(path, {"msg_type": 2, "markdown": {"content": md1}, "msg_id": msg_id, "msg_seq": 1})
    report_api_result("msg_type=2  文字+图床内嵌图", r1)
    save_log("url_test_1_mine", r1)

    sub("实验 2／4 —— 文字 + 内嵌图（官方示例图，443 端口）【对照组：隔离端口因素】")
    md2 = (
        "# 充能面包 · 对照测试\n"
        "这张来自腾讯官方文档示例，走标准 443 端口。\n\n"
        f"![官方示例图 #160px #160px]({DOC_EXAMPLE_IMAGE})"
    )
    r2 = await api.post(path, {"msg_type": 2, "markdown": {"content": md2}, "msg_id": msg_id, "msg_seq": 2})
    report_api_result("msg_type=2  文字+官方示例图", r2)
    save_log("url_test_2_control", r2)

    sub("实验 3／4 —— 文字 + 普通超链接（探测 URL 门禁是否活着）")
    md3 = f"# 充能面包 · 超链接测试\n这是一个普通链接：[点我]({DOC_EXAMPLE_IMAGE})\n"
    r3 = await api.post(path, {"msg_type": 2, "markdown": {"content": md3}, "msg_id": msg_id, "msg_seq": 3})
    report_api_result("msg_type=2  文字+超链接", r3)
    save_log("url_test_3_link", r3)

    sub("实验 4／4 —— 图床图 + force_verify_image_resource=true（让转存失败暴露）")
    md4 = (
        "# 充能面包 · 转存强校验\n"
        "转存失败会直接报错，而不是静默留白。\n\n"
        f"![充能面包 #160px #160px]({image_url})"
    )
    r4 = await api.post(
        path,
        {
            "msg_type": 2,
            "markdown": {"content": md4, "force_verify_image_resource": True},
            "msg_id": msg_id,
            "msg_seq": 4,
        },
    )
    report_api_result("msg_type=2  图床图+强校验", r4)
    save_log("url_test_4_forceverify", r4)

    sub("判读")
    labels = (
        ("1 图床图(默认)", r1),
        ("2 官方图(对照)", r2),
        ("3 超链接(门禁)", r3),
        ("4 图床图(强校验)", r4),
    )
    for label, r in labels:
        body = r["body"] if isinstance(r["body"], dict) else {}
        code = body.get("code")
        if r["http_status"] == 200 and not code:
            print(f"  ✅ {label}: 发送成功")
        else:
            hint = {
                40054010: "不允许发送URL —— URL 门禁拦下了这条",
                304003: "url 未报备 —— 需要去控制台报备该域名",
                40034004: "富媒体信息转存失败 —— 平台抓不到这个图片 URL",
                850026: "下载原始文件失败 —— 图片源不可达",
                40034124: "markdown 参数错误 —— 语法问题",
                40034011: "无效的 markdown 内容 —— 语法问题",
            }.get(code, "")
            print(f"  ❌ {label}: code={code} {body.get('message') or ''} {hint}")

    ok_control, ok_mine = _sent(r2), _sent(r1)
    if ok_control and not ok_mine:
        print("\n  结论：门禁没拦、官方图能转存，但你的图床不行 → 大概率是 8443 端口或防盗链")
    elif not ok_control and not _sent(r3):
        print("\n  结论：连超链接都被拦 → URL 门禁是活的，markdown 内嵌图这条路需要报备域名")
    elif ok_control and ok_mine:
        print("\n  结论：✅ 你的图床可以直接用，不需要报备")


def _sent(result: dict) -> bool:
    body = result["body"] if isinstance(result["body"], dict) else {}
    return result["http_status"] == 200 and not body.get("code")


async def send_probe_messages(api: Api, group_openid: str, event_data: dict) -> None:
    """可选：被动回复一条纯文本 + 一条 markdown（带按钮），验证发送能力。"""
    hdr("发送能力探测（--allow-send 才会执行）")
    msg_id = event_data.get("id")
    path = f"/v2/groups/{group_openid}/messages"

    sub("1) 纯文本被动回复 (msg_type=0)")
    r = await api.post(
        path,
        {"msg_type": 0, "content": "充能面包探针：纯文本回复测试", "msg_id": msg_id, "msg_seq": 1},
    )
    report_api_result("POST /v2/groups/{group}/messages  msg_type=0", r)
    save_log("send_text", r)

    sub("2) markdown 被动回复 (msg_type=2)")
    md = "# 充能面包探针\n**markdown** 测试\n- 列表项 A\n- 列表项 B\n\n> 引用测试\n\n***\n\n`## 二级标题` 与分割线是否生效"
    r = await api.post(
        path,
        {"msg_type": 2, "markdown": {"content": md}, "msg_id": msg_id, "msg_seq": 2},
    )
    report_api_result("POST /v2/groups/{group}/messages  msg_type=2", r)
    save_log("send_markdown", r)

    sub("3) markdown + 按钮被动回复 (keyboard)")
    md = "# 按钮测试\n点击下面的按钮，看回调能不能收到"
    kb = {
        "content": {
            "rows": [
                {
                    "buttons": [
                        {
                            "id": "btn_a",
                            "render_data": {"label": "回调按钮", "visited_label": "已点击", "style": 1},
                            "action": {
                                "type": 1,
                                "permission": {"type": 2},
                                "data": "probe:callback",
                                "unsupport_tips": "请升级QQ版本",
                            },
                        },
                        {
                            "id": "btn_b",
                            "render_data": {"label": "指令按钮", "visited_label": "已点击", "style": 0},
                            "action": {
                                "type": 2,
                                "permission": {"type": 2},
                                "data": "充能面包",
                                "unsupport_tips": "请升级QQ版本",
                            },
                        },
                    ]
                }
            ]
        }
    }
    r = await api.post(
        path,
        {"msg_type": 2, "markdown": {"content": md}, "keyboard": kb, "msg_id": msg_id, "msg_seq": 3},
    )
    report_api_result("POST /v2/groups/{group}/messages  msg_type=2 + keyboard", r)
    save_log("send_markdown_buttons", r)
    print("\n👉 现在去群里点一下那两个按钮，看第 4 节能不能收到 INTERACTION_CREATE")


# ---- main --------------------------------------------------------------------
async def amain(args: argparse.Namespace) -> int:
    env = load_env(ROOT / ".env")
    app_id = args.app_id or env.get("QBOT_APP_ID") or os.environ.get("QBOT_APP_ID") or ""
    secret = args.app_secret or env.get("QBOT_APP_SECRET") or os.environ.get("QBOT_APP_SECRET") or ""
    if not app_id or not secret:
        print("缺少 QBOT_APP_ID / QBOT_APP_SECRET，请填到 .env 或用 --app-id / --app-secret 传入", file=sys.stderr)
        return 2

    listen_seconds = args.listen or float(env.get("PROBE_LISTEN_SECONDS") or 180)
    allow_send = args.allow_send or env.get("PROBE_ALLOW_SEND", "0") in ("1", "true", "yes")
    url_test = bool(args.url_test)
    allow_send = allow_send or url_test
    image_url = args.image_url or env.get("PROBE_IMAGE_URL") or DOC_EXAMPLE_IMAGE

    hdr("0. 凭据与域名")
    print(f"AppID      : {app_id}")
    print(f"AppSecret  : 已读取（{len(secret)} 字符，不回显）")
    print(f"API base   : {args.api_base}")

    async with Api(args.api_base, app_id, secret) as api:
        sub("取 access_token")
        try:
            info = await api.fetch_token()
            print(f"✅ token 获取成功，expires_in={info['expires_in']}s，长度={len(api.token or '')}")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ {type(exc).__name__}: {exc!r}")
            return 1

        sub("机器人自身信息 GET /users/@me")
        r = await api.get("/users/@me")
        report_api_result("GET /users/@me", r)
        save_log("users_me", r)

        sub("网关地址 GET /gateway")
        r = await api.get("/gateway")
        report_api_result("GET /gateway", r)
        ws_url = (r["body"] or {}).get("url") if isinstance(r["body"], dict) else None
        if not ws_url:
            print("❌ 拿不到网关地址，后续无法进行")
            return 1
        print(f"     网关: {ws_url}")
        try:
            assert_public_ws_url(ws_url, require_wss=True)
            print("     ✅ 网关地址通过公网 wss 校验")
        except UnsafeUrlError as exc:
            print(f"     ⛔ 网关地址未通过校验: {exc}")
            return 1

        sub("网关分片信息 GET /gateway/bot")
        r = await api.get("/gateway/bot")
        report_api_result("GET /gateway/bot", r)
        save_log("gateway_bot", r)

        # 实测已在 2026-09-21 验证：群聊相关 intent 无需申请，全部可连。
        working_intents = (
            GUILDS | PUBLIC_GUILD_MESSAGES | GROUP_MEMBER_EVENT | GROUP_AND_C2C_EVENT | INTERACTION
        )

        best: int | None = None
        if args.intents is None:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
                best, _ = await probe_intents(session, ws_url, api.token or "")
            if not best:
                hdr("结论")
                print("❌ 没有任何 intent 组合能连上。")
                print("   如果每组都是 close_code=4014，说明群聊 intent 尚未授权，")
                print("   需要去 q.qq.com 开放平台申请对应的事件权限。")
                return 1
            print(f"\n✅ 可用的 intents 组合: {best} ({hex(best)})")
        else:
            best = working_intents if args.intents == "default" else int(args.intents, 0)
            print(f"\n跳过 intent 扫描，直接使用 intents={best} ({hex(best)})")

        if args.intents_only:
            hdr("结论")
            print(f"✅ 可用的 intents: {best} ({hex(best)})")
            return 0

        # 长连接用独立会话：握手给足时间（到腾讯的链路会抖），总超时不设，
        # 靠 listen 自己的 deadline 收口。
        ws_timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_connect=60)
        summary: dict | None = None
        for attempt in range(1, 6):
            try:
                async with aiohttp.ClientSession(timeout=ws_timeout) as session:
                    summary = await listen(
                        session,
                        ws_url,
                        api.token or "",
                        best,
                        listen_seconds,
                        api,
                        allow_send,
                        url_test=url_test,
                        image_url=image_url,
                    )
                break
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
                print(f"\n⚠️ 监听会话异常（第 {attempt}/5 次）: {type(exc).__name__}: {exc!r}")
                if attempt == 5:
                    print("❌ 连续失败，放弃")
                    return 1
                wait = min(30.0, 2.0**attempt)
                print(f"   {wait:.0f}s 后重连…（注意：重连会重置本次会话的事件日志）")
                await asyncio.sleep(wait)
        if summary is None:
            print("❌ 未能建立监听会话")
            return 1

    hdr("结论")
    print(f"可用 intents : {best} ({hex(best)})")
    print(f"事件统计     : {dump_json(summary['event_counts'])}")
    if summary["events_captured"] == 0:
        print("\n⚠️  没抓到任何事件。可能原因：")
        print("   - 没人在这段时间里 @ 机器人（这是最常见的原因）")
        print("   - 机器人不在对应群里，或者群消息需要在群里 @ 才触发")
        print("   - 后台『事件订阅与回调地址』被设成了 webhook 而不是 WebSocket")
    print(f"\n原始日志都在: {LOG_DIR}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="充能面包 Bot 平台能力探针")
    p.add_argument("--app-id", default=None, help="覆盖 .env 里的 AppID")
    p.add_argument("--app-secret", default=None, help="覆盖 .env 里的 AppSecret")
    p.add_argument("--api-base", default=DEFAULT_API_BASE, help=f"API 域名，默认 {DEFAULT_API_BASE}")
    p.add_argument("--listen", type=float, default=None, help="监听秒数，默认取 .env 或 180")
    p.add_argument("--intents-only", action="store_true", help="只测 intent 授权，不监听")
    p.add_argument(
        "--intents",
        default=None,
        help="跳过 intent 扫描，直接用这个值监听（接受 0x 前缀的十六进制）",
    )
    p.add_argument("--allow-send", action="store_true", help="允许往群里发测试消息")
    p.add_argument(
        "--url-test",
        action="store_true",
        help="把发出的测试消息换成 URL/markdown 内嵌图片实验（隐含 --allow-send）",
    )
    p.add_argument(
        "--image-url",
        default=None,
        help="实验用的图片地址，默认用官方文档示例图；也可用 .env 的 PROBE_IMAGE_URL",
    )
    return p


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(amain(build_parser().parse_args())))
    except KeyboardInterrupt:
        print("\n已中断")
        raise SystemExit(130)
