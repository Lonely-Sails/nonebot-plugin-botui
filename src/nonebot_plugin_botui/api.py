"""WebUI 的 HTTP 接口（httpx/FastAPI 路由）。"""

from __future__ import annotations

import os
import re
import json
import time
import shutil
import asyncio
import secrets
import tempfile
import ipaddress
from typing import TYPE_CHECKING, Any
from pathlib import Path
from dataclasses import replace
from urllib.parse import quote, urlsplit

import nonebot
from fastapi import Query, Request, APIRouter, WebSocket, HTTPException
from nonebot import logger
from nonebot.adapters import Bot
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.requests import HTTPConnection

from .config import plugin_config as cfg
from .models import (
    DIR_OUT,
    KIND_GROUP,
    ChatRecord,
    self_id_of,
    split_chat_key,
    describe_segments,
)
from .capture import resume_sent, suppress_sent, build_outgoing
from .segments import guess_mime, resolve_file_name
from .mediastore import MediaStore, url_of, safe_filename

if TYPE_CHECKING:
    from .store import MessageStore
    from .webui import WebUIServer
    from .mediastore import MediaRecord

VERSION = '0.1.0'

# WebSocket 空闲心跳间隔（秒）：两层作用，一是挡掉反向代理的空闲断连，
# 二是让服务端在下一次发送时发现已经断掉的连接。
WS_HEARTBEAT = 25.0

#: 一次发送最多带几个附件（多了适配器多半也发不出去，还会把请求撑得很大）
MAX_UPLOADS_PER_MESSAGE = 10

#: 清理媒体库时允许的模式（前端设置面板的按钮各对应一个）
MEDIA_CLEAR_MODES = frozenset({'all', 'orphans', 'image', 'file'})

router = APIRouter()

_store: 'MessageStore | None' = None
_server: 'WebUIServer | None' = None
_media: MediaStore | None = None
_token: str = ''
_last_send: dict[str, float] = {}
_static_dir = Path(__file__).parent / 'static'


def setup(
    store: 'MessageStore',
    server: 'WebUIServer',
    token: str,
    media: MediaStore | None = None,
) -> None:
    """注入依赖（由插件在启动时调用一次）。

    存储、事件服务、令牌都由这里一次性注入，而不是一半用模块变量、一半塞进
    ``app.state`` —— 两套机制混用会让「谁先谁后」变得难以推断。

    ``media`` 是媒体库（见 ``mediastore.py``）：机器人收发的图片/文件、WebUI
    上传的附件都放在一起。不传时按配置建一个默认实例，方便测试直接调用本模块。
    """
    global _store, _server, _token, _media
    _store = store
    _server = server
    _token = token
    if media is not None:
        _media = media
    elif _media is None:
        from .paths import BLOB_DIR

        _media = MediaStore(
            BLOB_DIR,
            max_bytes=cfg.botui_media_max_bytes if cfg.botui_media_enabled else 0,
            max_files=cfg.botui_media_max_files if cfg.botui_media_enabled else 0,
            ttl=cfg.botui_media_ttl,
            retention=cfg.botui_media_retention,
            file_max_bytes=cfg.botui_media_file_max_bytes,
        )


def auth_required() -> bool:
    return bool(cfg.botui_auth)


# ── 鉴权 ────────────────────────────────────────────────────────────────
#: 允许用 ``?token=`` 传令牌的**路由内相对路径**（挂载前缀由
#: :func:`_relative_path` 剥掉）。两类场景必须走 query：``<img>`` /
#: ``<a download>`` 这类浏览器自发请求带不上自定义头；WebSocket 的
#: ``new WebSocket(url)`` 也**无法设置请求头**。其余接口（含所有写接口）只认
#: ``X-BotUI-Token``，免得令牌被反向代理日志、浏览器历史或子资源请求带出去。
_QUERY_TOKEN_PATHS = frozenset(
    {'/media', '/api/media', '/api/file', '/api/preview', '/api/export', '/api/ws'}
)

#: 始终允许的 ``Host``（本机名，与来源限制配套）
_LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1', '::1', '0.0.0.0'})

#: 鉴权失败限速：同一来源在窗口内失败到这个次数后一律 429
_AUTH_FAIL_LIMIT = 10
_AUTH_WINDOW = 60.0

#: 内存里最多跟踪多少条一次性记录（鉴权失败窗口与发送限流各留一份额度）
_TRACK_LIMIT = 1024


class _AuthThrottle:
    """按来源记录鉴权失败时间戳的滑动窗口。

    同一来源在 :data:`_AUTH_WINDOW` 秒内失败到 :data:`_AUTH_FAIL_LIMIT` 次后，
    后续请求一律 429 —— 令牌熵足够高，这层只是不给「无成本反复猜」留余地。
    过期记录在读写时顺带清掉，字典本身再留一个条数上限。
    """

    def __init__(
        self,
        limit: int = _AUTH_FAIL_LIMIT,
        window: float = _AUTH_WINDOW,
        max_keys: int = _TRACK_LIMIT,
    ) -> None:
        self._limit = limit
        self._window = window
        self._max_keys = max_keys
        self._fails: dict[str, list[float]] = {}

    def exceeded(self, key: str) -> bool:
        """窗口内的失败次数是否已达上限。"""
        return len(self._fresh(key, time.time())) >= self._limit

    def record(self, key: str) -> None:
        """记一次失败。"""
        now = time.time()
        self._fails[key] = [*self._fresh(key, now), now]
        if len(self._fails) > self._max_keys:
            self._fails.pop(next(iter(self._fails)), None)

    def clear(self) -> None:
        self._fails.clear()

    def _fresh(self, key: str, now: float) -> list[float]:
        """窗口内的失败时间戳；顺手丢掉过期的（该来源没剩记录就移除键）。"""
        stamps = [t for t in self._fails.get(key, ()) if now - t < self._window]
        if stamps:
            self._fails[key] = stamps
        else:
            self._fails.pop(key, None)
        return stamps


_auth_throttle = _AuthThrottle()


def _client_host(conn: HTTPConnection) -> str:
    """请求来源地址；拿不到时返回 ``-``（既不匹配环回，也不会与真实地址撞车）。"""
    return (conn.client.host if conn.client else '') or '-'


def _relative_path(path: str) -> str:
    """剥掉挂载前缀，得到路由内路径（``/botui/api/x`` → ``/api/x``）。

    路由以 ``include_router(prefix=route)`` 挂载，``request.url.path`` 一定带着
    前缀；而地址白名单按路由内路径书写，比对前必须先剥掉。
    """
    route = str(cfg.botui_route or '').rstrip('/')
    return path[len(route) :] if route and path.startswith(route) else path


def _query_token_ok(path: str) -> bool:
    """该路径是否允许用 ``?token=`` 鉴权。"""
    relative = _relative_path(path)
    return any(
        relative == p or relative.startswith(f'{p}/') for p in _QUERY_TOKEN_PATHS
    )


def _token_ok(conn: HTTPConnection) -> bool:
    """校验令牌。

    前端只走 ``X-BotUI-Token`` 请求头；地址栏里的 ``?token=`` 与
    ``Authorization: Bearer`` 是为「curl / 直接点链接」留的便利，其中
    ``?token=`` 只对 :data:`_QUERY_TOKEN_PATHS` 里的只读路径放行（否则令牌容易
    被反向代理日志、浏览器历史或第三方子资源请求记录走）。

    参数类型是 ``HTTPConnection``：``Request`` 与 ``WebSocket`` 都是它的子类，
    校验逻辑（headers / query_params / client）两边完全一致。
    """
    if not cfg.botui_auth:
        return True
    if not _token:
        return False
    supplied = [
        conn.headers.get('x-botui-token', ''),
        conn.headers.get('authorization', '').removeprefix('Bearer ').strip(),
    ]
    if _query_token_ok(conn.url.path):
        supplied.append(conn.query_params.get('token', ''))
    return any(secrets.compare_digest(value, _token) for value in supplied if value)


