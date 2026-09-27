"""媒体资源的小工具：SSRF 校验、流式转发、文本读取与内存缓存。

WebUI 通过 ``/media`` 代理远程图片/文件：QQ 的媒体链接大多带防盗链，直接
``<img src>`` / ``<a href>`` 会加载或下载失败。

安全说明：``/media`` 是一个「按用户给的 URL 去取内容」的接口，天然容易变成
SSRF 跳板（拿它去打内网、打云元数据接口 ``169.254.169.254``）。所以这里对
目标地址做了限制，默认只允许公网地址，见 :func:`_blocked_reason`。

小资源（图片、缩略图）走 :func:`fetch` 读进内存缓存；大文件走
:func:`stream`，边收边转发、支持 Range 断点续传，不把文件读进内存。
"""

from __future__ import annotations

import socket
import asyncio
import ipaddress
import mimetypes
from typing import Any
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from collections.abc import AsyncIterator

import httpx
from nonebot import logger

from .config import plugin_config as cfg

# 单个资源最大体积（超过则不内联）
MAX_BYTES = 6 * 1024 * 1024
# 缓存条目上限
MAX_ENTRIES = 512
# 跟随重定向的最大跳数（每一跳都要重新校验目标地址）
MAX_REDIRECTS = 3
# 单次流式读取的块大小
CHUNK_SIZE = 64 * 1024
# 文本预览最多读取的字节数（避免预览一个几百 MB 的日志把内存吃光）
MAX_TEXT_BYTES = 512 * 1024

# 允许内联预览的文本类型（MIME）
_TEXT_TYPES = frozenset(
    {
        'application/json',
        'application/xml',
        'application/javascript',
        'application/x-javascript',
        'application/x-yaml',
        'application/yaml',
        'application/x-sh',
        'application/sql',
        'application/x-python',
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        '.txt',
        '.md',
        '.markdown',
        '.log',
        '.json',
        '.jsonl',
        '.xml',
        '.yaml',
        '.yml',
        '.toml',
        '.ini',
        '.cfg',
        '.conf',
        '.csv',
        '.tsv',
        '.env',
        '.py',
        '.js',
        '.mjs',
        '.cjs',
        '.ts',
        '.tsx',
        '.jsx',
        '.java',
        '.kt',
        '.go',
        '.rs',
        '.c',
        '.h',
        '.cpp',
        '.hpp',
        '.cs',
        '.rb',
        '.php',
        '.sh',
        '.bash',
        '.zsh',
        '.fish',
        '.ps1',
        '.bat',
        '.cmd',
        '.lua',
        '.sql',
        '.html',
        '.htm',
        '.css',
        '.scss',
        '.less',
        '.vue',
        '.svelte',
        '.patch',
        '.diff',
        '.srt',
        '.vtt',
    }
)

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


class Blocked(Exception):
    """目标地址不允许访问（或跳转太多）"""


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(cfg.botui_api_timeout, connect=min(5.0, cfg.botui_api_timeout))


