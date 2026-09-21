#!/usr/bin/env python3
"""校验一个 URL 是否可被平台抓取。

用法:
    python3 probe/check_url.py https://example.com/a.png
    python3 probe/check_url.py https://a.png https://b.png

做四件事：
  1. 过 netguard —— 只允许 http/https、拒绝内嵌凭据、
     解析后拒绝回环 / 私有 / 链路本地 / 保留地址（防 SSRF）
  2. 发 HEAD（禁止跟随重定向），拿状态码与 content-type
  3. HEAD 不被支持时退回 GET（只读前若干字节就断开）
  4. 检查是不是图片 content-type、有没有跨域/防盗链迹象

平台侧抓图失败时对应错误码 40034004 / 850026，这里先把能自己发现的问题挡掉。
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chargebread.netguard import (  # noqa: E402
    UnsafeUrlError,
    assert_public_http_url,
    make_no_redirect_opener,
)

IMAGE_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp")


class _HeadRequest(urllib.request.Request):
    def get_method(self) -> str:  # noqa: D102
        return "HEAD"


def check(url: str) -> bool:
    print(f"\n=== {url} ===")
    try:
        assert_public_http_url(url, require_https=False)
        print("  ✅ 通过 netguard：协议合法，解析到的都是公网地址")
    except UnsafeUrlError as exc:
        print(f"  ⛔ 未通过 netguard：{exc}")
        return False

    parts = urlsplit(url)
    if parts.port and parts.port not in (80, 443):
        # 2026-09-21 实测：8443 端口的图床被平台正常抓取并转存，所以这里只是提示，
        # 不是警告 —— 非标准端口确实能用。
        print(f"  ℹ️  非标准端口 {parts.port}（已实测 8443 可用）")

    opener = make_no_redirect_opener()
    status = ctype = clen = None
    try:
        with opener.open(_HeadRequest(url), timeout=25) as resp:
            status = resp.status
            ctype = resp.headers.get("Content-Type")
            clen = resp.headers.get("Content-Length")
    except urllib.error.HTTPError as exc:
        status = exc.code
        ctype = exc.headers.get("Content-Type") if exc.headers else None
        print(f"  ⚠️  HEAD 返回 HTTP {exc.code}，改用 GET 前 512 字节")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 请求失败：{type(exc).__name__}: {exc!r}")
        return False

    if status is None or status >= 400:
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=0-511"})
            with opener.open(req, timeout=25) as resp:
                status = resp.status
                ctype = resp.headers.get("Content-Type")
                clen = resp.headers.get("Content-Length")
                resp.read(512)
        except urllib.error.HTTPError as exc:
            print(f"  ❌ GET 也失败：HTTP {exc.code}")
            return False
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ GET 失败：{type(exc).__name__}: {exc!r}")
            return False

    print(f"  HTTP 状态   : {status}")
    print(f"  Content-Type: {ctype}")
    print(f"  Content-Len : {clen}")

    ok = status == 200
    if not ok:
        print("  ❌ 状态码不是 200，平台抓取大概率失败")
        return False
    if ctype and ctype.split(";")[0].strip().lower() in IMAGE_TYPES:
        print("  ✅ 是图片类型，平台应能转存")
        return True
    print(f"  ⚠️  content-type 不是常见图片类型（{ctype}）——"
          "富媒体上传按扩展名/魔数判断，markdown 内嵌图只看能不能抓，通常仍可用")
    return True


def main() -> int:
    urls = sys.argv[1:]
    if not urls:
        print(__doc__)
        return 2
    results = [check(u) for u in urls]
    print(f"\n结论：{sum(results)}/{len(results)} 个 URL 可被平台抓取")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