def _is_loopback(host: str) -> bool:
    """判断来源地址是否为本机。

    用 ``ipaddress`` 而不是字符串比对：除了 ``127.0.0.1``，本机还可能以
    ``127.0.0.2``、IPv6 的 ``::1``，或 IPv4-mapped 形式 ``::ffff:127.0.0.1``
    出现（不同 ASGI 服务器给的写法不一样）。
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == 'localhost'
    # IPv4-mapped IPv6（::ffff:127.0.0.1）要先还原成 IPv4 再判断：
    # ipaddress 对 ``::ffff:7f00:1`` 的 is_loopback 是 False。
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _host_only(header: str) -> str:
    """从 ``Host`` 头里取出主机名（去掉端口与 IPv6 的方括号）。"""
    text = header.strip().lower()
    if text.startswith('['):  # [::1] 或 [::1]:8080
        return text[1:].split(']', 1)[0]
    return text.rsplit(':', 1)[0] if ':' in text else text


def _host_allowed(conn: HTTPConnection) -> bool:
    """校验请求的 ``Host`` 头（用于挡 DNS rebinding，**按需启用**）。

    ``BOTUI_ALLOWED_HOSTS`` 为空时**不校验** —— 否则用主机名访问本机
    （如 ``http://my-nas:8080``）都会被挡住，而默认开启令牌鉴权时 DNS
    rebinding 也拿不到数据。把它当纵深项：走域名 / 反向代理部署时，把域名写进
    ``BOTUI_ALLOWED_HOSTS`` 即启用白名单，本机主机名与环回 IP 始终允许。
    """
    if cfg.botui_allow_remote:
        return True
    allowed = {
        str(item).strip().strip('[]').lower() for item in cfg.botui_allowed_hosts
    }
    if not allowed:
        return True
    header = (conn.headers.get('host') or '').strip().lower()
    if not header:
        return True
    host = _host_only(header)
    return host in _LOCAL_HOSTS or _is_loopback(host) or host in allowed


def _client_allowed(conn: HTTPConnection) -> bool:
    """是否允许该来源访问。

    默认只允许本机；要开放给外部必须显式设置 ``BOTUI_ALLOW_REMOTE=true``。
    这里刻意**不**根据 ``botui_host`` 推断：那个配置只用来拼提示链接，
    如果顺带决定安全策略，把链接改成局域网地址就会意外放开访问。

    仅本机模式下，若配置了 ``BOTUI_ALLOWED_HOSTS`` 还会校验 ``Host``
    （见 :func:`_host_allowed`），用于挡 DNS rebinding。
    """
    if not _host_allowed(conn):
        return False
    if cfg.botui_allow_remote:
        return True
    return _is_loopback(_client_host(conn))


def _guard(request: Request) -> None:
    if not _client_allowed(request):
        raise HTTPException(
            status_code=403,
            detail=(
                'BotUI 仅允许本机访问（如确需远程访问，请设置 BOTUI_ALLOW_REMOTE=true）'
            ),
        )
    client = _client_host(request)
    if _auth_throttle.exceeded(client):
        raise HTTPException(status_code=429, detail='鉴权失败次数过多，请稍后再试')
    if not _token_ok(request):
        _auth_throttle.record(client)
        raise HTTPException(status_code=401, detail='BotUI 令牌无效或缺失')


def _guard_write(request: Request) -> None:
    _guard(request)
    if not cfg.botui_write_enabled:
        raise HTTPException(status_code=403, detail='BotUI 当前为只读模式')


# ── 机器人选择 ──────────────────────────────────────────────────────────
def _bots() -> list[Bot]:
    """当前已连接的机器人（NoneBot 内置）"""
    return list(nonebot.get_bots().values())


def _live_bot(self_id: str) -> Bot | None:
    """按 ID 取当前在线的机器人，没有就返回 None"""
    self_id = str(self_id or '')
    if not self_id:
        return None
    for bot in _bots():
        if str(bot.self_id) == self_id:
            return bot
    return None


def _pick_bot(chat: ChatRecord | None, bots: list[Bot] | None = None) -> Bot | None:
    """为一条会话挑选要使用的机器人。

    会话 key 里内嵌了机器人 ID（``12345:group_678``），所以按它精确匹配
    —— 这正是「会话按机器人隔离」的落点：给 A 机器人的群发消息，绝不能从
    B 机器人发出去。拿不到会话或该机器人已离线时返回 None。
    """
    items = bots if bots is not None else _bots()
    if not items or chat is None:
        return None
    # 会话所属机器人不在线：宁可失败也不要用别的机器人冒名发送
    wanted = chat.self_id or self_id_of(chat.key)
    if not wanted:
        return None
    for bot in items:
        if str(bot.self_id) == wanted:
            return bot
    return None


def _request_self_id(request: Request) -> str:
    """从请求里读取前端当前选中的机器人 ID（``?bot=`` 或请求头）"""
    supplied = request.query_params.get('bot', '') or request.headers.get(
        'x-botui-bot', ''
    )
    return str(supplied or '').strip()


def _make_target(chat: ChatRecord) -> Any:
    """把会话转成 alconna 的 ``Target``。

    ``scope`` 直接用 uninfo 存下来的字符串（如 ``'QQClient'``）：alconna 的
    ``SCOPES`` 就是以这些值为键的，原样传进去即可。
    """
    from nonebot_plugin_alconna.uniseg import Target

    kind, chat_id = split_chat_key(chat.key)
    if kind == KIND_GROUP:
        return Target(
            chat_id,
            adapter=chat.adapter or None,
            scope=chat.scope or None,
            self_id=chat.self_id or None,
        )
    return Target(
        chat_id,
        private=True,
        adapter=chat.adapter or None,
        scope=chat.scope or None,
        self_id=chat.self_id or None,
    )


def _require_store() -> 'MessageStore':
    if _store is None or not _store.ready:
        raise HTTPException(status_code=503, detail='BotUI 存储尚未就绪')
    return _store


# ── 页面 ────────────────────────────────────────────────────────────────
@router.get('', include_in_schema=False)
async def index_redirect() -> RedirectResponse:
    return RedirectResponse(url=f'{cfg.botui_route}/')


@router.get('/', include_in_schema=False)
async def index() -> FileResponse:
    page = _static_dir / 'index.html'
    if not page.is_file():  # pragma: no cover - 打包异常时
        raise HTTPException(status_code=500, detail='BotUI 前端资源缺失')
    return FileResponse(page, media_type='text/html; charset=utf-8')


@router.get('/favicon.ico', include_in_schema=False)
async def favicon() -> JSONResponse:
    # 不额外打包图标资源，交给前端直接返回 204
    return JSONResponse(status_code=204, content=None)


# ── 接口 ────────────────────────────────────────────────────────────────
@router.get('/api/meta')
async def get_meta(request: Request) -> dict[str, Any]:
    """前端启动时要读的元信息。

    这个接口不能直接 ``_guard``：前端在**还没有令牌**的时候就要靠它知道
    「要不要弹令牌框」（``auth_required``）。所以这里把返回值分成两段——
    引导用的字段谁都给，涉及环境细节的字段（数据库路径、机器人账号清单、
    消息总数）只在通过校验时才带上。
    """
    store = _require_store()
    bots = _bots()
    bot = _pick_bot(None, bots)
    data: dict[str, Any] = {
        'name': 'BotUI',
        'version': VERSION,
        'auth_required': auth_required(),
        'write_enabled': bool(cfg.botui_write_enabled),
        'page_size': int(cfg.botui_page_size),
        'route': cfg.botui_route,
        'capabilities': {
            'recall': bool(cfg.botui_allow_recall),
            'image': True,
            'at': True,
            'reply': True,
            'file': True,
            'preview': bool(cfg.botui_file_preview),
            'export': True,
            'forward': True,
            'upload': bool(cfg.botui_media_enabled and cfg.botui_write_enabled),
        },
        'file_preview': bool(cfg.botui_file_preview),
        'file_max_bytes': int(cfg.botui_file_max_bytes),
        'robots_tag': 'noindex, nofollow',
        'upload_enabled': bool(cfg.botui_media_enabled and cfg.botui_write_enabled),
        'upload_max_bytes': int(
            cfg.botui_media_file_max_bytes or cfg.botui_file_max_bytes
        ),
        'cache_enabled': bool(_media is not None and _media.enabled),
    }
    # 这些是环境细节：数据库在磁盘上的位置、机器人的 self_id 清单、消息总量。
    # 未通过校验时不返回，避免匿名请求就能摸清部署情况。
    if _client_allowed(request) and _token_ok(request):
        data.update(
            {
                'self_id': str(bot.self_id) if bot else None,
                'adapter': bot.adapter.get_name() if bot else None,
                'bots': [str(b.self_id) for b in bots],
                # 连接过的全部机器人（含离线），供右上角切换器列表
                'bot_list': [b.to_dict() for b in store.bots()],
                'db': str(store.path),
                'message_count': await store.count(),
            }
        )
    return data


@router.get('/api/bots')
async def get_bots(request: Request) -> dict[str, Any]:
    """连接过的机器人清单（含在线标记）。

    右上角的切换器用它列出所有机器人：「连接过」的概念存在 ``bots`` 表里，
    机器人断开后仍然保留（``online=false``），因此列表不会因为离线而缩水。
    """
    _guard(request)
    store = _require_store()
    records = store.bots()
    # 以 NoneBot 当前实际连接为准刷新在线状态：钩子可能因为异常漏掉一次事件，
    # 这里按 ``get_bots()`` 校正，避免界面显示「离线」但实际还连着。
    live = {str(b.self_id) for b in _bots()}
    for record in records:
        record.online = record.self_id in live
    return {'bots': [b.to_dict() for b in records]}


@router.get('/api/health')
async def get_health(request: Request) -> dict[str, Any]:
    """健康检查。

    和别的接口一样走 ``_guard``：不加的话它会在 ``BOTUI_ALLOW_REMOTE=false`` 下
    也能访问，泄露机器人 ``self_id`` 清单与在线状态。
    """
    _guard(request)
    store = _store
    return {
        'ok': bool(store and store.ready),
        'db': bool(store and store.ready),
        'bots': [str(b.self_id) for b in _bots()],
    }


@router.get('/api/chats')
async def get_chats(
    request: Request,
    q: str = Query('', max_length=100),
    limit: int = Query(200, ge=1, le=1000),
    bot: str = Query('', max_length=64),
) -> dict[str, Any]:
    """会话列表。

    ``bot`` 是要筛选的机器人 ID（切换器选中的那个）：会话按机器人完全隔离，
    只返回该机器人的会话。
    """
    _guard(request)
    store = _require_store()
    self_id = str(bot or _request_self_id(request) or '').strip()
    chats = store.chats(q, limit, self_id=self_id if self_id else None)
    return {'chats': [c.to_dict() for c in chats], 'bot': self_id or None}


@router.get('/api/messages')
async def get_messages(
    request: Request,
    chat: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(50, ge=1, le=500),
    before: float | None = Query(None),
    before_id: int | None = Query(None),
) -> dict[str, Any]:
    _guard(request)
    store = _require_store()
    if store.chat(chat) is None and '_' not in chat:
        raise HTTPException(status_code=400, detail='会话 key 格式不正确')
    items = await store.messages(chat, limit, before, before_id)
    return {'messages': [m.to_dict() for m in items], 'chat': chat}


@router.get('/api/members')
async def get_members(
    request: Request,
    chat: str = Query(..., min_length=1, max_length=200),
    q: str = Query('', max_length=100),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """会话里出现过的成员（WebUI 的 @ 菜单用）。

    名册来自实际记录过的消息：发送者、被 @ 的人，以及 @ 解析出的昵称。
    没有名册时前端会退化成「手动填 QQ 号」，所以这里不做 404，一律返回列表。
    """
    _guard(request)
    store = _require_store()
    items = store.members(chat, q, limit)
    return {'chat': chat, 'members': [m.to_dict() for m in items]}


#: 备注最大长度（按字符计）：太长会撑破界面，也没必要
MAX_ALIAS_LEN = 64


def _clean_alias(value: Any) -> str:
    """规整备注文本：去首尾空白、折行，并限制长度。

    备注是纯展示信息，不做任何转义——前端统一用 textContent 插入，天然防 XSS。
    """
    text = str(value or '').replace('\r', ' ').replace('\n', ' ').strip()
    return text[:MAX_ALIAS_LEN]


@router.post('/api/rename')
async def post_rename(request: Request) -> dict[str, Any]:
    """设置会话/成员/机器人的备注（别名）。

    有些适配器拿不到真实的群名或用户昵称（拿到的可能只是一串 ID，或者干脆
    是空的），界面上就只剩群号可看。备注让用户自己给它们起个认得出的名字，
    只影响展示，不写入聊天记录，也不会被适配器后续上报的名称覆盖。

    请求体：``{kind: 'chat'|'member'|'bot', alias: str, ...}``；
    ``chat``/``member`` 需要 ``chat``；``member`` 还需要 ``user_id``；
    ``bot`` 需要 ``self_id``。``alias`` 传空串表示清除备注。
    """
    _guard_write(request)
    store = _require_store()
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail='请求体不是合法 JSON') from e

    kind = str(payload.get('kind') or '').strip()
    alias = _clean_alias(payload.get('alias'))
    if kind == 'chat':
        key = str(payload.get('chat') or '').strip()
        if not key:
            raise HTTPException(status_code=400, detail='缺少 chat 参数')
        chat = await store.set_chat_alias(key, alias)
        if chat is None:
            raise HTTPException(status_code=404, detail='会话不存在')
        if _server is not None:
            _server.publish_chat(chat)
        return {
            'ok': True,
            'kind': kind,
            'alias': alias or None,
            'chat': chat.to_dict(),
        }
    if kind == 'member':
        key = str(payload.get('chat') or '').strip()
        user_id = str(payload.get('user_id') or '').strip()
        if not key or not user_id:
            raise HTTPException(status_code=400, detail='缺少 chat 或 user_id 参数')
        await store.set_member_alias(key, user_id, alias)
        member = store.member(key, user_id)
        return {
            'ok': True,
            'kind': kind,
            'alias': alias or None,
            'member': member.to_dict() if member is not None else None,
        }
    if kind == 'bot':
        self_id = str(payload.get('self_id') or '').strip()
        if not self_id:
            raise HTTPException(status_code=400, detail='缺少 self_id 参数')
        bot = await store.set_bot_alias(self_id, alias)
        if bot is None:
            raise HTTPException(status_code=404, detail='机器人不存在')
        if _server is not None:
            _server.publish_bot(bot)
        return {'ok': True, 'kind': kind, 'alias': alias or None, 'bot': bot.to_dict()}
    raise HTTPException(status_code=400, detail=f'未知的 kind：{kind or "(空)"}')


@router.get('/api/search')
async def get_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=100),
    chat: str = Query('', max_length=200),
    bot: str = Query('', max_length=64),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """按内容搜索消息。

    界面上搜索框本来就写着「搜索会话 / 群号 / 消息」，之前只做了会话名的本地
    过滤，消息正文其实搜不到；这里补上真正的全文检索。

    ``bot`` 限定只在某个机器人的消息里搜，避免切换机器人后搜出别的机器人的
    聊天记录。
    """
    _guard(request)
    store = _require_store()
    self_id = str(bot or _request_self_id(request) or '').strip()
    items = await store.search(
        q, limit, chat or None, self_id=self_id if self_id else None
    )
    return {'query': q, 'messages': [m.to_dict() for m in items]}


@router.websocket('/api/ws')
async def ws_events(websocket: WebSocket, since: int = Query(0, ge=0)) -> None:
    """事件长连接。

    连上后服务端先补发 ``since`` 之后（订阅之前）落下的那一段，再持续推送新
    事件。客户端断线重连时把「最后收到的事件序号」放进 ``since`` 即可，不需要
    轮询。

    ``since = 0`` 表示「刚打开页面」：历史消息由前端自己去 ``/api/messages``
    拉，这里不做补发，免得把记录重复推一遍。

    鉴权用**先 accept 再关闭**的写法：直接拒绝握手的话浏览器只能看到一个笼统的
    连接失败，拿不到原因；accept 之后用 4401/4403 关闭，前端能区分「令牌不对」
    和「不让远程访问」，也便于弹出令牌页。
    """
    await websocket.accept()

    if not _client_allowed(websocket):
        await websocket.close(code=4403, reason='BotUI 仅允许本机访问')
        return
    if not _token_ok(websocket):
        await websocket.close(code=4401, reason='BotUI 令牌无效或缺失')
        return
    if _server is None:  # pragma: no cover - 未挂载时不会走到这里
        await websocket.close(code=1011, reason='BotUI 尚未就绪')
        return

    queue, boundary = _server.subscribe()

    async def _pump() -> None:
        """推送循环：补发漏掉的、报一个游标、然后一直推新事件。"""
        for event in _server.backlog(since, boundary):
            await websocket.send_json(event)
        await websocket.send_json({'type': 'ready', 'cursor': _server.latest})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=WS_HEARTBEAT)
            except TimeoutError:
                # 心跳：既保活（挡掉中间代理的空闲断连），也顺便探一次连接
                event = {'type': 'ping', 'now': time.time()}
            await websocket.send_json(event)

    async def _reader() -> None:
        """读循环：只为感知断开。

        客户端不需要发任何东西；收到什么都忽略（except 留个 ``close`` 方便调试）。
        断开时 ``receive_text`` 抛 ``WebSocketDisconnect``，``_reader`` 结束，
        下面等两个任务谁先结束 —— 单靠推送侧发现断开要等到下一次发送，最长
        要等一个心跳周期，那期间订阅队列会一直挂在总线上。
        """
        try:
            while await websocket.receive_text() != 'close':
                pass
        except Exception:
            pass

    pump = asyncio.ensure_future(_pump())
    reader = asyncio.ensure_future(_reader())
    try:
        await asyncio.wait({pump, reader}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        _server.unsubscribe(queue)
        pump.cancel()
        reader.cancel()
        # 收尾时的异常（多半是「连接已断开」）不需要上报
        await asyncio.gather(pump, reader, return_exceptions=True)
        try:
            await websocket.close()
        except Exception:  # pragma: no cover - 已经断开
            pass


def _message_id_of(receipt: Any) -> str:
    """从 alconna 的发送回执里取出消息 ID（拿不到就返回空串）。

    不同适配器把 ID 放在不同位置：优先看 ``get_reply()``（跨平台那份），
    再退回 OneBot 风格的 ``msg_ids``。
    """
    try:
        replies = receipt.get_reply() or []
        if replies:
            return str(getattr(replies[0], 'id', '') or '')
    except Exception:  # pragma: no cover - 回执形状取决于适配器
        pass
    try:
        ids = getattr(receipt, 'msg_ids', None) or []
        if ids:
            first = ids[0]
            return str(first.get('message_id') if isinstance(first, dict) else first)
    except Exception:  # pragma: no cover - 同上
        pass
    return ''


async def _throttle(key: str) -> None:
    """同一会话两次发送之间至少间隔 ``BOTUI_SEND_INTERVAL`` 秒。

    间隔大到离谱时（配置写错，比如 3600）直接拒绝而不是干等：请求挂在那里
    不返回比报错更难排查。
    """
    interval = cfg.botui_send_interval
    if interval <= 0:
        return
    wait = interval - (time.time() - _last_send.get(key, 0.0))
    if wait > 0:
        if wait > 5:  # pragma: no cover - 配置异常时避免长时间挂起
            raise HTTPException(status_code=429, detail='发送过于频繁，请稍后再试')
        await asyncio.sleep(wait)
    if len(_last_send) >= _TRACK_LIMIT:
        _last_send.pop(next(iter(_last_send)), None)
    _last_send[key] = time.time()


# ── 附件上传（WebUI 里选图/选文件后以机器人身份发出）────────────────────
#: 取回 / 删除附件时允许的 id 形状：媒体库里是内容 md5，待发送的是临时 id
_ANY_ID = re.compile(r'^[A-Za-z0-9_-]{6,80}$')


async def _read_uploaded(request: Request, limit: int) -> tuple[bytes, str, str]:
    """从上传请求里取出 ``(字节, 文件名, 类型)``，并保证不超过 ``limit`` 字节。

    刻意**不**解析 multipart/form-data：那需要额外依赖 python-multipart，
    而前端完全不必装成表单 —— 直接把文件字节放在请求体里、把文件名与类型放进
    查询参数（``?name=`` / ``?type=``）即可。请求体就是一段裸字节流，读取、
    限制大小、测试都简单得多（curl 用 ``--data-binary @a.png`` 即可）。

    先看 ``Content-Length`` 再**分块**读取：绝不能 ``await request.body()``
    一把梭 —— 那会先把整个 body 缓冲进内存、超限判断就形同虚设，拿一个
    ``Content-Length: 4GB`` 的请求就能把进程内存打爆。超限时抛 413。
    """
    _raise_if_oversized(request.headers.get('content-length'), limit)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        _raise_if_oversized(total, limit)
        chunks.append(chunk)
    name = request.query_params.get('name', '')
    mime = request.query_params.get('type', '')
    return b''.join(chunks), name, mime


def _raise_if_oversized(size: int | str | None, limit: int) -> None:
    """体积超限时抛 413。``limit <= 0`` 视为不限制；``size`` 可以是
    ``Content-Length`` 或已读字节数（非数字则忽略）。"""
    if limit <= 0:
        return
    value = int(size) if isinstance(size, str) and size.isdigit() else size
    if isinstance(value, int) and value > limit:
        raise HTTPException(
            status_code=413, detail=f'文件过大（上限 {limit // 1048576} MB）'
        )


@router.post('/api/upload')
async def post_upload(request: Request) -> dict[str, Any]:
    """把待发送附件暂存进系统临时目录，返回一个可在 ``/api/send`` 里引用的 id。

    前端「选附件」这一步就调这里：附件先暂存，之后带上 ``uploads: [id]`` 发送。
    这么拆是因为发送前往往要展示预览（图片缩略图、文件名），而发送时才需要
    真实文件；一次性把文件塞进发送请求会让「预览」变得没有意义。

    刻意**不**在这一步入库：还没发送的东西不该占媒体库配额，也不该被内容去重
    （同一个文件改名重发是常事）。暂存用 :mod:`tempfile`（系统临时目录），
    前端点「移除」或超时未发送时直接删掉；直到 ``/api/send`` 那一刻才按内容
    md5 正式进媒体库（见 ``MediaStore.commit``）。
    """
    _guard_write(request)
    if not cfg.botui_media_enabled:
        raise HTTPException(status_code=403, detail='BotUI 未开启附件上传')
    if _media is None:  # pragma: no cover - setup 一定会注入
        raise HTTPException(status_code=503, detail='BotUI 媒体库尚未就绪')

    data, name, mime = await _read_uploaded(
        request, int(cfg.botui_media_file_max_bytes)
    )
    if not data:
        raise HTTPException(status_code=400, detail='没有收到文件内容')
    record = await _media.stage(data, name=name, mime=mime)
    if record is None:
        raise HTTPException(status_code=413, detail='附件过大或媒体库未启用')
    return {'ok': True, 'file': record.to_dict()}


@router.get('/api/media/{fid}')
async def get_media(
    request: Request,
    fid: str,
    download: int = Query(0, ge=0, le=1),
) -> Any:
    """按 id 取回资源（消息段里的地址就指向这里）。

    机器人收到的图片/文件、自己发出的附件都从这里取，因此前端只需要认识一个
    地址形态。id 既可能是媒体库里的内容 md5，也可能是**还没发送**的暂存附件
    （``staging``），两种都能取 —— 后者正是「选完附件立刻看到缩略图」所依赖的。
    """
    _guard(request)
    if _media is None:  # pragma: no cover - setup 一定会注入
        raise HTTPException(status_code=503, detail='BotUI 媒体库尚未就绪')
    if not fid or not _ANY_ID.match(fid):
        raise HTTPException(status_code=404, detail='媒体不存在')
    record = await _media.resolve(fid)
    if record is None:
        raise HTTPException(status_code=404, detail='媒体不存在或已被清理')
    return _media_response(record, download=bool(download))


@router.delete('/api/media/{fid}')
async def delete_media(request: Request, fid: str) -> dict[str, Any]:
    """删除一个资源（前端移除待发送附件时调用）。

    待发送的暂存附件直接删临时文件；已入库的媒体则连同磁盘文件一起删掉。
    """
    _guard_write(request)
    if _media is None:  # pragma: no cover - setup 一定会注入
        raise HTTPException(status_code=503, detail='BotUI 媒体库尚未就绪')
    if not fid or not _ANY_ID.match(fid):
        raise HTTPException(status_code=404, detail='媒体不存在')
    removed = await _media.delete(fid)
    return {'ok': True, 'removed': removed}


async def _send_path(record: 'MediaRecord') -> tuple[Path, Path | None]:
    """取出发送用的磁盘路径，返回 ``(正文路径, 临时副本或 None)``。

    QQ 适配器发本地文件时取的是**磁盘名**，而媒体库里的文件叫内容 md5、暂存
    附件叫随机临时名，直接发过去对方收到的是「未命名」。所以发送时按真实文件名
    **硬链接**一份临时副本（同一分区，不占额外磁盘），调用方发完即删。
    """
    path = record.path
    name = safe_filename(record.name)
    if not name or path.name == name:
        return path, None
    directory = Path(tempfile.mkdtemp(prefix='botui-send-'))
    target = directory / name
    try:
        await asyncio.to_thread(os.link, path, target)
    except OSError:
        try:
            await asyncio.to_thread(shutil.copyfile, path, target)
        except OSError:  # pragma: no cover - 磁盘异常，退回直接发原名
            shutil.rmtree(directory, ignore_errors=True)
            return path, None
    return target, directory


async def _cleanup_send(record: 'MediaRecord', directory: Path | None) -> None:
    """删掉 :func:`_send_path` 临时链接出来的那棵目录。"""
    if directory is None:
        return
    await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)


async def _upload_segment(
    record: 'MediaRecord',
) -> tuple[Any, dict[str, Any], Path | None]:
    """把一条媒体资源转成 ``(uniseg 段, 记录里的段字典, 发送临时目录)``。

    图片按 ``Image`` 发（聊天里直接显示），其余按 ``File`` 发；两者都用文件
    内容/路径而不是 URL：适配器把 ``Image(path=...)`` 转成 ``file://`` 只有
    当它在同一台机器上时才读得到，而 ``raw=``（适配器会转成 ``base64://``）
    在任何部署下都能用 —— 代价是传输体积涨三分之一，对附件这个量级可以接受。

    ``File`` 段的 exporter 只认 ``path``（不认 ``raw``），所以文件走路径；又因为
    exporter 取的是磁盘名，路径要用 :func:`_send_path` 链接出真实文件名的那份
    临时副本，发送结束后由调用方删掉（第三个返回值）。

    段里的 url 指向媒体库地址（内容 md5），前端带令牌就能取回，因此**发出的
    消息在记录里依然能显示图片**。请求发送那一刻附件已经 ``commit`` 入库，所以
    这里的地址与库里、与推给前端的完全一致。
    """
    from nonebot_plugin_alconna.uniseg import File, Image

    url = url_of(record.id)
    kind = record.kind
    if kind == 'image':
        data = await asyncio.to_thread(record.path.read_bytes)
        seg: Any = Image(raw=data, name=record.name, mimetype=record.mime or None)
        payload: dict[str, Any] = {
            'type': 'image',
            'url': url,
            'name': record.name,
            'mime': record.mime or None,
        }
        return seg, payload, None
    path, cleanup = await _send_path(record)
    seg = File(path=path, name=record.name, mimetype=record.mime or None)
    payload = {
        'type': 'file',
        'url': url,
        'name': record.name,
        'mime': record.mime or None,
        'size': record.size,
    }
    return seg, payload, cleanup


async def _resolve_upload(raw: Any) -> 'MediaRecord':
    """按 id 取回附件（媒体库或待发送暂存区），取不到时抛 404。"""
    uid = str(raw or '').strip()
    if _media is None:  # pragma: no cover - setup 一定会注入
        raise HTTPException(status_code=503, detail='BotUI 媒体库尚未就绪')
    record = await _media.resolve(uid)
    if record is None:
        raise HTTPException(status_code=404, detail=f'附件 {uid or "?"} 不存在或已过期')
    return record


async def _send_via_bot(
    bot: Bot,
    chat: ChatRecord,
    at_list,
    text: str,
    reply_to: str,
    extra: list[Any] | None = None,
):
    """真正把消息发出去，返回回执。

    ``extra`` 是夹在正文后面的附加消息段（图片 / 文件等），由调用方按发送顺序
    排好；正文（@、文本）在前，附件在后，与常见聊天软件的排版一致。
    """
    from nonebot_plugin_alconna.uniseg import At, Text, Reply, UniMessage

    outgoing: list[Any] = []
    if reply_to:
        outgoing.append(Reply(reply_to))
    outgoing.extend(At('user', target) for target in at_list)
    if at_list and text:
        outgoing.append(Text(' '))
    if text:
        outgoing.append(Text(text))
    if extra:
        outgoing.extend(extra)

    # 关闭采集再发送：下面会手工补一条记录（为了拿到行号返回给前端），
    # 不关闭的话采集钩子会重复记一条，界面上就会看到两条一样的消息。
    token = suppress_sent()
    try:
        return await UniMessage(outgoing).send(
            target=_make_target(chat), bot=bot, fallback=True
        )
    finally:
        resume_sent(token)


@router.post('/api/send')
async def post_send(request: Request) -> dict[str, Any]:
    _guard_write(request)
    store = _require_store()
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail='请求体不是合法 JSON') from e

    key = str(payload.get('chat') or '').strip()
    text = str(payload.get('text') or '')
    at_list = [str(x) for x in (payload.get('at') or []) if str(x).strip()]
    reply_to = str(payload.get('reply_to') or '').strip()
    upload_ids = [str(x) for x in (payload.get('uploads') or []) if str(x).strip()]

    if not key:
        raise HTTPException(status_code=400, detail='缺少 chat 参数')
    if not text.strip() and not at_list and not upload_ids:
        raise HTTPException(status_code=400, detail='消息内容为空')
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail='消息过长（最多 4000 字）')
    if len(upload_ids) > MAX_UPLOADS_PER_MESSAGE:
        raise HTTPException(
            status_code=400, detail=f'一次最多发送 {MAX_UPLOADS_PER_MESSAGE} 个附件'
        )
    if upload_ids and not cfg.botui_media_enabled:
        raise HTTPException(status_code=403, detail='BotUI 未开启附件上传')

    # 附件要在发送前取好：一是确认它们都还在（过期会 404），二是把 uniseg 段
    # 提前构造出来。发送时附件排在正文之后。取好后立刻 commit 进媒体库：按
    # 内容 md5 落盘，消息段里引用的就是这个稳定地址。
    extra: list[Any] = []
    upload_segments: list[dict[str, Any]] = []
    cleanup_dirs: list[Path | None] = []
    for uid in upload_ids:
        item = await _resolve_upload(uid)
        item = await _media.commit(item.id) if _media is not None else item
        if item is None:  # pragma: no cover - resolve 已确认存在
            raise HTTPException(status_code=404, detail=f'附件 {uid} 不存在或已过期')
        seg, seg_dict, cleanup = await _upload_segment(item)
        extra.append(seg)
        upload_segments.append(seg_dict)
        cleanup_dirs.append(cleanup)

    chat = store.chat(key)
    if chat is None:
        kind, chat_id = split_chat_key(key)
        if not chat_id:
            raise HTTPException(status_code=400, detail='会话 key 格式不正确')
        chat = ChatRecord(key=key, kind=kind, chat_id=chat_id, self_id=self_id_of(key))
    else:
        # store 返回的是热缓存里的活对象，直接改它会污染缓存；复制一份再改。
        chat = replace(chat)

    await _throttle(key)

    bot = _pick_bot(chat)
    if bot is None:
        # 会话指定了机器人但它当前不在线时，不要用别的机器人顶替——那会让
        # 「A 机器人的群」里冒出 B 机器人发的消息。
        raise HTTPException(
            status_code=503, detail='该会话所属的机器人当前不在线，无法发送'
        )

    try:
        receipt = await _send_via_bot(bot, chat, at_list, text, reply_to, extra)
    except Exception as e:
        logger.opt(exception=True).warning(f'BotUI failed to send message: {e}')
        raise HTTPException(status_code=502, detail='发送失败，请查看服务端日志') from e
    finally:
        # 发送用的临时文件名副本（见 _send_path）只在发送期间需要
        for directory in cleanup_dirs:
            if directory is not None:
                await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)

    # 主动补一条记录：即使采集钩子因故没生效，WebUI 里也能看到。
    # 段里用的就是媒体库地址，与库里、与推送出去的完全一致（不再有竞态回填）。
    record = build_outgoing(
        chat,
        await _payload_segments(
            at_list,
            text,
            reply_to,
            store=store,
            chat_key=chat.key,
            extra=upload_segments,
        ),
        text,
        self_id=str(bot.self_id),
        adapter=bot.adapter.get_name(),
    )
    record.message_id = _message_id_of(receipt)
    store.enqueue(record)
    # 落库后行号才可用；store.on_persist 会给附件补上指向这条记录的引用
    await store.flush()
    return {'ok': True, 'message': record.to_dict()}


async def _quoted_segment(store: 'MessageStore', message_id: str) -> dict[str, Any]:
    """给「回复」补一条带摘要的 reply 段。

    记录里只留一个 id 的话，界面刷新后引用块就退化成「回复 12345」了。
    这里按消息 ID 反查那条被回复的记录，把「谁: 什么内容」一起存下来。
    """
    seg: dict[str, Any] = {'type': 'reply', 'id': message_id}
    try:
        quoted = await store.message_by_message_id(message_id)
    except Exception:  # pragma: no cover - 反查失败不影响发送
        return seg
    if quoted is None:
        return seg
    who = quoted.user_name or quoted.user_id
    if who:
        seg['name'] = who
    summary = quoted.text.strip() or describe_segments(quoted.segments)
    if summary:
        seg['text'] = summary[:4000]
        seg['preview'] = summary[:120]
    return seg


async def _payload_segments(
    at_list: list[str],
    text: str,
    reply_to: str = '',
    store: 'MessageStore | None' = None,
    chat_key: str = '',
    extra: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把 WebUI 的发送内容转成用于记录的消息段

    ``extra`` 是附件（图片 / 文件）的段字典，顺序与下面 uniseg 的发送顺序一致：
    引用、@、正文，最后是附件。
    """
    segments: list[dict[str, Any]] = []
    if reply_to and store is not None:
        segments.append(await _quoted_segment(store, reply_to))
    for target in at_list:
        name = ''
        if store is not None and chat_key:
            # 名册里记过昵称/备注就带上，界面上显示「@小明」而不是「@10001」
            member = store.member(chat_key, target)
            if member is not None:
                label = member.alias or member.name
                if label and label != target:
                    name = label
        segments.append({'type': 'at', 'target': target, 'name': name or None})
    if at_list and text:
        segments.append({'type': 'text', 'text': ' '})
    if text:
        segments.append({'type': 'text', 'text': text})
    if extra:
        segments.extend(extra)
    return segments