def _client(headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    """构造代理用的客户端。

    ``follow_redirects=False``：重定向要自己逐跳校验，不能让 httpx 直接跟
    ——否则公网地址 302 到内网就绕过了 :func:`_blocked_reason`。
    """
    return httpx.AsyncClient(
        timeout=_timeout(), follow_redirects=False, headers=headers
    )


async def _open(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    """发起请求并处理重定向，每一跳都重新校验目标地址。

    ``headers`` 会被 httpx **合并**到客户端的默认请求头上，所以调用方只需
    传 ``Range`` 这类逐次不同的头。
    """
    allow_private = bool(cfg.botui_media_allow_private)
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not allow_private:
            reason = await _blocked_reason(current)
            if reason:
                raise Blocked(f'拒绝代理媒体 {current[:80]}：{reason}')
        resp = await client.get(current, headers=headers)
        if resp.is_redirect and resp.headers.get('location'):
            current = urljoin(current, resp.headers['location'])
            continue
        resp.raise_for_status()
        return resp
    raise Blocked(f'重定向次数过多：{url[:64]}')


async def fetch(url: str) -> tuple[bytes, str] | None:
    """下载小资源（图片、缩略图）并缓存；失败或目标地址不允许时返回 None。"""
    if not url or not url.startswith(('http://', 'https://')):
        return None
    if url in _cache:
        return _cache[url]

    try:
        async with _client() as client:
            resp = await _open(client, url)
            data = resp.content
            if not data or len(data) > MAX_BYTES:
                logger.debug(f'Skip media {url[:64]}: size={len(data)}')
                return None
            ctype = (resp.headers.get('content-type') or '').split(';')[0].strip()
            if not ctype:
                ctype = mimetypes.guess_type(url)[0] or 'application/octet-stream'
            _remember(url, data, ctype)
            return data, ctype
    except Blocked as e:
        logger.warning(str(e))
        return None
    except Exception as e:
        logger.debug(f'Failed to fetch media {url[:64]}: {e}')
        return None


async def probe(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    """发起请求但立刻关闭 body，只留下响应头供调用方读取。

    流式转发需要先把 ``content-type`` / ``content-length`` / ``content-range``
    告诉浏览器，所以单独探一次头。返回的 response 已经关闭，只用它的
    ``status_code`` 与 ``headers``。
    """
    async with _client() as client:
        resp = await _open(client, url, headers=headers)
        await resp.aclose()
        return resp


async def stream(
    url: str, headers: dict[str, str] | None = None
) -> AsyncIterator[bytes]:
    """边下边转发远程资源，不在内存里完整驻留。

    ``headers`` 用于透传 ``Range``（音视频拖动进度条会用到）。目标地址不允许
    时抛 :class:`Blocked`，其他网络错误原样向上抛，由调用方决定怎么回给浏览器。
    """
    async with _client() as client:
        resp = await _open(client, url, headers=headers)
        async for chunk in resp.aiter_bytes(CHUNK_SIZE):
            yield chunk


class TooLarge(Exception):
    """下载的内容超过了允许的体积（只用来中断缓存下载）"""


async def download(
    url: str,
    dest: Path,
    *,
    max_bytes: int = 0,
    timeout: float | None = None,
) -> tuple[int, str]:
    """把远程资源流式写入本地文件，返回 ``(字节数, content-type)``。

    与 :func:`stream` 的区别是「边下边落盘」：预缓存媒体时不该先把整个文件读进
    内存，尤其是视频。超过 ``max_bytes`` 时中断下载并抛 :class:`TooLarge`
    （调用方负责删掉半截的临时文件）；目标地址不允许时抛 :class:`Blocked`。
    """
    dest = Path(dest)
    limit = int(max_bytes)
    client = (
        _client()
        if timeout is None
        else httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            follow_redirects=False,
        )
    )
    written = 0
    try:
        async with client:
            resp = await _open(client, url)
            ctype = (resp.headers.get('content-type') or '').split(';')[0].strip()
            declared = resp.headers.get('content-length')
            if limit > 0 and declared and declared.isdigit() and int(declared) > limit:
                raise TooLarge(f'资源过大（{int(declared)} 字节）')
            # 这里就地把下载内容写进本地文件：调用方（预缓存）本来就是后台任务，
            # 而每块只有 64 KB，比起「每块都切一次线程」的开销这样更划算。
            handle = dest.open('wb')  # noqa: ASYNC230
            try:
                async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                    written += len(chunk)
                    if limit > 0 and written > limit:
                        raise TooLarge(f'资源过大（>{limit} 字节）')
                    handle.write(chunk)
            finally:
                handle.close()
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if not ctype:
        ctype = mimetypes.guess_type(url)[0] or 'application/octet-stream'
    return written, ctype


def is_textual(content_type: str, name: str = '') -> bool:
    """判断一个资源是否适合当纯文本在线预览。"""
    ctype = (content_type or '').split(';')[0].strip().lower()
    if ctype.startswith('text/') or ctype in _TEXT_TYPES:
        return True
    # MIME 不可靠（很多文件源返回 application/octet-stream），按扩展名兜底
    target = name or ''
    try:
        target = urlsplit(target).path or target
    except ValueError:  # pragma: no cover - 防御性
        target = name or ''
    dot = target.rfind('.')
    suffix = target[dot:].lower() if dot >= 0 else ''
    return suffix in _TEXT_SUFFIXES


def decode_text(data: bytes) -> tuple[str, bool]:
    """把字节解码成文本，返回 ``(文本, 是否被截断)``。

    先看 BOM 定 UTF-16/32，再依次尝试 UTF-8 / GB18030：中文聊天里发来的 txt
    大多是这几种。不带 BOM 的 UTF-16 与 GB18030 无法可靠区分，这里优先按
    GB18030 解（UTF-16 文件一般带 BOM）。都不行就退回 UTF-8 + 替换字符。
    """
    truncated = len(data) > MAX_TEXT_BYTES
    head = data[:MAX_TEXT_BYTES]
    # 带 BOM 的 UTF-16/32：交给 utf-16 处理（它认得 BOM）
    if head[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try:
            return head.decode('utf-16'), truncated
        except UnicodeDecodeError:
            pass
    for encoding in ('utf-8', 'gb18030'):
        try:
            return head.decode(encoding), truncated
        except (UnicodeDecodeError, LookupError):
            continue
    return head.decode('utf-8', errors='replace'), truncated


async def fetch_text(url: str) -> tuple[str, bool, str, int] | None:
    """读取文本文件内容用于在线预览，返回 ``(文本, 被截断, 类型, 总字节数)``。

    用 ``Range: bytes=0-N`` 只取前 :data:`MAX_TEXT_BYTES` 个字节，避免预览
    一个几百 MB 的日志把内存和带宽吃光；服务端不支持 Range 时读完再截断。
    总字节数取自 ``Content-Range`` / ``Content-Length``，拿不到时为 0。
    """
    if not url or not url.startswith(('http://', 'https://')):
        return None
    range_header = {'Range': f'bytes=0-{MAX_TEXT_BYTES - 1}'}
    try:
        async with _client() as client:
            resp = await _open(client, url, headers=range_header)
            try:
                data = await resp.aread()
            finally:
                await resp.aclose()
    except Blocked as e:
        logger.warning(str(e))
        return None
    except Exception as e:
        logger.debug(f'Failed to read text {url[:64]}: {e}')
        return None
    if not data:
        return None
    ctype = (resp.headers.get('content-type') or '').split(';')[0].strip()
    text, truncated = decode_text(data)
    # 服务端忽略 Range 时也要截断，避免超长内容拖垮前端
    if len(data) >= MAX_TEXT_BYTES:
        truncated = True
    return text, truncated, ctype, _total_bytes(resp.headers)


def _total_bytes(headers: Any) -> int:
    """从响应头里推断资源总字节数（拿不到返回 0）。"""
    content_range = headers.get('content-range') or ''
    if '/' in content_range:
        tail = content_range.rsplit('/', 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    length = headers.get('content-length')
    return int(length) if length and length.isdigit() else 0


def avatar_of(avatar: Any) -> str:
    """从 uninfo 的 Scene/User 头像字段里取出可用的 url"""
    if not avatar:
        return ''
    if isinstance(avatar, str):
        return avatar
    if isinstance(avatar, dict):
        return str(avatar.get('url') or avatar.get('src') or '')
    return str(getattr(avatar, 'url', '') or '')
