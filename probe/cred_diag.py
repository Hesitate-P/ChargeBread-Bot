#!/usr/bin/env python3
"""一次性诊断：定位 100016 到底是 AppID 还是 AppSecret 被拒。

从项目 .env 读凭据，脚本本身不硬编码任何密钥。
出站请求前过 netguard：协议校验 + 解析后 IP 边界校验 + 主机白名单 + 禁跟随重定向。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.netguard import (  # noqa: E402
    UnsafeUrlError,
    assert_allowed_host,
    assert_public_http_url,
    make_no_redirect_opener,
)

ENV = Path("/home/hesitate-p/chargebread-bot/.env")
GARBAGE_SECRET = "definitely_not_a_valid_secret_000"
TOKEN_PATH = "/app/getAppAccessToken"

# 只允许打这两个官方域名
ALLOWED_HOSTS = {"api.bot.qq.com", "sandbox.api.sgroup.qq.com"}


def load_env() -> dict[str, str]:
    cfg: dict[str, str] = {}
    for raw in ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip().strip("'\"")
    return cfg


def probe(label: str, base: str, appid: str, secret: str) -> None:
    url = f"{base.rstrip('/')}{TOKEN_PATH}"
    try:
        assert_public_http_url(url, require_https=True)
        assert_allowed_host(url, ALLOWED_HOSTS)
    except UnsafeUrlError as exc:
        print(f"⛔ {label}\n     URL 未通过安全校验: {exc}")
        return

    payload = json.dumps({"appId": appid, "clientSecret": secret}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = make_no_redirect_opener()
    try:
        with opener.open(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"❌ {label}\n     HTTP {exc.code}: {exc.read()[:200]!r}")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {label}\n     {type(exc).__name__}: {exc}")
        return

    ok = "access_token" in data
    print(f"{'✅' if ok else '❌'} {label}")
    print(f"     code={data.get('code')}  message={data.get('message')!r}")
    if ok:
        print(f"     ⭐ 拿到 token！expires_in={data.get('expires_in')}")


def main() -> int:
    cfg = load_env()
    app_id = cfg.get("QBOT_APP_ID", "")
    secret = cfg.get("QBOT_APP_SECRET", "")
    if not app_id or not secret:
        print("缺 QBOT_APP_ID / QBOT_APP_SECRET", file=sys.stderr)
        return 2

    print(f"用到的 AppID = {app_id}（{len(app_id)} 位）")
    print(f"用到的 secret 长度 = {len(secret)}")
    print(f"用到的 token 端点 = {TOKEN_PATH}")
    print()

    prod = "https://api.bot.qq.com"
    sandbox = "https://sandbox.api.sgroup.qq.com"

    print("=== 一：AppID 是否被平台认识（与虚构 AppID 对比）===")
    print("（只改 AppID、secret 固定为垃圾值，用错误码差异判断 AppID 是否存在）")
    probe("生产 + 虚构 AppID 0000000000 + 垃圾 secret", prod, "0000000000", GARBAGE_SECRET)
    probe("生产 + 虚构 AppID 1234567890 + 垃圾 secret", prod, "1234567890", GARBAGE_SECRET)
    probe("生产 + 你的 AppID 末位 +1 + 垃圾 secret", prod, app_id[:-1] + "2", GARBAGE_SECRET)
    probe("生产 + 你的 AppID 末位 -1 + 垃圾 secret", prod, app_id[:-1] + "0", GARBAGE_SECRET)
    probe("生产 + 你的 AppID + 垃圾 secret", prod, app_id, GARBAGE_SECRET)
    print()

    print("=== 二：沙箱域名是否接受这对凭据 ===")
    probe("沙箱 + 你的 AppID + 你的 secret", sandbox, app_id, secret)
    probe("沙箱 + 你的 AppID + 垃圾 secret", sandbox, app_id, GARBAGE_SECRET)
    print()

    print("=== 三：生产域名复现一次 ===")
    probe("生产 + 你的 AppID + 你的 secret", prod, app_id, secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