@router.post('/api/recall')
async def post_recall(request: Request) -> dict[str, Any]:
    _guard_write(request)
    store = _require_store()
    if not cfg.botui_allow_recall:
        raise HTTPException(status_code=403, detail='BotUI 未开启撤回功能')
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail='请求体不是合法 JSON') from e

    row_id = payload.get('id')
    if row_id in (None, ''):
        raise HTTPException(status_code=400, detail='缺少 id 参数')
    try:
        record = await store.message_by_id(int(row_id))
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail='id 必须是整数') from e
    if record is None:
        raise HTTPException(status_code=404, detail='找不到这条消息')
    if record.direction != DIR_OUT:
        raise HTTPException(status_code=400, detail='只能撤回机器人自己发送的消息')
    if not record.message_id:
        raise HTTPException(
            status_code=400, detail='这条消息没有可用的消息 ID，无法撤回'
        )

    chat = store.chat(record.chat_key) or ChatRecord(
        key=record.chat_key,
        kind=record.chat_kind,
        chat_id=record.chat_id,
        self_id=record.self_id or self_id_of(record.chat_key),
    )
    bot = _pick_bot(chat)
    if bot is None:
        raise HTTPException(
            status_code=503, detail='该消息所属的机器人当前不在线，无法撤回'
        )

    try:
        # 从 .adapters 导入而不是 uniseg 顶层：顶层那行是普通 import（没有
        # `as` 再导出形式），类型检查会认为它不属于公开 API 而报警告
        from nonebot_plugin_alconna.uniseg.adapters import alter_get_exporter

        exporter = alter_get_exporter(bot.adapter.get_name())
        if exporter is None:
            raise RuntimeError(f'适配器 {bot.adapter.get_name()} 不支持撤回')
        await exporter.recall(record.message_id, bot, _make_target(chat))
    except Exception as e:
        logger.opt(exception=True).warning(f'BotUI failed to recall message: {e}')
        raise HTTPException(status_code=502, detail='撤回失败，请查看服务端日志') from e

    await store.mark_recalled(record.row_id)
    if _server is not None:
        _server.publish_recall(record.row_id)
    return {'ok': True, 'id': record.row_id}


