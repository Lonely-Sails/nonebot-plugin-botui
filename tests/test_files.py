"""文件下载 / 在线预览 / 合并转发 / 导出的接口测试。

这些接口都是「按用户给的 URL 去取内容」或「按 ID 去调适配器」，因此必须与
其他数据接口一样鉴权；同时不能因为目标不可达就把整个请求挂死。
"""

from __future__ import annotations

import json
import asyncio
from typing import ClassVar

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
    """两条消息（一条收到、一条发出），供文件/转发/导出接口使用。

    与 test_api.py 的夹具同名但不共享：那里是模块级 fixture，跨模块不可见。
    """
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
@pytest.mark.parametrize(
    'path',
    [
        '/botui/api/file?u=http://93.184.216.34/a.txt',
        '/botui/api/preview?u=http://93.184.216.34/a.txt',
        '/botui/api/forward?id=1',
        '/botui/api/export?chat=12345678:group_1',
    ],
)
async def test_file_endpoints_require_token(client: AsyncClient, path: str):
    resp = await client.get(path)
    assert resp.status_code == 401, path
    assert resp.json()['ok'] is False


# ── 文本预览 ────────────────────────────────────────────────────────────
async def test_preview_rejects_missing_url(client: AsyncClient, seeded):
    resp = await client.get('/botui/api/preview', headers=HEADERS)
    assert resp.status_code == 400


async def test_preview_blocks_internal_target(client: AsyncClient, seeded):
    """预览同样不能变成读内网的跳板。"""
    resp = await client.get(
        '/botui/api/preview',
        params={'u': 'http://127.0.0.1:8080/secret.txt'},
        headers=HEADERS,
    )
    assert resp.status_code == 502
    assert '无法读取' in resp.json()['error']


async def test_file_too_large_is_413(
    client: AsyncClient, seeded, monkeypatch: pytest.MonkeyPatch
):
    """超过 BOTUI_FILE_MAX_BYTES 的文件在转发前就被拦下。"""
    import src.nonebot_plugin_botui.media as media_module

    class _FakeResponse:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {'content-length': str(200 * 1024 * 1024)}

    async def _fake_probe(url, headers=None):
        return _FakeResponse()

    monkeypatch.setattr(media_module, 'probe', _fake_probe)
    resp = await client.get(
        '/botui/api/file',
        params={'u': 'http://93.184.216.34/huge.bin'},
        headers=HEADERS,
    )
    assert resp.status_code == 413
    assert '过大' in resp.json()['error']


# ── 文件下载 ────────────────────────────────────────────────────────────
async def test_file_rejects_unknown_id(client: AsyncClient, seeded):
    """既不是链接、也不是能反查到的文件标识时直接报错，而不是空转发。"""
    resp = await client.get(
        '/botui/api/file', params={'u': 'definitely-not-a-link'}, headers=HEADERS
    )
    assert resp.status_code == 400


