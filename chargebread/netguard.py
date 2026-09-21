"""出站 URL 守卫。

服务端要拿外部 URL 发请求时一律先过这里。挡住三件事：
  1. 不在允许列表里的协议（file://、gopher:// 之类）
  2. 解析后落在回环 / 私有 / 链路本地 / 保留 / 组播 / 未指定地址上的目标
  3. 跟随重定向跳进内网（配合 make_no_redirect_opener）

注意 DNS rebinding：校验时解析一次、真正连接时可能解析到别的地址。这里的做法是
"校验即解析"，调用方应尽可能复用同一个已解析结果，或用短超时把窗口压小。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

HTTP_SCHEMES = ("http", "https")
WS_SCHEMES = ("ws", "wss")
SECURE_SCHEMES = ("https", "wss")
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


class UnsafeUrlError(ValueError):
    """URL 未通过安全校验。"""


def _assert_public(url: str, allowed_schemes: tuple[str, ...], require_secure: bool) -> str:
    parts = urlsplit(url)
    if parts.scheme not in allowed_schemes:
        raise UnsafeUrlError(f"只允许 {'/'.join(allowed_schemes)}，收到 {parts.scheme!r}")
    if require_secure and parts.scheme not in SECURE_SCHEMES:
        raise UnsafeUrlError(f"此处要求加密传输（https/wss），收到 {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("URL 缺少主机名")
    if parts.username or parts.password:
        raise UnsafeUrlError("URL 不得内嵌凭据")

    port = parts.port or DEFAULT_PORTS.get(parts.scheme, 443)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"无法解析主机 {host}: {exc}") from exc
    if not infos:
        raise UnsafeUrlError(f"主机 {host} 未解析出任何地址")

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeUrlError(f"拒绝非公网地址 {ip}（来自 {host}）")
    return url


def assert_public_http_url(url: str, *, require_https: bool = True) -> str:
    """校验 url 只指向公网 http/https 资源。"""
    return _assert_public(url, HTTP_SCHEMES, require_https)


def assert_public_ws_url(url: str, *, require_wss: bool = True) -> str:
    """校验 url 只指向公网 ws/wss 端点。网关地址来自接口响应，不能无条件信任。"""
    return _assert_public(url, WS_SCHEMES, require_wss)


def assert_allowed_host(url: str, allowed_hosts: set[str]) -> str:
    """在公网校验之外再套一层主机白名单。"""
    host = urlsplit(url).hostname or ""
    if host not in allowed_hosts:
        raise UnsafeUrlError(f"主机 {host!r} 不在白名单内 {sorted(allowed_hosts)}")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向，避免校验过的公网 URL 跳进内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url, code, f"拒绝跟随重定向到 {newurl}", headers, fp
        )


def make_no_redirect_opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(_NoRedirect)
    opener.addheaders = [("User-Agent", "chargebread/0.1")]
    return opener