# ── 页面资源 ────────────────────────────────────────────────────────────
@router.get('/static/{filename}')
async def get_static(filename: str) -> FileResponse:
    if not filename or '/' in filename or '\\' in filename or filename.startswith('.'):
        raise HTTPException(status_code=400, detail='非法的资源名')
    path = (_static_dir / filename).resolve()
    if _static_dir.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail='资源不存在')
    media = {
        '.css': 'text/css; charset=utf-8',
        '.js': 'application/javascript; charset=utf-8',
        '.html': 'text/html; charset=utf-8',
        '.svg': 'image/svg+xml',
        '.png': 'image/png',
        '.ico': 'image/x-icon',
    }.get(path.suffix.lower(), 'application/octet-stream')
    return FileResponse(path, media_type=media)


# ── 媒体代理 ────────────────────────────────────────────────────────────
@router.get('/media')
async def proxy_media(request: Request, u: str = Query(..., min_length=8)) -> Any:
    """代理远程图片：QQ 的图片链接大多带防盗链，直接 <img src> 会加载失败。

    这个接口是「按用户给的 URL 去取内容」，所以必须和别的数据接口一样过
    ``_guard``：否则任何人都能拿它当代理去访问内网（SSRF）。目标地址的
    限制在 ``media._blocked_reason`` 里。
    """
    from fastapi.responses import Response

    from .media import fetch

    _guard(request)
    parts = urlsplit(u)
    if parts.scheme not in ('http', 'https'):
        raise HTTPException(status_code=400, detail='仅支持 http/https 链接')
    # 本地媒体库优先：图片是最先失效的一类资源，命中就不再联网
    cached = await _local_media(u)
    if cached is not None:
        return _media_response(cached)
    result = await fetch(u)
    if result is None:
        # 拿不到就退回原链接，前端仍然显示为破图而不是报错
        return RedirectResponse(url=u, status_code=302)
    data, ctype = result
    return Response(
        content=data,
        media_type=ctype,
        headers={'Cache-Control': 'private, max-age=3600'},
    )


