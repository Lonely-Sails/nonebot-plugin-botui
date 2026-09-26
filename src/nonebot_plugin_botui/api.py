"""WebUI 的 HTTP 接口（httpx/FastAPI 路由）。"""

from __future__ import annotations

import time
import asyncio
import secrets
import ipaddress
from typing import TYPE_CHECKING, Any
from pathlib import Path
from urllib.parse import urlsplit

import nonebot
from fastapi import Query, Request, APIRouter, WebSocket, HTTPException
from nonebot import logger
from nonebot.adapters import Bot
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.requests import HTTPConnection

from .config import plugin_config as cfg
from .models import DIR_OUT, KIND_GROUP, ChatRecord, split_chat_key, describe_segments
from .capture import resume_sent, suppress_sent, build_outgoing

if TYPE_CHECKING:
    from .store import MessageStore
    from .webui import WebUIServer

VERSION = '0.1.0'

# WebSocket 空闲心跳间隔（秒）：两层作用，一是挡掉反向代理的空闲断连，
# 二是让服务端在下一次发送时发现已经断掉的连接。
WS_HEARTBEAT = 25.0

router = APIRouter()

_store: 'MessageStore | None' = None
_server: 'WebUIServer | None' = None
_token: str = ''
_last_send: dict[str, float] = {}
_static_dir = Path(__file__).parent / 'static'


def setup(store: 'MessageStore', server: 'WebUIServer', token: str) -> None:
    """注入依赖（由插件在启动时调用一次）。

    存储、事件服务、令牌都由这里一次性注入，而不是一半用模块变量、一半塞进
    ``app.state`` —— 两套机制混用会让「谁先谁后」变得难以推断。
    """
    global _store, _server, _token
    _store = store
    _server = server
    _token = token


def auth_required() -> bool:
    return bool(cfg.botui_auth)


# ── 鉴权 ────────────────────────────────────────────────────────────────
def _token_ok(conn: HTTPConnection) -> bool:
    """校验令牌。

    前端只走 ``X-BotUI-Token`` 请求头；另外两种是为「curl / 直接点链接」留的：
    地址栏里带 ``?token=``，以及 ``Authorization: Bearer``。

    参数类型是 ``HTTPConnection``：``Request`` 与 ``WebSocket`` 都是它的子类，
    校验逻辑（headers / query_params / client）两边完全一致。
    """
    if not cfg.botui_auth:
        return True
    if not _token:
        return False
    supplied = (
        conn.headers.get('x-botui-token', ''),
        conn.headers.get('authorization', '').removeprefix('Bearer ').strip(),
        conn.query_params.get('token', ''),
    )
    return any(secrets.compare_digest(value, _token) for value in supplied)


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


def _client_allowed(conn: HTTPConnection) -> bool:
    """是否允许该来源访问。

    默认只允许本机；要开放给外部必须显式设置 ``BOTUI_ALLOW_REMOTE=true``。
    这里刻意**不**根据 ``botui_host`` 推断：那个配置只用来拼提示链接，
    如果顺带决定安全策略，把链接改成局域网地址就会意外放开访问。
    """
    if cfg.botui_allow_remote:
        return True
    client = (conn.client.host if conn.client else '') or ''
    return _is_loopback(client)


def _guard(request: Request) -> None:
    if not _client_allowed(request):
        raise HTTPException(
            status_code=403,
            detail=(
                'BotUI 仅允许本机访问（如确需远程访问，请设置 BOTUI_ALLOW_REMOTE=true）'
            ),
        )
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail='BotUI 令牌无效或缺失')


def _guard_write(request: Request) -> None:
    _guard(request)
    if not cfg.botui_write_enabled:
        raise HTTPException(status_code=403, detail='BotUI 当前为只读模式')


# ── 机器人选择 ──────────────────────────────────────────────────────────
def _bots() -> list[Bot]:
    """当前已连接的机器人（NoneBot 内置）"""
    return list(nonebot.get_bots().values())


def _pick_bot(chat: ChatRecord | None, bots: list[Bot] | None = None) -> Bot | None:
    items = bots if bots is not None else _bots()
    if not items:
        return None
    if chat is not None:
        if chat.self_id:
            for bot in items:
                if str(bot.self_id) == str(chat.self_id):
                    return bot
        if chat.adapter:
            for bot in items:
                if bot.adapter.get_name() == chat.adapter:
                    return bot
    return items[0]


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
        },
    }
    # 这些是环境细节：数据库在磁盘上的位置、机器人的 self_id 清单、消息总量。
    # 未通过校验时不返回，避免匿名请求就能摸清部署情况。
    if _client_allowed(request) and _token_ok(request):
        data.update(
            {
                'self_id': str(bot.self_id) if bot else None,
                'adapter': bot.adapter.get_name() if bot else None,
                'bots': [str(b.self_id) for b in bots],
                'db': str(store.path),
                'message_count': await store.count(),
            }
        )
    return data


