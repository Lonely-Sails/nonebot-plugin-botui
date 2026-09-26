"""图片等媒体资源的小工具：下载并做内存缓存。

这里只做**内存**缓存：WebUI 通过 ``/media`` 代理远程图片，同一个链接通常
只被请求几次，没必要落盘（要落盘可以用 localstore 的
``get_plugin_cache_dir()``，目前用不上就不引入这个复杂度）。

安全说明：``/media`` 是一个「按用户给的 URL 去取内容」的接口，天然容易变成
SSRF 跳板（拿它去打内网、打云元数据接口 ``169.254.169.254``）。所以这里对
目标地址做了限制，默认只允许公网地址，见 :func:`_blocked_reason`。
"""

from __future__ import annotations

import socket
import asyncio
import ipaddress
import mimetypes
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from nonebot import logger

from .config import plugin_config as cfg

# 单个资源最大体积（超过则不内联）
MAX_BYTES = 6 * 1024 * 1024
# 缓存条目上限
MAX_ENTRIES = 512
# 跟随重定向的最大跳数（每一跳都要重新校验目标地址）
MAX_REDIRECTS = 3

_cache: dict[str, tuple[bytes, str]] = {}
_order: list[str] = []


def _remember(url: str, data: bytes, content_type: str) -> None:
    if url in _cache:
        return
    _cache[url] = (data, content_type)
    _order.append(url)
    while len(_order) > MAX_ENTRIES:
        _cache.pop(_order.pop(0), None)


def _ip_blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """地址是否属于「不该让这里去访问」的范围，返回原因（可访问则返回空串）。"""
    # IPv4-mapped IPv6（::ffff:127.0.0.1）要先还原成 IPv4 再判断，
    # 否则 is_global 会把它当成普通 IPv6 地址放过
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return '环回地址'
    if ip.is_link_local:
        return '链路本地地址（含云元数据接口）'
    if ip.is_private:
        return '内网地址'
    if ip.is_reserved:
        return '保留地址'
    if ip.is_multicast:
        return '组播地址'
    if ip.is_unspecified:
        return '未指定地址'
    if not ip.is_global:
        return '非公网地址'
    return ''


async def _blocked_reason(url: str) -> str:
    """检查 URL 的**目标地址**是否不允许访问，允许则返回空串。

    域名会先解析成 IP，并且**解析出的每一个**地址都要通过检查：只查第一个
    的话，攻击者可以用一个同时返回公网与内网地址的域名绕过。

    注意这不能完全防住 DNS rebinding（这里解析一次、httpx 连接时可能再解析
    一次）。对「只在本机使用的管理后台」来说这个强度够用；真要严格防住，
    需要把解析结果固定下来再直连 IP。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return '链接格式不合法'
    if parts.scheme not in ('http', 'https'):
        return '仅支持 http/https 链接'
    host = parts.hostname
    if not host:
        return '链接里没有主机名'

    # 字面量 IP 直接判断，不走 DNS
    try:
        return _ip_blocked_reason(ipaddress.ip_address(host))
    except ValueError:
        pass

    # 域名：解析出全部地址逐个判断
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parts.port or (443 if parts.scheme == 'https' else 80)
        )
    except (OSError, socket.gaierror) as e:
        return f'域名无法解析（{e}）'
    if not infos:
        return '域名无法解析'
    for info in infos:
        addr = info[4][0]
        try:
            reason = _ip_blocked_reason(ipaddress.ip_address(addr))
        except ValueError:
            continue
        if reason:
            return f'{host} 解析到{reason} {addr}'
    return ''


async def fetch(url: str) -> tuple[bytes, str] | None:
    """下载资源并缓存；失败或目标地址不允许时返回 None。"""
    if not url or not url.startswith(('http://', 'https://')):
        return None
    if url in _cache:
        return _cache[url]

    allow_private = bool(cfg.botui_media_allow_private)
    timeout = httpx.Timeout(
        cfg.botui_api_timeout, connect=min(5.0, cfg.botui_api_timeout)
    )
    try:
        # 自己处理重定向：每一跳都要重新校验，否则公网地址 302 到内网
        # 就能绕过上面的检查（httpx 的 follow_redirects 不给我们插手的余地）
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                if not allow_private:
                    reason = await _blocked_reason(current)
                    if reason:
                        logger.warning(f'拒绝代理媒体 {current[:80]}：{reason}')
                        return None
                resp = await client.get(current)
                if resp.is_redirect and resp.headers.get('location'):
                    current = urljoin(current, resp.headers['location'])
                    continue
                resp.raise_for_status()
                data = resp.content
                if not data or len(data) > MAX_BYTES:
                    logger.debug(f'Skip media {url[:64]}: size={len(data)}')
                    return None
                ctype = (resp.headers.get('content-type') or '').split(';')[0].strip()
                if not ctype:
                    ctype = mimetypes.guess_type(url)[0] or 'application/octet-stream'
                _remember(url, data, ctype)
                return data, ctype
            logger.debug(f'Too many redirects for media {url[:64]}')
            return None
    except Exception as e:
        logger.debug(f'Failed to fetch media {url[:64]}: {e}')
        return None


def avatar_of(avatar: Any) -> str:
    """从 uninfo 的 Scene/User 头像字段里取出可用的 url"""
    if not avatar:
        return ''
    if isinstance(avatar, str):
        return avatar
    if isinstance(avatar, dict):
        return str(avatar.get('url') or avatar.get('src') or '')
    return str(getattr(avatar, 'url', '') or '')