# ── 文件：下载 / 预览 / 合并转发 / 导出 ────────────────────────────────────
async def _resolve_media_url(store: 'MessageStore | None', raw: str) -> str:
    """把消息段里的文件地址解析成可直接访问的 http(s) 链接。

    有些适配器只给一个文件 ID（拿不到直链），那就用 ``message_by_message_id``
    反查那条记录，从它已经存下的段里找可用的 url。纯文件名则无从下手，
    返回空串由调用方报错。
    """
    url = str(raw or '').strip()
    if not url:
        return ''
    if url.startswith(('http://', 'https://')):
        return url
    if store is not None and len(url) <= 256:
        record = await store.message_by_message_id(url)
        if record is not None:
            for seg in record.segments:
                candidate = str(seg.get('url') or '').strip()
                if candidate.startswith(('http://', 'https://')):
                    return candidate
    return ''


async def _local_media(u: str) -> 'MediaRecord | None':
    """如果这个地址在媒体库里已有副本，返回对应的 :class:`MediaRecord`。

    有了它，媒体/文件就再也不用去碰那个随时会失效的原始链接 —— 这正是媒体库
    的收益所在。地址既可能是原始链接，也可能是媒体库地址本身（``/api/media``）。
    """
    if _media is None:
        return None
    text = str(u or '').strip()
    if not text:
        return None
    return await _media.lookup(text)