@router.get('/api/health')
async def get_health() -> dict[str, Any]:
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
) -> dict[str, Any]:
    _guard(request)
    store = _require_store()
    chats = store.chats(q, limit)
    return {'chats': [c.to_dict() for c in chats]}


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
    if store.chat(chat) is None and not chat.count('_'):
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


@router.get('/api/search')
async def get_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=100),
    chat: str = Query('', max_length=200),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """按内容搜索消息。

    界面上搜索框本来就写着「搜索会话 / 群号 / 消息」，之前只做了会话名的本地
    过滤，消息正文其实搜不到；这里补上真正的全文检索。
    """
    _guard(request)
    store = _require_store()
    items = await store.search(q, limit, chat or None)
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
    _last_send[key] = time.time()


async def _send_via_bot(bot: Bot, chat: ChatRecord, at_list, text: str, reply_to: str):
    """真正把消息发出去，返回回执。"""
    from nonebot_plugin_alconna.uniseg import At, Text, Reply, UniMessage

    outgoing: list[Any] = []
    if reply_to:
        outgoing.append(Reply(reply_to))
    outgoing.extend(At('user', target) for target in at_list)
    if at_list and text:
        outgoing.append(Text(' '))
    if text:
        outgoing.append(Text(text))

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
        raise HTTPException(status_code=400, detail=f'请求体不是合法 JSON：{e}') from e

    key = str(payload.get('chat') or '').strip()
    text = str(payload.get('text') or '')
    at_list = [str(x) for x in (payload.get('at') or []) if str(x).strip()]
    reply_to = str(payload.get('reply_to') or '').strip()

    if not key:
        raise HTTPException(status_code=400, detail='缺少 chat 参数')
    if not text.strip() and not at_list:
        raise HTTPException(status_code=400, detail='消息内容为空')
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail='消息过长（最多 4000 字）')

    chat = store.chat(key)
    if chat is None:
        kind, chat_id = split_chat_key(key)
        if not chat_id:
            raise HTTPException(status_code=400, detail='会话 key 格式不正确')
        chat = ChatRecord(key=key, kind=kind, chat_id=chat_id)

    await _throttle(key)

    bot = _pick_bot(chat)
    if bot is None:
        raise HTTPException(status_code=503, detail='当前没有已连接的机器人，无法发送')

    try:
        receipt = await _send_via_bot(bot, chat, at_list, text, reply_to)
    except Exception as e:
        logger.opt(exception=True).warning(f'BotUI failed to send message: {e}')
        raise HTTPException(status_code=502, detail=f'发送失败：{e}') from e

    # 主动补一条记录：即使采集钩子因故没生效，WebUI 里也能看到
    record = build_outgoing(
        chat,
        await _payload_segments(
            at_list, text, reply_to, store=store, chat_key=chat.key
        ),
        text,
        self_id=str(bot.self_id),
        adapter=bot.adapter.get_name(),
    )
    record.message_id = _message_id_of(receipt)
    store.enqueue(record)
    # 等待落库：既保证下面的返回值带有真实行号，也让增量事件先推出去
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
) -> list[dict[str, Any]]:
    """把 WebUI 的发送内容转成用于记录的消息段"""
    segments: list[dict[str, Any]] = []
    if reply_to and store is not None:
        segments.append(await _quoted_segment(store, reply_to))
    for target in at_list:
        name = ''
        if store is not None and chat_key:
            # 名册里记过昵称就带上，界面上显示「@小明」而不是「@10001」
            member = store.member(chat_key, target)
            if member is not None and member.name and member.name != target:
                name = member.name
        segments.append({'type': 'at', 'target': target, 'name': name or None})
    if at_list and text:
        segments.append({'type': 'text', 'text': ' '})
    if text:
        segments.append({'type': 'text', 'text': text})
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
        raise HTTPException(status_code=400, detail=f'请求体不是合法 JSON：{e}') from e

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
        key=record.chat_key, kind=record.chat_kind, chat_id=record.chat_id
    )
    bot = _pick_bot(chat)
    if bot is None:
        raise HTTPException(status_code=503, detail='当前没有已连接的机器人，无法撤回')

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
        raise HTTPException(status_code=502, detail=f'撤回失败：{e}') from e

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
async def get_media(request: Request, u: str = Query(..., min_length=8)) -> Any:
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


__all__ = ['VERSION', 'auth_required', 'router', 'setup']