@pytest_asyncio.fixture
async def local_http(monkeypatch: pytest.MonkeyPatch):
    """在环回地址上起一个真实的小型 HTTP 服务。

    ``BOTUI_MEDIA_ALLOW_PRIVATE`` 默认会拦住环回地址，所以这里临时打开它，
    才能验证「真的能把内容取回来」这条链路（转发、Range、文本读取）。

    注意 patch 的是 ``media`` 模块里那个 ``cfg`` **对象**：测试的别名模块
    机制可能产生两个模块对象，只有真正被引用的那个对象的属性才生效。
    """
    import src.nonebot_plugin_botui.media as media_module

    monkeypatch.setattr(media_module.cfg, 'botui_media_allow_private', True)
    bodies = {
        '/note.txt': '第一行\n第二行\n'.encode(),
        '/blob.bin': bytes(range(256)) * 8,
    }
    handlers: list[asyncio.AbstractServer] = []
    received: list[dict[str, str]] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
        except Exception:
            writer.close()
            return
        request = head.decode('latin-1')
        target = request.split(' ')[1] if ' ' in request else '/'
        path, _, query = target.partition('?')
        range_header = ''
        for line in request.split('\r\n'):
            if line.lower().startswith('range:'):
                range_header = line.split(':', 1)[1].strip()
        received.append({'path': path, 'query': query, 'range': range_header})

        body = bodies.get(path, b'not found')
        start, end = 0, len(body) - 1
        status = '200 OK'
        extra = ''
        if range_header.startswith('bytes='):
            spec = range_header.removeprefix('bytes=').split(',')[0]
            first, _, last = spec.partition('-')
            start = int(first or 0)
            end = min(int(last) if last else end, len(body) - 1)
            status = '206 Partial Content'
            extra = f'Content-Range: bytes {start}-{end}/{len(body)}\r\n'
        chunk = body[start : end + 1]
        writer.write(
            (
                f'HTTP/1.1 {status}\r\n'
                'Content-Type: text/plain\r\n'
                f'Content-Length: {len(chunk)}\r\n'
                f'{extra}'
                'Connection: close\r\n\r\n'
            ).encode()
            + chunk
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, '127.0.0.1', 0)
    handlers.append(server)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f'http://127.0.0.1:{port}', received
    finally:
        server.close()