def _media_response(
    record: 'MediaRecord', *, download: bool = False, name: str = ''
) -> Any:
    """媒体库副本的响应（与 ``/api/file`` 的对外行为保持一致）。

    统一交给 ``FileResponse``：它自带 Range 支持（音视频拖进度条靠它），
    自己拼 ``StreamingResponse`` 反而要重写一遍 Range 解析。
    """
    filename = resolve_file_name(name or record.name, record.source, fallback='file')
    media = (
        record.mime
        or guess_mime(record.source or filename)
        or 'application/octet-stream'
    )
    return FileResponse(
        record.path,
        media_type=media,
        headers={
            'Cache-Control': 'private, max-age=600',
            # 响应头不能把「命中了媒体库」变成浏览器的缓存键，所以只做一个诊断标记
            'X-BotUI-Cache': 'hit' if not download else 'hit-download',
            'Content-Disposition': _content_disposition(
                'attachment' if download else 'inline', filename
            ),
        },
    )


async def _preview_media(record: 'MediaRecord', name: str) -> dict[str, Any]:
    """用媒体库副本做文本预览（不联网，因而不受链接失效影响）。"""
    from .media import MAX_TEXT_BYTES, is_textual, decode_text

    filename = resolve_file_name(name or record.name, record.source)
    if not is_textual(record.mime, filename):
        raise HTTPException(status_code=415, detail='该文件不是文本类型，无法在线预览')

    def _read() -> bytes:
        with record.path.open('rb') as handle:
            return handle.read(MAX_TEXT_BYTES)

    data = await asyncio.to_thread(_read)
    text, truncated = decode_text(data)
    if len(data) >= MAX_TEXT_BYTES:
        truncated = True
    return {
        'ok': True,
        'name': filename,
        'text': text,
        'truncated': truncated,
        'content_type': record.mime or None,
        'bytes': record.size,
        'url': record.source,
        'cached': True,
    }


