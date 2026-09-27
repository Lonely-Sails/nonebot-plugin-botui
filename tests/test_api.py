"""WebUI 接口测试：鉴权、会话列表、消息、增量事件、发送、撤回、静态资源。

这里用 ``httpx.AsyncClient`` + ``ASGITransport`` 直接打挂载好的 ASGI 应用：
既不用真的监听端口，也能保证请求和存储写入跑在同一个事件循环里
（同步的 ``TestClient`` 会另开线程/事件循环，与 aiosqlite 连接冲突）。
"""

from __future__ import annotations

import json
import asyncio

import pytest
import pytest_asyncio
from httpx import AsyncClient

TOKEN = 'test-token'
HEADERS = {'X-BotUI-Token': TOKEN}
GROUP_ID = 87654321
USER_ID = 10001
BOT_ID = '12345678'


@pytest_asyncio.fixture
async def seeded(botui):
    """准备一条收到的消息和一条发出的消息。"""
    from src.nonebot_plugin_botui.models import (
        ChatRecord,
        MessageRecord,
        now_ts,
        chat_key,
    )
    from src.nonebot_plugin_botui.capture import build_outgoing

    store = botui._get_store()
    await store.reset()

    key = chat_key('group', str(GROUP_ID), BOT_ID)
    chat = ChatRecord(
        key=key, kind='group', chat_id=str(GROUP_ID), self_id=BOT_ID, name='测试群'
    )

    store.enqueue(
        MessageRecord(
            chat_key=key,
            chat_kind='group',
            chat_id=str(GROUP_ID),
            chat_name='测试群',
            direction='in',
            ts=now_ts(),
            user_id=str(USER_ID),
            user_name='小明',
            text='你好',
            segments=[{'type': 'text', 'text': '你好'}],
        )
    )
    outgoing = build_outgoing(
        chat,
        [{'type': 'text', 'text': '你好呀'}],
        '你好呀',
        self_id=BOT_ID,
        adapter='OneBot V11',
    )
    outgoing.message_id = '4242'
    store.enqueue(outgoing)
    await store.flush()
    return store, key


# ── 鉴权 ────────────────────────────────────────────────────────────────
async def test_chats_requires_token(client: AsyncClient):
    resp = await client.get('/botui/api/chats')
    assert resp.status_code == 401
    assert resp.json()['ok'] is False


async def test_chats_accepts_token_header(client: AsyncClient):
    resp = await client.get('/botui/api/chats', headers=HEADERS)
    assert resp.status_code == 200
    assert 'chats' in resp.json()


async def test_chats_accepts_token_query(client: AsyncClient):
    resp = await client.get(f'/botui/api/chats?token={TOKEN}')
    assert resp.status_code == 200


async def test_chats_accepts_bearer_token(client: AsyncClient):
    resp = await client.get(
        '/botui/api/chats', headers={'Authorization': f'Bearer {TOKEN}'}
    )
    assert resp.status_code == 200


async def test_wrong_token_is_rejected(client: AsyncClient):
    resp = await client.get('/botui/api/chats', headers={'X-BotUI-Token': 'nope'})
    assert resp.status_code == 401


# ── 页面与静态资源 ──────────────────────────────────────────────────────
async def test_page_is_served(client: AsyncClient):
    resp = await client.get('/botui/')
    assert resp.status_code == 200
    assert 'text/html' in resp.headers['content-type']
    assert 'botui' in resp.text.lower()