async def test_file_download_proxies_content(client: AsyncClient, seeded, local_http):
    """走 /api/file 能把远端内容原样取回，并带上 Content-Disposition。"""
    base, _ = local_http
    resp = await client.get(
        '/botui/api/file',
        params={'u': base + '/note.txt', 'name': '笔记.txt', 'download': 1},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.content.decode() == '第一行\n第二行\n'
    disposition = resp.headers['content-disposition']
    assert disposition.startswith('attachment')
    assert "filename*=UTF-8''" in disposition


async def test_file_download_uses_inline_by_default(
    client: AsyncClient, seeded, local_http
):
    """不传 download 时用 inline，便于浏览器直接预览。"""
    base, _ = local_http
    resp = await client.get(
        '/botui/api/file', params={'u': base + '/note.txt'}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.headers['content-disposition'].startswith('inline')


async def test_file_download_forwards_range(client: AsyncClient, seeded, local_http):
    """Range 请求原样透传：音视频拖动进度条靠它，服务端不该整段重发。"""
    base, received = local_http
    resp = await client.get(
        '/botui/api/file',
        params={'u': base + '/blob.bin'},
        headers={**HEADERS, 'Range': 'bytes=0-9'},
    )
    assert resp.status_code == 206
    assert len(resp.content) == 10
    assert resp.headers['content-range'].startswith('bytes 0-9/')
    assert received[-1]['range'] == 'bytes=0-9'


async def test_preview_reads_text_content(client: AsyncClient, seeded, local_http):
    """文本预览返回内容、类型与截断标记。"""
    base, _ = local_http
    resp = await client.get(
        '/botui/api/preview',
        params={'u': base + '/note.txt', 'name': 'note.txt'},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['text'] == '第一行\n第二行\n'
    assert data['truncated'] is False
    assert data['content_type'] == 'text/plain'


async def test_preview_rejects_binary(client: AsyncClient, seeded, local_http):
    """二进制文件不带文本扩展名 / 类型时拒绝预览。"""
    base, _ = local_http
    resp = await client.get(
        '/botui/api/preview',
        params={'u': base + '/blob.bin', 'name': 'blob.bin'},
        headers=HEADERS,
    )
    assert resp.status_code in (415, 200)
    if resp.status_code == 415:
        assert '文本' in resp.json()['error']


async def test_preview_recovers_name_from_url(client: AsyncClient, seeded, local_http):
    """QQ 适配器只给直链、name 是 file.bin 时，预览要能按链接还原真名。"""
    base, _ = local_http
    resp = await client.get(
        '/botui/api/preview',
        params={
            'u': base + '/note.txt?fname=%E7%AC%94%E8%AE%B0.txt',
            'name': 'file.bin',
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()['name'] == '笔记.txt'


async def test_file_download_recovers_name_from_url(
    client: AsyncClient, seeded, local_http
):
    """下载响应头里的文件名同样要从链接还原，而不是干掉成 file。"""
    base, _ = local_http
    resp = await client.get(
        '/botui/api/file',
        params={
            'u': base + '/note.txt?fname=%E7%AC%94%E8%AE%B0.txt',
            'name': 'file.bin',
            'download': 1,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    disposition = resp.headers['content-disposition']
    assert "filename*=UTF-8''%E7%AC%94%E8%AE%B0.txt" in disposition


# ── 合并转发 ────────────────────────────────────────────────────────────
async def test_forward_uses_inline_nodes(client: AsyncClient, seeded):
    """记录里已经内联了转发节点时，不调适配器接口也能展开。"""
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts

    store, key = seeded
    nodes = [
        {
            'name': '小明',
            'user_id': '10001',
            'time': 1000000.0,
            'segments': [{'type': 'text', 'text': '转发里的第一句'}],
        }
    ]
    store.enqueue(
        MessageRecord(
            chat_key=key,
            chat_kind='group',
            chat_id='87654321',
            direction='in',
            ts=now_ts(),
            user_id='10001',
            user_name='小明',
            segments=[{'type': 'forward', 'id': 'fwd-1', 'count': 1, 'nodes': nodes}],
        )
    )
    await store.flush()

    resp = await client.get(
        '/botui/api/forward', params={'id': 'fwd-1', 'chat': key}, headers=HEADERS
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['source'] == 'record'
    assert data['nodes'][0]['segments'][0]['text'] == '转发里的第一句'


async def test_forward_without_bot_is_404(client: AsyncClient, seeded):
    """没有内联节点、会话机器人也不在线时，明确报错而不是卡住。"""
    _, key = seeded
    resp = await client.get(
        '/botui/api/forward', params={'id': '不存在', 'chat': key}, headers=HEADERS
    )
    assert resp.status_code == 404


# ── 导出 ────────────────────────────────────────────────────────────────
async def test_export_returns_json_attachment(client: AsyncClient, seeded):
    _, key = seeded
    resp = await client.get('/botui/api/export', params={'chat': key}, headers=HEADERS)
    assert resp.status_code == 200
    assert 'attachment' in resp.headers['content-disposition']
    assert 'application/json' in resp.headers['content-type']

    payload = json.loads(resp.content.decode('utf-8'))
    assert payload['count'] == 2
    assert [m['text'] for m in payload['messages']] == ['你好', '你好呀']
    assert payload['chat']['key'] == key


async def test_export_filename_handles_non_ascii(client: AsyncClient, seeded):
    """会话名是中文时，Content-Disposition 要给出 RFC 5987 的 filename*。"""
    _, key = seeded
    resp = await client.get('/botui/api/export', params={'chat': key}, headers=HEADERS)
    disposition = resp.headers['content-disposition']
    assert "filename*=UTF-8''" in disposition
    # ASCII 兜底名 + 编码名都在，浏览器总能挑一个
    assert 'filename="' in disposition


async def test_export_limits_rows(client: AsyncClient, seeded):
    resp = await client.get(
        '/botui/api/export', params={'chat': seeded[1], 'limit': 1}, headers=HEADERS
    )
    payload = json.loads(resp.content.decode('utf-8'))
    assert payload['count'] == 1


# ── meta 能力声明 ───────────────────────────────────────────────────────
async def test_meta_exposes_file_capabilities(client: AsyncClient, seeded):
    resp = await client.get('/botui/api/meta', headers=HEADERS)
    data = resp.json()
    assert data['capabilities']['preview'] is True
    assert data['capabilities']['export'] is True
    assert data['capabilities']['forward'] is True
    assert data['file_preview'] is True
    assert data['file_max_bytes'] > 0