def _content_disposition(disposition: str, filename: str) -> str:
    """构造 Content-Disposition，兼顾非 ASCII 文件名。"""
    safe = filename.replace('\\', '_').replace('/', '_').replace('"', '')
    ascii_name = safe.encode('ascii', 'ignore').decode('ascii').strip() or 'file'
    return (
        f'{disposition}; '
        f'filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(safe, safe='')}"
    )


def _upstream_filename(header: str | None) -> str:
    """从上游的 ``Content-Disposition`` 里取出文件名（优先 RFC 5987）。"""
    if not header:
        return ''
    text = str(header)
    idx = text.lower().find("filename*=utf-8''")
    if idx >= 0:
        tail = text[idx + len("filename*=utf-8''") :]
        return tail.split(';')[0].strip().strip('"')
    idx = text.lower().find('filename=')
    if idx >= 0:
        tail = text[idx + len('filename=') :]
        return tail.split(';')[0].strip().strip('"')
    return ''


# 逐跳透传的响应头（缓存类头自己定，避免把上游的私密策略拍给浏览器）
_PASSTHROUGH_HEADERS = (
    'content-length',
    'content-range',
    'accept-ranges',
    'etag',
    'last-modified',
)


# ── 媒体库：取回与管理 ──────────────────────────────────────────────────
@router.get('/api/cache')
async def get_cache(request: Request) -> dict[str, Any]:
    """媒体库概况（设置面板显示占用与命中数）。"""
    _guard(request)
    if _media is None:  # pragma: no cover - setup 一定会注入
        raise HTTPException(status_code=503, detail='BotUI 媒体库尚未就绪')
    return {'ok': True, 'cache': await _media.stats()}


@router.post('/api/cache')
async def post_cache(request: Request) -> dict[str, Any]:
    """媒体库维护：清理（按模式）与重置统计。

    清理是真的删文件，所以走 ``_guard_write`` —— 只读模式下不该动磁盘。
    """
    _guard_write(request)
    if _media is None:  # pragma: no cover - setup 一定会注入
        raise HTTPException(status_code=503, detail='BotUI 媒体库尚未就绪')
    try:
        payload = await request.json()
    except Exception:
        payload = None
    body = payload if isinstance(payload, dict) else {}

    action = str(body.get('action') or 'clear')
    if action == 'reset_stats':
        _media.reset_stats()
        return {'ok': True, 'cache': await _media.stats()}
    if action != 'clear':
        raise HTTPException(status_code=400, detail=f'不支持的操作：{action}')

    mode = str(body.get('mode') or 'all')
    if mode not in MEDIA_CLEAR_MODES:
        raise HTTPException(status_code=400, detail=f'不支持的清理方式：{mode}')
    result = await _media.clear(mode)
    return {
        'ok': True,
        'mode': mode,
        'removed': int(result.get('removed') or 0),
        'bytes': int(result.get('bytes') or 0),
        'cache': await _media.stats(),
    }