async def test_errors_use_botui_json_shape(client: AsyncClient, seeded):
    """BotUI 路由下的错误统一是 {"ok": false, "error": ...}，前端只认这个。

    这里同时盯住 Starlette 的异常处理器注册：处理器按 MRO 查找，
    注册在 fastapi.HTTPException 上会被 Starlette 自带的那份盖住。
    """
    _, key = seeded

    resp = await client.get('/botui/api/chats')  # 缺令牌
    assert resp.status_code == 401
    assert resp.json()['ok'] is False
    assert resp.json()['error']

    resp = await client.get('/botui/api/messages?chat=nope', headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()['ok'] is False

    resp = await client.get(f'/botui/api/messages?chat={key}&limit=0', headers=HEADERS)
    assert resp.status_code == 422  # FastAPI 校验错误
    assert resp.json()['ok'] is False


async def test_other_routes_keep_default_error_shape(client: AsyncClient):
    """BotUI 之外的 404 不应该被改写成 BotUI 的格式（别影响宿主项目）。"""
    resp = await client.get('/definitely-not-botui')
    assert resp.status_code == 404
    assert 'ok' not in resp.json()


async def test_index_redirects(client: AsyncClient):
    resp = await client.get('/botui')
    assert resp.status_code in (200, 307)
    if resp.status_code == 307:
        assert resp.headers['location'].endswith('/botui/')


async def test_static_assets_are_served(client: AsyncClient):
    for name, ctype in (('app.css', 'text/css'), ('app.js', 'javascript')):
        resp = await client.get(f'/botui/static/{name}')
        assert resp.status_code == 200, name
        assert ctype in resp.headers['content-type'], name


async def test_static_rejects_path_traversal(client: AsyncClient):
    for name in ('..%2F..%2Fpyproject.toml', '.env', 'nope.js'):
        resp = await client.get(f'/botui/static/{name}')
        assert resp.status_code in (400, 404), name


async def test_frontend_reads_page_size_from_meta():
    """前端的分页大小要听 /meta 的，不能写死。

    ``BOTUI_PAGE_SIZE`` 会被限制在 10~200；前端若固定 50，改这个配置就不会
    生效。这里直接查源码，确保写死的常量只作为兜底存在。
    """
    from pathlib import Path

    source = (
        Path(__file__).parent.parent
        / 'src'
        / 'nonebot_plugin_botui'
        / 'static'
        / 'app.js'
    ).read_text(encoding='utf-8')
    lines = source.splitlines()

    assert 'Number(meta.page_size)' in source, '前端应当从 meta.page_size 取值'
    # 兜底常量仍应存在，供 meta 缺失时使用
    assert 'PAGE_LIMIT_FALLBACK' in source

    # 拼请求的每一行都必须用运行时值，不能把兜底常量塞进 URL
    limit_lines = [ln for ln in lines if '&limit=' in ln]
    assert limit_lines, '没找到拼接 limit 的代码，测试需要更新'
    for line in limit_lines:
        assert 'PAGE_LIMIT_FALLBACK' not in line, f'分页大小写死了：{line.strip()}'

    # 「是否还有更早的消息」的判断也要用本次请求的值
    more_lines = [ln for ln in lines if 'state.hasMore = ' in ln and '>=' in ln]
    assert more_lines, '没找到 hasMore 的比较，测试需要更新'
    for line in more_lines:
        assert 'PAGE_LIMIT_FALLBACK' not in line, f'分页比较写死了：{line.strip()}'


async def test_favicon_is_empty(client: AsyncClient):
    assert (await client.get('/botui/favicon.ico')).status_code == 204


# ── meta / health ───────────────────────────────────────────────────────
async def test_meta(client: AsyncClient, seeded):
    resp = await client.get('/botui/api/meta', headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data['name'] == 'BotUI'
    assert data['auth_required'] is True
    assert isinstance(data['page_size'], int)
    assert data['page_size'] > 0
    assert data['capabilities']['recall'] is True
    assert data['message_count'] == 2


async def test_health(client: AsyncClient, seeded):
    data = (await client.get('/botui/api/health')).json()
    assert data['ok'] is True


# ── 会话与消息 ──────────────────────────────────────────────────────────
async def test_chats_list(client: AsyncClient, seeded):
    _, key = seeded
    data = (await client.get('/botui/api/chats', headers=HEADERS)).json()
    chat = next(c for c in data['chats'] if c['key'] == key)
    assert chat['name'] == '测试群'
    assert chat['kind'] == 'group'
    assert chat['last_text'] == '你好呀'
    assert chat['last_direction'] == 'out'
    assert chat['message_count'] == 2


async def test_chats_search(client: AsyncClient, seeded):
    data = (await client.get('/botui/api/chats?q=测试', headers=HEADERS)).json()
    assert data['chats']
    data = (await client.get('/botui/api/chats?q=不存在的群', headers=HEADERS)).json()
    assert data['chats'] == []


async def test_messages(client: AsyncClient, seeded):
    _, key = seeded
    data = (await client.get(f'/botui/api/messages?chat={key}', headers=HEADERS)).json()
    assert data['chat'] == key
    messages = data['messages']
    assert [m['text'] for m in messages] == ['你好', '你好呀']
    assert messages[0]['direction'] == 'in'
    assert messages[0]['recallable'] is False
    assert messages[1]['direction'] == 'out'
    assert messages[1]['self'] is True
    assert messages[1]['recallable'] is True
    assert messages[1]['message_id'] == '4242'


async def test_messages_limit(client: AsyncClient, seeded):
    _, key = seeded
    data = (
        await client.get(f'/botui/api/messages?chat={key}&limit=1', headers=HEADERS)
    ).json()
    assert [m['text'] for m in data['messages']] == ['你好呀']


async def test_messages_pagination(client: AsyncClient, seeded):
    """用 before_id 翻页不会重复返回已经看过的消息。"""
    _, key = seeded
    first = (
        await client.get(f'/botui/api/messages?chat={key}&limit=1', headers=HEADERS)
    ).json()['messages']
    newest = first[0]
    older = (
        await client.get(
            f'/botui/api/messages?chat={key}&limit=10'
            f'&before={newest["time"]}&before_id={newest["id"]}',
            headers=HEADERS,
        )
    ).json()['messages']
    assert [m['text'] for m in older] == ['你好']


async def test_messages_pagination_with_identical_timestamps(
    client: AsyncClient, botui, seeded
):
    """同一时刻写入的多条消息也要能一页页翻完，一条不漏、一条不重。

    这是 before_id 存在的理由：只按 ts 过滤的话，同一秒内的其他消息会被跳过。
    """
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts

    store, key = seeded
    same_ts = now_ts()
    for i in range(3):
        store.enqueue(
            MessageRecord(
                chat_key=key,
                chat_kind='group',
                chat_id=str(GROUP_ID),
                chat_name='测试群',
                direction='in',
                ts=same_ts,
                user_id=str(USER_ID),
                user_name='小明',
                text=f'同刻{i}',
                segments=[{'type': 'text', 'text': f'同刻{i}'}],
            )
        )
    await store.flush()

    seen: list[str] = []
    cursor: dict | None = None
    for _ in range(20):
        url = f'/botui/api/messages?chat={key}&limit=2'
        if cursor is not None:
            url += f'&before={cursor["time"]}&before_id={cursor["id"]}'
        page = (await client.get(url, headers=HEADERS)).json()['messages']
        if not page:
            break
        seen = [m['text'] for m in page] + seen
        cursor = page[0]

    # 每条消息恰好出现一次
    assert len(seen) == len(set(seen))
    for i in range(3):
        assert f'同刻{i}' in seen


async def test_messages_rejects_bad_chat(client: AsyncClient, seeded):
    resp = await client.get('/botui/api/messages?chat=nokey', headers=HEADERS)
    assert resp.status_code == 400


async def test_messages_unknown_chat_is_empty(client: AsyncClient, seeded):
    data = (
        await client.get('/botui/api/messages?chat=group_1', headers=HEADERS)
    ).json()
    assert data['messages'] == []


# ── 成员名册（@ 菜单） ──────────────────────────────────────────────────
async def test_members_requires_guard(client: AsyncClient):
    resp = await client.get(f'/botui/api/members?chat=group_{GROUP_ID}')
    assert resp.status_code == 401


async def test_members_lists_recorded_users(client: AsyncClient, seeded):
    """名册来自实际记录过的消息：发送者与被 @ 的人。"""
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts

    store, key = seeded
    store.enqueue(
        MessageRecord(
            chat_key=key,
            chat_kind='group',
            chat_id=str(GROUP_ID),
            direction='in',
            ts=now_ts(),
            user_id='10002',
            user_name='小红',
            text='@一下',
            segments=[{'type': 'at', 'target': '10003', 'name': '小刚'}],
        )
    )
    await store.flush()

    data = (await client.get(f'/botui/api/members?chat={key}', headers=HEADERS)).json()
    ids = {m['id'] for m in data['members']}
    assert {'10001', '10002', '10003'} <= ids
    names = {m['id']: m['name'] for m in data['members']}
    assert names['10003'] == '小刚'

    # 关键词过滤同时匹配昵称和 id
    filtered = (
        await client.get(f'/botui/api/members?chat={key}&q=小刚', headers=HEADERS)
    ).json()['members']
    assert [m['id'] for m in filtered] == ['10003']


async def test_members_empty_for_unknown_chat(client: AsyncClient, seeded):
    """没有名册时返回空列表（前端据此提示），不是 404。"""
    resp = await client.get('/botui/api/members?chat=group_999', headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()['members'] == []


async def test_send_records_reply_with_quoted_summary(
    client: AsyncClient, botui, seeded
):
    """WebUI 回复某条消息时，记录里要带上被引用的人与内容。

    否则刷新页面后引用块就只剩一个 id，看不出回复的是什么。
    """
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot, Adapter

    from src.nonebot_plugin_botui.models import MessageRecord, now_ts

    store, key = seeded
    # seeded 里那条收到的消息没有适配器消息 ID，这里补一条带 ID 的当回复目标
    store.enqueue(
        MessageRecord(
            chat_key=key,
            chat_kind='group',
            chat_id=str(GROUP_ID),
            direction='in',
            ts=now_ts(),
            user_id='10001',
            user_name='小明',
            text='这一条会被回复',
            segments=[{'type': 'text', 'text': '这一条会被回复'}],
            message_id='8888',
        )
    )
    await store.flush()

    adapter = nonebot.get_adapter(Adapter)
    bot = Bot(adapter, self_id=BOT_ID)

    async def fake_call_api(_bot, api, **data):
        return {'message_id': 9100, 'status': 'ok'}

    original = adapter._call_api
    adapter._call_api = fake_call_api  # type: ignore[method-assign]
    adapter.bot_connect(bot)
    try:
        resp = await client.post(
            '/botui/api/send',
            headers=HEADERS,
            json={'chat': key, 'text': '收到', 'reply_to': '8888'},
        )
        assert resp.status_code == 200
        segments = resp.json()['message']['segments']
    finally:
        adapter.bot_disconnect(bot)
        adapter._call_api = original

    reply = next(s for s in segments if s['type'] == 'reply')
    assert reply['id'] == '8888'
    assert reply['name'] == '小明'
    assert reply['preview'] == '这一条会被回复'


async def test_send_records_at_with_member_name(client: AsyncClient, botui, seeded):
    """发出去的 @ 要带上名册里的昵称，界面上显示「@小明」而不是「@10001」。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot, Adapter

    _, key = seeded

    async def fake_call_api(_bot, api, **data):
        return {'message_id': 9101, 'status': 'ok'}

    adapter = nonebot.get_adapter(Adapter)
    bot = Bot(adapter, self_id=BOT_ID)
    original = adapter._call_api
    adapter._call_api = fake_call_api  # type: ignore[method-assign]
    adapter.bot_connect(bot)
    try:
        resp = await client.post(
            '/botui/api/send',
            headers=HEADERS,
            json={'chat': key, 'text': '在吗', 'at': ['10001']},
        )
        assert resp.status_code == 200
        segments = resp.json()['message']['segments']
    finally:
        adapter.bot_disconnect(bot)
        adapter._call_api = original

    at = next(s for s in segments if s['type'] == 'at')
    assert at['target'] == '10001'
    # seeded 里那条收到的消息来自 10001/小明，名册里应该有名字
    assert at['name'] == '小明'


# ── 实时事件（WebSocket） ───────────────────────────────────────────────
def _ws_app(scope, receive, send):
    """取宿主驱动暴露的 ASGI 应用（WebUI 就挂在这上面）"""
    from nonebot import get_driver

    return get_driver().asgi(scope, receive, send)


class _WSClient:
    """直接驱动 ASGI WebSocket 的极简客户端。

    不用 ``starlette.testclient.TestClient``：它会另开线程与事件循环，而
    aiosqlite 连接绑定在当前会话的事件循环上（见 ``client`` fixture 的说明）。
    这里手工构造 websocket scope、用两个队列对接 receive/send，全程跑在同一个
    循环里，行为也更接近真实的 ASGI 服务器。
    """

    def __init__(self, app=None, query: str = ''):
        self._app = app
        self._query = query
        self._to_app: asyncio.Queue = asyncio.Queue()
        self._from_app: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def connect(self) -> dict:
        """发起握手并返回服务端的第一条消息（accept 或直接 close）"""
        scope = {
            'type': 'websocket',
            'asgi': {'version': '3.0', 'spec_version': '2.3'},
            'http_version': '1.1',
            'scheme': 'ws',
            'path': '/botui/api/ws',
            'raw_path': b'/botui/api/ws',
            'query_string': self._query.encode(),
            'root_path': '',
            'headers': [(b'host', b'botui.test')],
            'client': ('127.0.0.1', 54321),
            'server': ('botui.test', 80),
            'subprotocols': [],
        }
        await self._to_app.put({'type': 'websocket.connect'})
        self._task = asyncio.create_task(
            _ws_app(scope, self._to_app.get, self._from_app.put)
        )
        return await self.receive()

    async def receive(self, timeout: float = 3.0) -> dict:
        """取服务端的一条 ASGI 消息并归一化。

        ASGI 层只有 ``websocket.accept`` / ``websocket.send`` / ``websocket.close``
        三种，这里把 ``send`` 的 JSON 文本解析出来当事件本身返回，断言写起来
        就直接是「收到了一个 message 事件」。
        """
        raw = await asyncio.wait_for(self._from_app.get(), timeout)
        kind = raw['type']
        if kind == 'websocket.send':
            return json.loads(raw['text'])
        if kind == 'websocket.close':
            return {'type': 'close', 'code': raw.get('code')}
        return {'type': 'accept'}

    async def send_text(self, text: str) -> None:
        await self._to_app.put({'type': 'websocket.receive', 'text': text})

    async def close(self) -> None:
        """客户端正常断开，并等待服务端收尾"""
        await self._to_app.put({'type': 'websocket.disconnect', 'code': 1000})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except (TimeoutError, asyncio.TimeoutError):  # pragma: no cover
                self._task.cancel()
            self._task = None


async def test_ws_rejects_missing_token(client: AsyncClient, seeded):
    """握手先 accept 再关闭：这样浏览器能拿到 4401，识别为「令牌不对」"""
    ws = _WSClient(client._transport.app, 'since=0')
    first = await ws.connect()
    assert first['type'] == 'accept'
    closed = await ws.receive()
    assert closed['type'] == 'close'
    assert closed['code'] == 4401
    await ws.close()


async def test_ws_accepts_token_and_sends_ready(client: AsyncClient, seeded):
    ws = _WSClient(client._transport.app, f'since=0&token={TOKEN}')
    assert (await ws.connect())['type'] == 'accept'
    ready = await ws.receive()
    assert ready['type'] == 'ready'
    assert ready['cursor'] >= 0
    await ws.close()


async def test_ws_pushes_new_messages(client: AsyncClient, botui, seeded):
    """连上后不用轮询：新记录应当被主动推过来。"""
    from src.nonebot_plugin_botui.models import ChatRecord
    from src.nonebot_plugin_botui.capture import build_outgoing

    store, key = seeded
    ws = _WSClient(client._transport.app, f'since=0&token={TOKEN}')
    await ws.connect()
    await ws.receive()  # ready

    chat = ChatRecord(key=key, kind='group', chat_id=str(GROUP_ID), name='测试群')
    rec = build_outgoing(chat, [], '推送消息', self_id=BOT_ID, adapter='OneBot V11')
    store.enqueue(rec)
    await store.flush()

    event = await ws.receive()
    assert event['type'] == 'message'
    assert event['message']['text'] == '推送消息'
    assert event['chat']['key'] == key
    await ws.close()


async def test_ws_backfills_after_reconnect(client: AsyncClient, botui, seeded):
    """断线重连带上游标：订阅之前漏掉的事件要补发回来。"""
    from src.nonebot_plugin_botui.models import ChatRecord
    from src.nonebot_plugin_botui.capture import build_outgoing

    store, key = seeded
    probe = _WSClient(client._transport.app, f'since=0&token={TOKEN}')
    await probe.connect()
    cursor = (await probe.receive())['cursor']
    await probe.close()

    chat = ChatRecord(key=key, kind='group', chat_id=str(GROUP_ID), name='测试群')
    for text in ('离线一', '离线二'):
        store.enqueue(
            build_outgoing(chat, [], text, self_id=BOT_ID, adapter='OneBot V11')
        )
    await store.flush()

    ws = _WSClient(client._transport.app, f'since={cursor}&token={TOKEN}')
    await ws.connect()
    texts = []
    for _ in range(2):
        texts.append((await ws.receive())['message']['text'])
    assert texts == ['离线一', '离线二']
    ready = await ws.receive()
    assert ready['type'] == 'ready'
    assert ready['cursor'] > cursor
    await ws.close()


async def test_ws_since_zero_does_not_backfill(client: AsyncClient, seeded):
    """刚打开页面的客户端不该收到历史事件（记录它自己会去拉）。"""
    ws = _WSClient(client._transport.app, f'since=0&token={TOKEN}')
    await ws.connect()
    first = await ws.receive()
    assert first['type'] == 'ready'
    await ws.close()


async def test_ws_cross_thread_publish_is_delivered(client: AsyncClient, botui, seeded):
    """从别的线程发布事件也要能送到（队列不是线程安全的，总线要自己绕回循环）"""
    import threading

    _, key = seeded
    ws = _WSClient(client._transport.app, f'since=0&token={TOKEN}')
    await ws.connect()
    await ws.receive()  # ready

    from src.nonebot_plugin_botui.models import ChatRecord, MessageRecord, now_ts

    chat = ChatRecord(key=key, kind='group', chat_id=str(GROUP_ID), name='测试群')
    rec = MessageRecord(
        row_id=777,
        chat_key=key,
        chat_kind='group',
        chat_id=str(GROUP_ID),
        direction='in',
        ts=now_ts(),
        text='跨线程',
    )
    done = threading.Event()

    def worker() -> None:
        botui._get_server().publish_message(rec, chat)
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        event = await ws.receive()
        assert event['message']['text'] == '跨线程'
    finally:
        thread.join(timeout=3)
        await ws.close()


async def test_ws_ping_keeps_connection_alive(client: AsyncClient, seeded, monkeypatch):
    """心跳间隔到点就发 ping，顺便探活。"""
    from src.nonebot_plugin_botui import api

    monkeypatch.setattr(api, 'WS_HEARTBEAT', 0.05)
    ws = _WSClient(client._transport.app, f'since=0&token={TOKEN}')
    await ws.connect()
    await ws.receive()  # ready
    beat = await ws.receive()
    assert beat['type'] == 'ping'
    assert beat['now'] > 0
    # 再发一次 'close' 让读循环结束（覆盖 reader 分支）
    await ws.send_text('close')
    await ws.close()


async def test_ws_ignores_other_incoming_text(client: AsyncClient, seeded, monkeypatch):
    """客户端发来的无关内容被忽略，不影响推送。"""
    from src.nonebot_plugin_botui import api

    monkeypatch.setattr(api, 'WS_HEARTBEAT', 0.05)
    ws = _WSClient(client._transport.app, f'since=0&token={TOKEN}')
    await ws.connect()
    await ws.receive()  # ready
    await ws.send_text('hello')
    assert (await ws.receive())['type'] == 'ping'
    await ws.close()


async def test_ws_closed_when_not_ready(client: AsyncClient, seeded, monkeypatch):
    """存储/服务未就绪时用 1011 关掉连接，而不是挂在那里。"""
    from src.nonebot_plugin_botui import api

    monkeypatch.setattr(api, '_server', None)
    ws = _WSClient(client._transport.app, f'since=0&token={TOKEN}')
    assert (await ws.connect())['type'] == 'accept'
    closed = await ws.receive()
    assert closed['type'] == 'close'
    assert closed['code'] == 1011
    await ws.close()


async def test_events_http_endpoint_is_gone(client: AsyncClient, seeded):
    """轮询接口已下线，别再留着一个没人用的旧入口。"""
    resp = await client.get('/botui/api/events?since=0', headers=HEADERS)
    assert resp.status_code == 404


# ── 发送 ────────────────────────────────────────────────────────────────
async def test_send_without_bot(client: AsyncClient, seeded):
    """没有已连接的机器人时返回 503，而不是 500。"""
    _, key = seeded
    resp = await client.post(
        '/botui/api/send', headers=HEADERS, json={'chat': key, 'text': 'hi'}
    )
    assert resp.status_code == 503
    assert resp.json()['ok'] is False


async def test_send_does_not_duplicate_record(client: AsyncClient, botui, seeded):
    """WebUI 发消息只应该产生一条记录。

    采集钩子（Bot.on_calling_api）和 /api/send 自己补的那条很容易撞车，
    一旦撞车界面上就会出现两条一样的消息、message_count 也会多算。
    """
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot, Adapter

    store, key = seeded
    before = len(
        (await client.get(f'/botui/api/messages?chat={key}', headers=HEADERS)).json()[
            'messages'
        ]
    )

    adapter = nonebot.get_adapter(Adapter)
    bot = Bot(adapter, self_id=BOT_ID)

    async def fake_call_api(_bot, api, **data):
        return {'message_id': 9001, 'status': 'ok'}

    original = adapter._call_api
    # 测试替身：_call_api 是适配器内部方法，类型上是只读的，
    # 这里就是要打桩，所以显式忽略类型检查
    adapter._call_api = fake_call_api  # type: ignore[method-assign]
    adapter.bot_connect(bot)
    try:
        resp = await client.post(
            '/botui/api/send', headers=HEADERS, json={'chat': key, 'text': '只此一条'}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body['ok'] is True
        assert body['message']['message_id'] == '9001'
        assert body['message']['recallable'] is True
    finally:
        adapter.bot_disconnect(bot)
        adapter._call_api = original

    await store.flush()
    messages = (
        await client.get(f'/botui/api/messages?chat={key}', headers=HEADERS)
    ).json()['messages']
    assert len(messages) == before + 1
    assert [m['text'] for m in messages].count('只此一条') == 1

    chats = (await client.get('/botui/api/chats', headers=HEADERS)).json()['chats']
    chat = next(c for c in chats if c['key'] == key)
    assert chat['message_count'] == len(messages)


async def test_send_rejects_empty(client: AsyncClient, seeded):
    _, key = seeded
    resp = await client.post(
        '/botui/api/send', headers=HEADERS, json={'chat': key, 'text': ' '}
    )
    assert resp.status_code == 400


async def test_send_rejects_missing_chat(client: AsyncClient, seeded):
    resp = await client.post('/botui/api/send', headers=HEADERS, json={'text': 'hi'})
    assert resp.status_code == 400


async def test_send_rejects_bad_json(client: AsyncClient, seeded):
    resp = await client.post(
        '/botui/api/send',
        headers={**HEADERS, 'Content-Type': 'application/json'},
        content=b'{not json',
    )
    assert resp.status_code == 400


async def test_send_rejects_too_long(client: AsyncClient, seeded):
    _, key = seeded
    resp = await client.post(
        '/botui/api/send', headers=HEADERS, json={'chat': key, 'text': 'x' * 4001}
    )
    assert resp.status_code == 400


# ── 附件上传与发送 ──────────────────────────────────────────────────────
_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
    b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
)


def _connected_bot():
    """接一个假的 OneBot 机器人上来，返回 ``(adapter, bot, restore)``。

    ``_call_api`` 被换成记录型替身：既能返回消息 ID，也方便断言发出的消息段
    里确实带了图片 / 文件。
    """
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot, Adapter

    adapter = nonebot.get_adapter(Adapter)
    bot = Bot(adapter, self_id=BOT_ID)
    calls: list[tuple[str, dict]] = []

    async def fake_call_api(_bot, api, **data):
        calls.append((api, data))
        return {'message_id': 9500, 'status': 'ok'}

    original = adapter._call_api
    adapter._call_api = fake_call_api  # type: ignore[method-assign]
    adapter.bot_connect(bot)

    def restore() -> None:
        adapter.bot_disconnect(bot)
        adapter._call_api = original

    return bot, calls, restore


async def test_upload_requires_token(client: AsyncClient):
    resp = await client.post('/botui/api/upload?name=a.png', content=b'x')
    assert resp.status_code == 401
    assert resp.json()['ok'] is False


async def test_upload_rejects_empty_body(client: AsyncClient, seeded):
    resp = await client.post(
        '/botui/api/upload?name=a.png', headers=HEADERS, content=b''
    )
    assert resp.status_code == 400


async def test_upload_rejects_too_large(
    client: AsyncClient, seeded, monkeypatch: pytest.MonkeyPatch
):
    """超过 BOTUI_UPLOAD_MAX_BYTES 的附件在写盘前就被拦下。"""
    from src.nonebot_plugin_botui import api

    monkeypatch.setattr(api.cfg, 'botui_upload_max_bytes', 4)
    resp = await client.post(
        '/botui/api/upload?name=a.bin', headers=HEADERS, content=b'12345678'
    )
    assert resp.status_code == 413


async def test_upload_then_fetch_and_delete(client: AsyncClient, botui, seeded):
    """上传 → 取回 → 删除的完整链路。"""
    up = await client.post(
        '/botui/api/upload?name=%E5%9B%BE%E7%89%87.png&type=image/png',
        headers=HEADERS,
        content=_PNG,
    )
    assert up.status_code == 200
    info = up.json()['file']
    assert info['name'] == '图片.png'
    assert info['kind'] == 'image'
    assert info['size'] == len(_PNG)

    got = await client.get(f"/botui/api/upload?id={info['id']}", headers=HEADERS)
    assert got.status_code == 200
    assert got.content == _PNG
    assert got.headers['content-type'].startswith('image/png')

    # 鉴权同样是硬要求：图片是 <img src> 直接取的，只能靠 query
    assert (await client.get(f"/botui/api/upload?id={info['id']}")).status_code == 401

    rm = await client.request(
        'DELETE', f"/botui/api/upload?id={info['id']}", headers=HEADERS
    )
    assert rm.status_code == 200
    assert rm.json()['removed'] is True
    assert (
        await client.get(f"/botui/api/upload?id={info['id']}", headers=HEADERS)
    ).status_code == 404


async def test_upload_rejects_path_traversal(client: AsyncClient, seeded):
    """id 里带路径分隔符一律拒绝，避免变成读文件接口。"""
    for uid in ('../../etc/passwd', 'a/b', '..'):
        resp = await client.get(f'/botui/api/upload?id={uid}', headers=HEADERS)
        # FastAPI 的 Query(min_length=6) 会先挡掉太短的，其余由 uploads 校验拦截
        assert resp.status_code in (400, 404, 422), uid


async def test_send_image_attachment(client: AsyncClient, botui, seeded):
    """上传一张 PNG 并发出：请求里应当带上 image 段，记录里也带上。"""
    store, key = seeded
    up = await client.post(
        '/botui/api/upload?name=pic.png&type=image/png',
        headers=HEADERS,
        content=_PNG,
    )
    uid = up.json()['file']['id']

    _bot, calls, restore = _connected_bot()
    try:
        resp = await client.post(
            '/botui/api/send',
            headers=HEADERS,
            json={'chat': key, 'text': '看图', 'uploads': [uid]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body['message']['message_id'] == '9500'
    finally:
        restore()

    # 发出的 CQ 消息里应当包含 image 段（onebot11 exporter 会转成 base64://）
    api_name, payload = calls[-1]
    assert api_name == 'send_msg'
    message = str(payload['message'])
    assert '[CQ:image' in message
    assert 'base64://' in message

    image = next(s for s in body['message']['segments'] if s['type'] == 'image')
    assert image['name'] == 'pic.png'
    assert image['url'].startswith('/botui/api/upload?id=')
    await store.flush()


async def test_send_file_attachment(client: AsyncClient, botui, seeded):
    """非图片附件按 File 段发送：OneBot 走 upload_group_file。"""
    store, key = seeded
    up = await client.post(
        '/botui/api/upload?name=报告.txt&type=text/plain',
        headers=HEADERS,
        content='报告内容'.encode(),
    )
    assert up.status_code == 200
    uid = up.json()['file']['id']

    _bot, calls, restore = _connected_bot()
    try:
        resp = await client.post(
            '/botui/api/send',
            headers=HEADERS,
            json={'chat': key, 'uploads': [uid]},
        )
        assert resp.status_code == 200
        body = resp.json()
    finally:
        restore()

    api_name, payload = calls[-1]
    assert api_name == 'upload_group_file'
    assert payload['name'] == '报告.txt'
    assert payload['file'].endswith('blob')

    file_seg = next(s for s in body['message']['segments'] if s['type'] == 'file')
    assert file_seg['name'] == '报告.txt'
    assert file_seg['size'] == len('报告内容'.encode())
    await store.flush()


async def test_send_with_unknown_upload(client: AsyncClient, botui, seeded):
    """引用了不存在 / 已过期的附件时给出 404，而不是把空消息发出去。"""
    _, key = seeded
    resp = await client.post(
        '/botui/api/send',
        headers=HEADERS,
        json={'chat': key, 'text': 'hi', 'uploads': ['nonexistent-id']},
    )
    assert resp.status_code == 404


async def test_send_rejects_too_many_uploads(client: AsyncClient, botui, seeded):
    """一次发送的附件数量有上限。"""
    from src.nonebot_plugin_botui import api

    _, key = seeded
    resp = await client.post(
        '/botui/api/send',
        headers=HEADERS,
        json={
            'chat': key,
            'uploads': [f'id-{i}' for i in range(api.MAX_UPLOADS_PER_MESSAGE + 1)],
        },
    )
    assert resp.status_code == 400


async def test_send_rejects_uploads_when_disabled(
    client: AsyncClient, botui, seeded, monkeypatch: pytest.MonkeyPatch
):
    from src.nonebot_plugin_botui import api

    monkeypatch.setattr(api.cfg, 'botui_upload_enabled', False)
    resp = await client.post(
        '/botui/api/send',
        headers=HEADERS,
        json={'chat': seeded[1], 'uploads': ['whatever-id']},
    )
    assert resp.status_code == 403


async def test_meta_reports_upload_capability(client: AsyncClient, seeded):
    meta = (await client.get('/botui/api/meta', headers=HEADERS)).json()
    assert meta['upload_enabled'] is True
    assert meta['capabilities']['upload'] is True
    assert meta['upload_max_bytes'] > 0


async def test_cleanup_removes_expired_uploads(tmp_path):
    """过期附件由 cleanup 回收：没发出的按 ttl，发出的按 retention。"""
    import json
    import time

    from src.nonebot_plugin_botui.uploads import UploadStore

    store = UploadStore(tmp_path, ttl=3600.0, retention=3600.0)
    fresh = store.save(b'hello', 'a.txt')
    used = store.save(b'world', 'b.txt')
    store.mark_used(used.id)
    assert store.get(fresh.id) is not None

    # 把两条附件的「过期时刻」手动拨到过去，避免依赖真实等待时间
    past = time.time() - 10
    for uid in (fresh.id, used.id):
        meta_file = tmp_path / uid / 'meta.json'
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        meta['expires'] = past
        if meta.get('used'):
            # 已发出的按 used_at + retention 判断，这里把发出时间也推到过去
            meta['used_at'] = past - store.retention - 10
        meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')

    removed = store.cleanup()
    # 没发出的过期了，已发出的也过了 retention，两条都该清掉
    assert removed == 2
    assert store.get(fresh.id) is None
    assert store.get(used.id) is None


async def test_used_upload_survives_ttl(tmp_path):
    """已发出的附件不应按 ttl 过期：否则发出后一小时聊天记录里的图就坏了。"""
    import json
    import time

    from src.nonebot_plugin_botui.uploads import UploadStore

    store = UploadStore(tmp_path, ttl=3600.0, retention=99999.0)
    used = store.save(b'world', 'b.txt')
    store.mark_used(used.id)
    meta_file = tmp_path / used.id / 'meta.json'
    meta = json.loads(meta_file.read_text(encoding='utf-8'))
    meta['expires'] = time.time() - 10
    meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')

    assert store.get(used.id) is not None
    assert store.cleanup() == 0
    assert store.get(used.id) is not None


async def test_upload_filename_is_sanitized(tmp_path):
    """浏览器给的名字可能带路径，落盘时必须只保留基名。"""
    from src.nonebot_plugin_botui.uploads import UploadStore, safe_filename

    assert safe_filename('C:\\Users\\a\\图片.png') == '图片.png'
    assert safe_filename('../../etc/passwd') == 'passwd'
    assert safe_filename('') == 'file'

    store = UploadStore(tmp_path, ttl=3600)
    record = store.save(b'x', '../../evil.sh')
    assert record.name == 'evil.sh'
    assert record.path.parent.parent == tmp_path


# ── 撤回 ────────────────────────────────────────────────────────────────
async def test_recall_unknown_id(client: AsyncClient, seeded):
    resp = await client.post('/botui/api/recall', headers=HEADERS, json={'id': 999999})
    assert resp.status_code == 404


async def test_recall_rejects_bad_id(client: AsyncClient, seeded):
    resp = await client.post('/botui/api/recall', headers=HEADERS, json={'id': 'abc'})
    assert resp.status_code == 400


async def test_recall_rejects_incoming(client: AsyncClient, seeded):
    """只能撤回机器人自己发出的消息（行号 1 是收到的消息）。"""
    resp = await client.post('/botui/api/recall', headers=HEADERS, json={'id': 1})
    assert resp.status_code == 400
    assert '机器人' in resp.json()['error']


async def test_recall_without_bot(client: AsyncClient, seeded):
    """发出的消息（行号 2）在没有机器人的情况下返回 503。"""
    resp = await client.post('/botui/api/recall', headers=HEADERS, json={'id': 2})
    assert resp.status_code == 503


# ── 媒体代理 ────────────────────────────────────────────────────────────
async def test_media_requires_guard(client: AsyncClient):
    """这个接口会「按你给的 URL 去取内容」，必须和别的数据接口一样鉴权。

    曾经它没有 _guard，任何人都能拿它当代理读内网（SSRF）。
    """
    resp = await client.get('/botui/media?u=file:///etc/passwd')
    assert resp.status_code == 401


async def test_media_rejects_non_http(client: AsyncClient):
    resp = await client.get('/botui/media?u=file:///etc/passwd', headers=HEADERS)
    assert resp.status_code == 400


@pytest.mark.parametrize(
    'url',
    [
        # 云元数据接口：SSRF 最经典的目标
        'http://169.254.169.254/latest/meta-data/',
        # 环回（含 IPv4-mapped IPv6 的写法）
        'http://127.0.0.1:8080/secret',
        'http://127.0.0.2:8080/secret',
        'http://[::1]:8080/secret',
        'http://[::ffff:127.0.0.1]:8080/secret',
        # 内网
        'http://192.168.1.1/',
        'http://10.0.0.5/',
        'http://172.16.0.1/',
        # 未指定 / 保留
        'http://0.0.0.0/',
        # 本机名
        'http://localhost:8080/secret',
    ],
)
async def test_media_blocks_internal_targets(client: AsyncClient, url: str):
    """内网/环回目标一律不代理，退回原链接（302）而不是把内容取回来。"""
    resp = await client.get('/botui/media', params={'u': url}, headers=HEADERS)
    assert resp.status_code == 302, url
    assert resp.headers['location'] == url


async def test_media_blocked_reason_covers_aliases():
    """_blocked_reason 要把等价写法都拦住，别只看字面量。"""
    from src.nonebot_plugin_botui.media import _blocked_reason

    for bad in (
        'http://127.0.0.1/x',
        'http://[::1]/x',
        'http://[::ffff:127.0.0.1]/x',  # IPv4-mapped，必须还原成 IPv4 再判断
        'http://169.254.169.254/x',
        'http://10.1.2.3/x',
        'http://localhost/x',
        'file:///etc/passwd',
        'http:///nohost',
    ):
        assert await _blocked_reason(bad), bad

    # 公网地址放行（用 IP 避免测试依赖 DNS）
    assert await _blocked_reason('http://93.184.216.34/x') == ''


@pytest.mark.parametrize('path', ['/botui/api/meta', '/botui/api/health'])
async def test_endpoints_do_not_require_token(client: AsyncClient, path: str):
    """meta/health 不需要令牌：前端拿到令牌之前就要靠它们判断该不该弹令牌框。

    注意这两个接口对匿名请求只返回引导性字段，数据库路径、机器人清单这类
    环境细节要鉴权后才给（见 test_meta_hides_environment_details_without_token）。
    """
    assert (await client.get(path)).status_code == 200


# ── 来源限制（BOTUI_ALLOW_REMOTE） ──────────────────────────────────────
@pytest.mark.parametrize(
    ('host', 'allowed'),
    [
        ('127.0.0.1', True),
        # 本机可能以这些写法出现，别只认 127.0.0.1
        ('127.0.0.2', True),
        ('::1', True),
        ('::ffff:127.0.0.1', True),
        ('localhost', True),
        ('192.168.1.5', False),
        ('10.0.0.1', False),
        ('203.0.113.7', False),
        ('', False),
        (None, False),
    ],
)
async def test_client_allowed_only_loopback_by_default(botui, host, allowed):
    """默认只允许本机来源；局域网/公网地址一律拒绝。"""
    from types import SimpleNamespace

    request = SimpleNamespace(
        client=SimpleNamespace(host=host) if host else None,
    )
    assert botui.api._client_allowed(request) is allowed


async def test_allow_remote_opens_access(botui, monkeypatch):
    """BOTUI_ALLOW_REMOTE=true 时才放开非本机来源。"""
    from types import SimpleNamespace

    request = SimpleNamespace(client=SimpleNamespace(host='192.168.1.5'))

    cfg = botui.cfg
    assert cfg.botui_allow_remote is False
    assert botui.api._client_allowed(request) is False

    monkeypatch.setattr(cfg, 'botui_allow_remote', True, raising=False)
    assert botui.api._client_allowed(request) is True


async def test_changing_link_host_does_not_weaken_gate(botui, monkeypatch):
    """改 BOTUI_HOST 只影响链接显示，不该把来源限制一起关掉。

    这是拆分配置项的原因：以前 host 设成 '' / '0.0.0.0' / '*' 会顺带
    变成「不限制来源」，把提示链接改成局域网地址又会误伤访问。
    """
    from types import SimpleNamespace

    cfg = botui.cfg
    request = SimpleNamespace(client=SimpleNamespace(host='192.168.1.5'))

    for host in ('127.0.0.1', '192.168.1.5', '', '0.0.0.0', '*'):
        monkeypatch.setattr(cfg, 'botui_host', host, raising=False)
        assert botui.api._client_allowed(request) is False, host


async def test_meta_hides_environment_details_without_token(client: AsyncClient):
    """未通过校验时不返回数据库路径、机器人清单、消息总数。

    这些是部署细节，匿名请求不该拿到；但 ``auth_required`` 这类引导字段必须
    照常返回，否则前端没法知道要不要弹令牌框。
    """
    resp = await client.get('/botui/api/meta')
    assert resp.status_code == 200
    data = resp.json()

    # 引导字段要在
    assert data['auth_required'] is True
    assert isinstance(data['page_size'], int)
    assert 'capabilities' in data

    # 环境细节不能有
    for leaked in ('db', 'bots', 'self_id', 'adapter', 'message_count'):
        assert leaked not in data, f'未鉴权时不应返回 {leaked}'


async def test_meta_includes_environment_details_with_token(
    client: AsyncClient, seeded
):
    """带上令牌就照常返回完整信息（界面要显示机器人信息与数据库路径）。"""
    data = (await client.get('/botui/api/meta', headers=HEADERS)).json()
    assert 'db' in data
    assert 'bots' in data
    assert isinstance(data['message_count'], int)


# ── 消息搜索 ────────────────────────────────────────────────────────────
async def test_search_requires_token(client: AsyncClient):
    resp = await client.get('/botui/api/search', params={'q': '你好'})
    assert resp.status_code == 401


async def test_search_rejects_empty_query(client: AsyncClient, seeded):
    """空关键词要被参数校验挡住，不能退化成「列出全部消息」。"""
    resp = await client.get('/botui/api/search', params={'q': ''}, headers=HEADERS)
    assert resp.status_code == 422


async def test_search_finds_message_body(client: AsyncClient, seeded):
    """搜索框写着「搜索消息」，正文必须真的能搜到。"""
    resp = await client.get('/botui/api/search', params={'q': '你好'}, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data['query'] == '你好'
    hits = data['messages']
    assert hits, '应当搜到「你好」和「你好呀」'
    assert all('你好' in m['text'] for m in hits)


async def test_search_returns_empty_for_no_match(client: AsyncClient, seeded):
    resp = await client.get(
        '/botui/api/search', params={'q': '绝对不存在的词'}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()['messages'] == []


async def test_search_escapes_like_wildcards(client: AsyncClient, botui, seeded):
    """``%`` 是 LIKE 通配符，不转义的话搜一个 ``%`` 会返回全部消息。"""
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts

    store, key = seeded
    # 用只有本用例会命中的关键词；时间戳取「刚刚」而不是未来时间——
    # 这个 store 是整个测试会话共用的，写未来时间戳会让后面用例里
    # 「最后一条消息」的断言看到这一条。
    marker = '通配符转义回归用例'
    total = await store.count()
    store.enqueue(
        MessageRecord(
            chat_key=key,
            chat_kind='group',
            chat_id=str(GROUP_ID),
            chat_name='测试群',
            direction='in',
            ts=now_ts(),
            user_id=str(USER_ID),
            user_name='小明',
            text=f'{marker} 进度 100% 了',
            segments=[{'type': 'text', 'text': f'{marker} 进度 100% 了'}],
        )
    )
    await store.flush()

    try:
        resp = await client.get('/botui/api/search', params={'q': '%'}, headers=HEADERS)
        hits = resp.json()['messages']
        assert len(hits) == 1, f'通配符没转义，返回了 {len(hits)} 条（总共 {total} 条）'
        assert marker in hits[0]['text']
    finally:
        # 这个 store 是整个测试会话共用的，写入的记录必须清掉，
        # 否则后面用例里「最后一条消息」的断言会看到这一条。
        await store.reset()


def test_make_target_accepts_uninfo_scope_verbatim(botui):
    """会话里存的 scope 必须能直接喂给 alconna 的 ``Target``。

    uninfo 的 ``session.scope`` 存进数据库是 ``str`` 形式（``'QQClient'``），
    而 alconna 的 ``SCOPES`` 正好以这些字符串为键。曾经这里多套了一层
    「按成员**名**查 ``__members__``」的转换，可成员名是 ``qq_client`` 而不是
    ``'QQClient'``，于是永远查不到、原样返回 —— 一个没人察觉的空转。
    """
    from src.nonebot_plugin_botui.api import _make_target
    from src.nonebot_plugin_botui.models import ChatRecord

    chat = ChatRecord(
        key='999:group_123',
        kind='group',
        chat_id='123',
        scope='QQClient',
        adapter='OneBot V11',
        self_id='999',
    )
    target = _make_target(chat)
    # selector 被设上就说明 scope 被 alconna 认出来了（认不出会抛 KeyError）
    assert target.selector is not None
    assert target.scope == 'QQClient'

    private = _make_target(
        ChatRecord(key='999:private_7', kind='private', chat_id='7', scope='Telegram')
    )
    assert private.private is True
    assert private.scope == 'Telegram'


# ── 多机器人：切换、在线标记与会话隔离 ─────────────────────────────────
BOT_B_ID = '876543210'


async def test_bots_endpoint_lists_connected_bots(client: AsyncClient, seeded):
    """切换器要能列出所有连接过的机器人，并标出在线状态。"""
    store, _ = seeded
    await store.upsert_bot(BOT_ID, adapter='OneBot V11', online=True)
    await store.upsert_bot(BOT_B_ID, adapter='OneBot V11', online=False)

    resp = await client.get('/botui/api/bots', headers=HEADERS)
    assert resp.status_code == 200
    bots = {b['self_id']: b for b in resp.json()['bots']}
    assert set(bots) == {BOT_ID, BOT_B_ID}
    assert isinstance(bots[BOT_ID]['online'], bool)
    # 离线机器人仍然在列表里（「连接过」的都要保留）
    assert bots[BOT_B_ID]['online'] is False


async def test_bots_requires_token(client: AsyncClient):
    assert (await client.get('/botui/api/bots')).status_code == 401


async def test_chats_are_scoped_per_bot(client: AsyncClient, seeded):
    """同一群在 A、B 两个机器人下是两条会话，切换机器人互不干扰。"""
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts, chat_key

    store, key_a = seeded
    key_b = chat_key('group', str(GROUP_ID), BOT_B_ID)
    store.enqueue(
        MessageRecord(
            chat_key=key_b,
            chat_kind='group',
            chat_id=str(GROUP_ID),
            chat_name='测试群',
            direction='in',
            ts=now_ts(),
            self_id=BOT_B_ID,
            user_id='10001',
            user_name='小明',
            text='B 机器人收到的',
            segments=[{'type': 'text', 'text': 'B 机器人收到的'}],
        )
    )
    await store.flush()

    # 按 A 过滤只看得到 A 的会话
    data_a = (
        await client.get('/botui/api/chats', params={'bot': BOT_ID}, headers=HEADERS)
    ).json()
    keys_a = {c['key'] for c in data_a['chats']}
    assert key_a in keys_a
    assert key_b not in keys_a

    # 按 B 过滤只看得到 B 的会话
    data_b = (
        await client.get('/botui/api/chats', params={'bot': BOT_B_ID}, headers=HEADERS)
    ).json()
    keys_b = {c['key'] for c in data_b['chats']}
    assert key_b in keys_b
    assert key_a not in keys_b


async def test_search_is_scoped_per_bot(client: AsyncClient, seeded):
    """切换机器人后，正文搜索不能搜出别的机器人的聊天记录。"""
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts, chat_key

    store, _ = seeded
    store.enqueue(
        MessageRecord(
            chat_key=chat_key('group', '999', BOT_B_ID),
            chat_kind='group',
            chat_id='999',
            direction='in',
            ts=now_ts(),
            self_id=BOT_B_ID,
            user_id='1',
            user_name='路人',
            text='只在 B 里出现的关键词',
            segments=[{'type': 'text', 'text': '只在 B 里出现的关键词'}],
        )
    )
    await store.flush()

    # 用 A 的 id 搜不到 B 的消息
    none_a = (
        await client.get(
            '/botui/api/search',
            params={'q': '只在', 'bot': BOT_ID},
            headers=HEADERS,
        )
    ).json()
    assert none_a['messages'] == []

    # 用 B 的 id 能搜到
    hit_b = (
        await client.get(
            '/botui/api/search',
            params={'q': '只在', 'bot': BOT_B_ID},
            headers=HEADERS,
        )
    ).json()
    assert [m['text'] for m in hit_b['messages']] == ['只在 B 里出现的关键词']


async def test_meta_exposes_bot_list(client: AsyncClient, seeded):
    """/meta（带令牌）要带上机器人清单，前端首屏就能渲染切换器。"""
    store, _ = seeded
    await store.upsert_bot(BOT_ID, adapter='OneBot V11', online=True)
    data = (await client.get('/botui/api/meta', headers=HEADERS)).json()
    assert 'bot_list' in data
    assert any(b['self_id'] == BOT_ID for b in data['bot_list'])


async def test_send_prefers_chat_own_bot(client: AsyncClient, botui, seeded):
    """给 A 机器人的会话发消息，必须用 A 发送，不能落到 B 头上。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot, Adapter

    _, key = seeded  # key 属于 BOT_ID

    sent: list[str] = []

    async def fake_call_api(bot, api, **data):
        sent.append(str(bot.self_id))
        return {'message_id': 9300, 'status': 'ok'}

    adapter = nonebot.get_adapter(Adapter)
    bot_a = Bot(adapter, self_id=BOT_ID)
    bot_b = Bot(adapter, self_id=BOT_B_ID)
    original = adapter._call_api
    adapter._call_api = fake_call_api  # type: ignore[method-assign]
    adapter.bot_connect(bot_a)
    adapter.bot_connect(bot_b)
    try:
        resp = await client.post(
            '/botui/api/send',
            headers=HEADERS,
            json={'chat': key, 'text': '你好'},
        )
        assert resp.status_code == 200
    finally:
        adapter.bot_disconnect(bot_a)
        adapter.bot_disconnect(bot_b)
        adapter._call_api = original

    assert sent == [BOT_ID], '会话内嵌的机器人 ID 决定了由谁发送'


async def test_send_fails_when_own_bot_offline(client: AsyncClient, botui, seeded):
    """会话所属机器人不在线时宁可失败，也不能用别的机器人冒名发送。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot, Adapter

    _, key = seeded  # 属于 BOT_ID

    async def fake_call_api(_bot, api, **data):
        return {'message_id': 9400, 'status': 'ok'}

    adapter = nonebot.get_adapter(Adapter)
    bot_b = Bot(adapter, self_id=BOT_B_ID)  # 只有 B 在线
    original = adapter._call_api
    adapter._call_api = fake_call_api  # type: ignore[method-assign]
    adapter.bot_connect(bot_b)
    try:
        resp = await client.post(
            '/botui/api/send',
            headers=HEADERS,
            json={'chat': key, 'text': '你好'},
        )
        assert resp.status_code == 503
    finally:
        adapter.bot_disconnect(bot_b)
        adapter._call_api = original