@router.get('/api/file')
async def get_file(
    request: Request,
    u: str = Query('', max_length=4096),
    name: str = Query('', max_length=300),
    chat: str = Query('', max_length=200),
    download: int = Query(0, ge=0, le=1),
) -> Any:
    """文件代理：在线预览与下载都走这里。

    直接用文件直链会踩两个坑：一是 QQ 的链接带防盗链会 403，二是很多适配器
    只给一个文件 ID 根本没直链。所以统一由服务端代取：

    - ``Range`` 原样透传，音视频可以拖进度条、大文件可以断点续传；
    - 体积超过 ``BOTUI_FILE_MAX_BYTES`` 时直接 413，不当无限流量通道；
    - 只做直通转发，不落盘。
    """
    from fastapi.responses import StreamingResponse

    from .media import Blocked, probe, stream

    _guard(request)
    store = _require_store()

    # 有本地媒体库副本就直接回本地文件：既能规避防盗链，也不再受链接过期影响
    cached = await _local_media(u)
    if cached is not None:
        return _media_response(cached, download=bool(download), name=name)

    url = await _resolve_media_url(store, u)
    if not url:
        raise HTTPException(status_code=400, detail='缺少可用的文件链接')

    range_header = request.headers.get('range') or ''
    upstream_headers = {'Range': range_header} if range_header else None
    try:
        head = await probe(url, headers=upstream_headers)
    except Blocked as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except Exception as e:
        logger.opt(exception=True).warning(f'BotUI failed to open file: {e}')
        raise HTTPException(status_code=502, detail='获取文件失败') from e

    max_bytes = int(cfg.botui_file_max_bytes)
    length = head.headers.get('content-length')
    if max_bytes > 0 and head.status_code == 200 and length:
        try:
            if int(length) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f'文件过大（{(int(length) / 1048576):.1f} MB，'
                        f'上限 {max_bytes // 1048576} MB）'
                    ),
                )
        except ValueError:  # pragma: no cover - 上游乱给 content-length
            pass

    ctype = (head.headers.get('content-type') or '').split(';')[0].strip()
    # QQ 这类直链的 content-type 常是 octet-stream；用链接里的后缀补一个更准的
    if not ctype or ctype == 'application/octet-stream':
        ctype = guess_mime(url) or ctype
    filename = resolve_file_name(
        name or _upstream_filename(head.headers.get('content-disposition')),
        url,
    )
    headers = {
        key: head.headers[key] for key in _PASSTHROUGH_HEADERS if key in head.headers
    }
    headers['Cache-Control'] = 'private, max-age=600'
    headers['Content-Disposition'] = _content_disposition(
        'attachment' if download else 'inline', filename
    )

    async def _body():
        try:
            async for chunk in stream(url, headers=upstream_headers):
                yield chunk
        except Blocked as e:  # pragma: no cover - 重定向后才发现也在上面拦
            logger.warning(f'BotUI file stream blocked: {e}')
        except Exception as e:
            # 响应头已经发出，只能中断传输（浏览器会提示下载失败）
            logger.debug(f'BotUI file stream aborted: {e}')

    return StreamingResponse(
        _body(),
        status_code=head.status_code,
        media_type=ctype or 'application/octet-stream',
        headers=headers,
    )


@router.get('/api/preview')
async def get_preview(
    request: Request,
    u: str = Query('', max_length=4096),
    name: str = Query('', max_length=300),
    chat: str = Query('', max_length=200),
) -> dict[str, Any]:
    """文本文件在线预览：只读前 512 KB，避免大文件把内存吃光。"""
    from .media import fetch_text, is_textual

    _guard(request)
    if not cfg.botui_file_preview:
        raise HTTPException(status_code=403, detail='BotUI 未开启文件在线预览')
    store = _require_store()

    # 本地缓存优先：读到本地副本就干脆不必联网
    cached = await _local_media(u)
    if cached is not None:
        return await _preview_media(cached, name)

    url = await _resolve_media_url(store, u)
    if not url:
        raise HTTPException(status_code=400, detail='缺少可用的文件链接')

    filename = resolve_file_name(name, url)
    result = await fetch_text(url)
    if result is None:
        raise HTTPException(status_code=502, detail='无法读取文件内容')
    text, truncated, ctype, total = result
    if not is_textual(ctype, filename):
        raise HTTPException(status_code=415, detail='该文件不是文本类型，无法在线预览')
    return {
        'ok': True,
        'name': filename,
        'text': text,
        'truncated': truncated,
        'content_type': ctype or None,
        'bytes': total,
        'url': url,
    }


@router.get('/api/forward')
async def get_forward(
    request: Request,
    id: str = Query(..., min_length=1, max_length=200),
    chat: str = Query('', max_length=200),
) -> dict[str, Any]:
    """展开合并转发。

    优先用记录里已内联的节点（部分适配器把内容直接放在了段里）；没有就调用
    适配器的 ``get_forward_msg``，失败时换成 ``get_forward_message`` 再试一次：
    不同适配器称呼不一样。
    """
    from .segments import parse_forward_nodes

    _guard(request)
    store = _require_store()

    inline = await store.forward_nodes(chat or None, id)
    if inline:
        return {'ok': True, 'id': id, 'source': 'record', 'nodes': inline}

    chat_record = store.chat(chat) if chat else None
    bot = _pick_bot(chat_record)
    if bot is None:
        raise HTTPException(
            status_code=404, detail='展开合并转发需要该会话的机器人在线'
        )

    timeout = float(cfg.botui_api_timeout)
    attempts = (
        ('get_forward_msg', {'id': id}),
        ('get_forward_msg', {'message_id': id}),
        ('get_forward_message', {'message_id': id}),
    )
    last_error: Exception | None = None
    for api_name, params in attempts:
        try:
            result = await asyncio.wait_for(
                bot.call_api(api_name, **params), timeout=timeout
            )
        except Exception as e:
            last_error = e
            continue
        nodes = parse_forward_nodes(result)
        if nodes:
            return {'ok': True, 'id': id, 'source': 'api', 'nodes': nodes}
    logger.debug(f'BotUI cannot expand forward {id}: {last_error!r}')
    adapter = bot.adapter.get_name()
    raise HTTPException(
        status_code=404,
        detail=f'无法展开这条合并转发（适配器 {adapter} 可能不支持）',
    )


@router.get('/api/export')
async def get_export(
    request: Request,
    chat: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20000, ge=1, le=200000),
) -> Any:
    """导出某个会话的聊天记录（JSON，含全部消息段）。"""
    from fastapi.responses import Response

    _guard(request)
    store = _require_store()
    record = store.chat(chat)
    kind, chat_id = split_chat_key(chat)

    messages: list[dict[str, Any]] = []
    after: float | None = None
    after_id: int | None = None
    while len(messages) < limit:
        batch = await store.stream_messages(
            chat, min(1000, limit - len(messages)), after, after_id
        )
        if not batch:
            break
        messages.extend(m.to_dict() for m in batch)
        after = batch[-1].ts
        after_id = batch[-1].row_id
        if len(batch) < 1000:
            break

    payload = {
        'name': 'BotUI',
        'version': VERSION,
        'exported_at': time.time(),
        'chat': record.to_dict()
        if record is not None
        else {
            'key': chat,
            'kind': kind,
            'id': chat_id,
            'name': chat_id,
        },
        'count': len(messages),
        'messages': messages,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    title = (record.name if record is not None else chat_id) or chat_id
    filename = f'{title}-{int(payload["exported_at"])}.json'
    return Response(
        content=text.encode('utf-8'),
        media_type='application/json; charset=utf-8',
        headers={
            'Content-Disposition': _content_disposition('attachment', filename),
            'Cache-Control': 'no-store',
        },
    )


__all__ = ['VERSION', 'auth_required', 'router', 'setup']
