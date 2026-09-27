"""本地缓存（filecache.py）与缓存接口的测试。

缓存解决的是「媒体直链会过期」这个现实问题，所以这里重点覆盖三件事：

1. 存取与去重：同一个来源链接反复入库只留一份；
2. 回收策略：配额淘汰、ttl / retention、以及「引用」对保留期的影响；
3. 接口行为：``GET /api/cache``、``POST /api/cache``、``GET /api/cache/{id}``
   的鉴权、参数校验，以及 ``/api/file``、``/api/preview`` 的「缓存优先」。
"""

from __future__ import annotations

import time
import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient

TOKEN = 'test-token'
HEADERS = {'X-BotUI-Token': TOKEN}

SRC = 'http://93.184.216.34/pic.png'


def _cache(tmp_path: Path, **kwargs):
    from src.nonebot_plugin_botui.filecache import FileCache

    return FileCache(tmp_path, **kwargs)


# ── 纯逻辑：地址与类型 ──────────────────────────────────────────────────
def test_url_of_and_id_from_url_roundtrip():
    from src.nonebot_plugin_botui import filecache

    fid = 'AbCdEf123456'
    url = filecache.url_of(fid)
    assert url.endswith(f'/api/cache/{fid}')
    assert filecache.id_from_url(url) == fid
    # 不是缓存地址、或者类型不对时都不该乱认
    assert filecache.id_from_url('https://example.com/x.png') == ''
    assert filecache.id_from_url(None) == ''
    assert filecache.id_from_url(123) == ''


def test_kind_of_classifies_images_and_files():
    from src.nonebot_plugin_botui.filecache import kind_of

    assert kind_of('image') == 'image'
    assert kind_of('file', 'image/jpeg') == 'image'
    assert kind_of('file', 'application/pdf', 'report.pdf') == 'file'
    assert kind_of('file', '', 'archive.zip') == 'file'


def test_cacheable_segments_filters_by_type_and_scheme():
    from src.nonebot_plugin_botui.segments import cacheable_segments

    segments = [
        {'type': 'image', 'url': 'http://a/img.png'},
        {'type': 'text', 'text': 'hi'},
        {'type': 'file', 'url': 'base64://xxxx'},
        {'type': 'file', 'url': 'https://b/f.zip', 'name': 'f.zip'},
        {'type': 'video', 'url': 'http://c/v.mp4'},
    ]
    picked = cacheable_segments(segments)
    assert [s['type'] for s in picked] == ['image', 'file', 'video']
    # limit 生效
    assert len(cacheable_segments(segments, limit=2)) == 2


# ── 纯逻辑：存取与去重 ──────────────────────────────────────────────────
def test_save_and_lookup_by_source_and_cache_url(tmp_path: Path):
    cache = _cache(tmp_path, max_bytes=1024 * 1024, max_files=100)
    assert cache.enabled

    rec = cache.save(
        b'hello', name='a.txt', mime='text/plain', kind='file', source=SRC
    )
    assert rec is not None
    assert rec.path.read_bytes() == b'hello'

    # 来源链接与缓存地址都能命中同一条目
    assert cache.lookup(SRC) is not None
    from src.nonebot_plugin_botui.filecache import url_of

    assert cache.lookup(url_of(rec.id)) is not None
    assert cache.get(rec.id) is not None
    assert cache.has(SRC)
    assert cache.lookup('http://nope/x') is None


def test_put_dedupes_same_source(tmp_path: Path):
    """同一来源链接第二次入库时只追加引用，不重复占盘。"""
    cache = _cache(tmp_path, max_bytes=1024 * 1024, max_files=100)
    tmp = cache.tmp_dir()

    first = tmp / 'a.part'
    first.write_bytes(b'0123456789')
    rec1 = cache.put(first, name='a.bin', source=SRC, kind='file')
    assert rec1 is not None

    second = tmp / 'b.part'
    second.write_bytes(b'0123456789')
    rec2 = cache.put(second, name='a.bin', source=SRC, kind='file')
    assert rec2 is not None
    assert rec2.id == rec1.id
    # 新的临时文件被丢弃，磁盘上只有一份
    assert not second.exists()
    assert cache.total_files() == 1


def test_allows_respects_limits(tmp_path: Path):
    cache = _cache(tmp_path, max_bytes=100, max_files=10, file_max_bytes=50)
    assert cache.allows(50)
    assert not cache.allows(51)  # 超过单文件上限
    assert not cache.allows(0)

    disabled = _cache(tmp_path, max_bytes=0, max_files=0)
    assert not disabled.enabled
    assert not disabled.allows(1)
    assert disabled.save(b'x') is None


# ── 纯逻辑：引用与回收 ──────────────────────────────────────────────────
def test_reference_protects_from_ttl(tmp_path: Path):
    """有聊天记录引用的资源，ttl 到了也不删；retention 到了才删。"""
    cache = _cache(tmp_path, max_bytes=1024, max_files=100, ttl=100, retention=1000)
    rec = cache.save(b'data', name='a.bin', source=SRC, kind='file')
    assert rec is not None

    # 只带 src: 引用时，等 ttl 过期就会被回收
    old = time.time() - 200
    entry = cache._load_index()[SRC]
    entry['accessed'] = old
    entry['created'] = old
    cache._write_meta(entry)
    assert cache.cleanup() == 1
    assert cache.get(rec.id) is None

    # 再看被引用的：ttl 过期但 retention 未到 → 保留
    rec2 = cache.save(b'data2', name='b.bin', source=SRC + '2', kind='file')
    assert rec2 is not None
    assert cache.add_ref(SRC + '2', 'msg:123:1')
    entry = cache._load_index()[SRC + '2']
    entry['accessed'] = old
    entry['created'] = old
    cache._write_meta(entry)
    assert cache.cleanup() == 0
    assert cache.get(rec2.id) is not None


def test_add_and_drop_ref_ignore_unknown(tmp_path: Path):
    cache = _cache(tmp_path, max_bytes=1024, max_files=100)
    # 资源不在缓存里时静默失败，不抛异常
    assert cache.add_ref(SRC, 'msg:1:1') is False
    assert cache.drop_ref(SRC, 'msg:1:1') is False

    rec = cache.save(b'x', name='a.bin', source=SRC, kind='file')
    assert rec is not None
    assert cache.add_ref(SRC, 'msg:1:1') is True
    assert cache.drop_ref(SRC, 'msg:1:1') is True


def test_eviction_prefers_unreferenced(tmp_path: Path):
    """超配额时先删没人引用的，被引用的尽量留着。"""
    cache = _cache(tmp_path, max_bytes=16, max_files=0)
    # 两个 8 字节资源：一个被引用，一个没有
    kept = cache.save(b'12345678', name='k.bin', source=SRC + '/k', kind='file')
    assert kept is not None
    assert cache.add_ref(SRC + '/k', 'msg:1:1')

    dropped = cache.save(b'abcdefgh', name='d.bin', source=SRC + '/d', kind='file')
    assert dropped is not None

    # 再塞一个，触发淘汰：被引用的留着，没引用的先走
    cache.save(b'XYZXYZXY', name='n.bin', source=SRC + '/n', kind='file')
    assert cache.get(kept.id) is not None
    assert cache.get(dropped.id) is None


def test_clear_modes(tmp_path: Path):
    cache = _cache(tmp_path, max_bytes=1024 * 1024, max_files=100)
    img = cache.save(b'img', name='a.png', mime='image/png', kind='image', source=SRC)
    fil = cache.save(b'fil', name='a.bin', kind='file', source=SRC + 'f')
    orphan = cache.save(b'orph', name='o.bin', kind='file', source=SRC + 'o')
    for rec in (img, fil, orphan):
        assert rec is not None
    cache.add_ref(SRC, 'msg:1:1')
    cache.add_ref(SRC + 'f', 'msg:1:2')

    # orphans 只删没引用的
    result = cache.clear('orphans')
    assert result['removed'] == 1
    assert cache.get(orphan.id) is None
    assert cache.get(img.id) is not None

    # image 只删图片
    assert cache.clear('image')['removed'] == 1
    assert cache.get(img.id) is None
    assert cache.get(fil.id) is not None

    # all 全清
    cache.clear('all')
    assert cache.total_files() == 0


def test_stats_shape(tmp_path: Path):
    cache = _cache(tmp_path, max_bytes=1024, max_files=100, ttl=1, retention=2)
    cache.save(b'img', name='a.png', kind='image', source=SRC)
    cache.save(b'file', name='b.bin', kind='file', source=SRC + 'f')
    cache.lookup(SRC)  # 制造一次 hit
    cache.lookup('http://miss/x')  # 制造一次 miss

    stats = cache.stats()
    assert stats['files'] == 2
    assert stats['bytes'] == 7
    assert stats['images']['files'] == 1
    assert stats['others']['files'] == 1
    assert stats['hits'] == 1
    assert stats['misses'] == 1
    assert stats['enabled'] is True
    cache.reset_stats()
    assert cache.stats()['hits'] == 0


def test_index_rebuilds_when_missing(tmp_path: Path):
    """索引文件被删掉也能扫盘重建（缓存能自愈）。"""
    cache = _cache(tmp_path, max_bytes=1024 * 1024, max_files=100)
    rec = cache.save(b'data', name='a.bin', source=SRC, kind='file')
    assert rec is not None

    (tmp_path / 'index.json').unlink()
    fresh = _cache(tmp_path, max_bytes=1024 * 1024, max_files=100)
    assert fresh.get(rec.id) is not None
    assert fresh.lookup(SRC) is not None


# ── 接口 ────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def cache_env(botui):
    """把插件用的缓存清干净，并保证是启用状态。"""
    import src.nonebot_plugin_botui.api as api_module

    cache = botui._get_cache()
    cache.clear('all')
    cache.reset_stats()
    api_module._cache = cache
    yield cache
    cache.clear('all')


async def test_cache_endpoints_require_token(client: AsyncClient, cache_env):
    assert (await client.get('/botui/api/cache')).status_code == 401
    assert (await client.post('/botui/api/cache')).status_code == 401


async def test_meta_exposes_cache_enabled(client: AsyncClient, cache_env):
    resp = await client.get('/botui/api/meta', headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()['cache_enabled'] is True


async def test_get_cache_stats(client: AsyncClient, cache_env):
    cache_env.save(b'hello', name='a.txt', kind='file', source=SRC)
    resp = await client.get('/botui/api/cache', headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data['ok'] is True
    assert data['cache']['files'] == 1


async def test_post_cache_clear_and_reset(client: AsyncClient, cache_env):
    rec = cache_env.save(b'hello', name='a.txt', kind='file', source=SRC)
    assert rec is not None

    resp = await client.post(
        '/botui/api/cache',
        json={'action': 'clear', 'mode': 'orphans'},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()['removed'] == 1

    resp = await client.post(
        '/botui/api/cache', json={'action': 'reset_stats'}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()['cache']['hits'] == 0


async def test_post_cache_rejects_bad_mode(client: AsyncClient, cache_env):
    resp = await client.post(
        '/botui/api/cache',
        json={'action': 'clear', 'mode': 'nonsense'},
        headers=HEADERS,
    )
    assert resp.status_code == 400


async def test_post_cache_rejects_bad_action(client: AsyncClient, cache_env):
    resp = await client.post(
        '/botui/api/cache', json={'action': 'drop-database'}, headers=HEADERS
    )
    assert resp.status_code == 400


async def test_get_cached_returns_file_and_download(
    client: AsyncClient, cache_env
):
    rec = cache_env.save(
        b'payload', name='笔记.txt', mime='text/plain', kind='file', source=SRC
    )
    assert rec is not None

    resp = await client.get(f'/botui/api/cache/{rec.id}', headers=HEADERS)
    assert resp.status_code == 200
    assert resp.content == b'payload'
    assert resp.headers['x-botui-cache'] == 'hit'
    assert resp.headers['content-disposition'].startswith('inline')

    resp = await client.get(
        f'/botui/api/cache/{rec.id}', params={'download': 1}, headers=HEADERS
    )
    assert resp.headers['content-disposition'].startswith('attachment')
    assert "filename*=UTF-8''" in resp.headers['content-disposition']


async def test_get_cached_bad_id_is_404(client: AsyncClient, cache_env):
    resp = await client.get('/botui/api/cache/../../etc/passwd', headers=HEADERS)
    assert resp.status_code == 404
    resp = await client.get('/botui/api/cache/zzzzzzzz', headers=HEADERS)
    assert resp.status_code == 404


async def test_file_endpoint_serves_from_cache(client: AsyncClient, cache_env):
    """原始链接已经有本地副本时，/api/file 不再去碰那个会过期的链接。"""
    cache_env.save(
        b'cached-body', name='note.txt', mime='text/plain', kind='file', source=SRC
    )
    resp = await client.get('/botui/api/file', params={'u': SRC}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.content == b'cached-body'
    assert resp.headers['x-botui-cache'] == 'hit'


async def test_preview_endpoint_serves_from_cache(client: AsyncClient, cache_env):
    cache_env.save(
        b'line1\nline2\n', name='note.txt', mime='text/plain', kind='file', source=SRC
    )
    resp = await client.get(
        '/botui/api/preview', params={'u': SRC, 'name': 'note.txt'}, headers=HEADERS
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['text'] == 'line1\nline2\n'
    assert data['cached'] is True


@pytest.mark.parametrize('mode', ['all', 'orphans', 'image', 'file'])
def test_clear_modes_are_supported(mode: str):
    from src.nonebot_plugin_botui.api import CACHE_CLEAR_MODES

    assert mode in CACHE_CLEAR_MODES


# ── 下载并回填段（filefetch.py） ────────────────────────────────────────
@pytest_asyncio.fixture
async def local_media(monkeypatch: pytest.MonkeyPatch):
    """环回地址上的小 HTTP 服务，用来验证「真的把链接内容抓回来了」。

    与 test_files.py 的同名夹具同源：默认 SSRF 校验会拦住环回地址，这里临时
    在 ``media`` 模块真正持有的那个 ``cfg`` 对象上打开开关。
    """
    import src.nonebot_plugin_botui.media as media_module

    monkeypatch.setattr(media_module.cfg, 'botui_media_allow_private', True)
    bodies = {
        '/pic.png': b'\x89PNG\r\n\x1a\n' + b'0' * 32,
        '/doc.pdf': b'%PDF-1.4 hello',
    }

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
        except Exception:
            writer.close()
            return
        request = head.decode('latin-1')
        target = request.split(' ')[1] if ' ' in request else '/'
        path = target.partition('?')[0]
        body = bodies.get(path)
        if body is None:
            writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n')
        else:
            writer.write(
                (
                    'HTTP/1.1 200 OK\r\n'
                    f'Content-Length: {len(body)}\r\n'
                    'Content-Type: application/octet-stream\r\n'
                    'Connection: close\r\n\r\n'
                ).encode()
                + body
            )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f'http://127.0.0.1:{port}'
    finally:
        server.close()


async def test_cache_segment_downloads_and_rewrites_url(
    cache_env, local_media, botui
):
    """抓回媒体后，段里的 ``url`` 会被换成缓存地址并建立聊天记录引用。"""
    from src.nonebot_plugin_botui.filefetch import ref_for, cache_segment

    ref = ref_for('group_1:12345678', 7)
    seg = {'type': 'image', 'url': local_media + '/pic.png', 'name': 'pic.png'}
    ok = await cache_segment(cache_env, seg, cfg=botui.cfg, ref=ref)
    assert ok is True
    # 链接已改写成缓存地址，WebUI 以后不会再碰原始链接
    from src.nonebot_plugin_botui.filecache import id_from_url

    fid = id_from_url(seg['url'])
    assert fid
    assert seg['size'] == 40
    record = cache_env.get(fid)
    assert record is not None
    assert record.path.read_bytes().startswith(b'\x89PNG')
    # 引用落进 meta，回收时能识别出「还有人用」
    entry = cache_env._load_index()[f'cache:{fid}']
    assert ref in entry['refs']


async def test_cache_segment_skips_already_cached(cache_env, botui):
    """已经是缓存地址的段不再重复抓。"""
    from src.nonebot_plugin_botui.filefetch import cache_segment

    rec = cache_env.save(b'x', name='a.bin', kind='file', source='http://s/a')
    assert rec is not None
    from src.nonebot_plugin_botui.filecache import url_of

    seg = {'type': 'file', 'url': url_of(rec.id)}
    assert await cache_segment(cache_env, seg, cfg=botui.cfg) is False


async def test_cache_message_respects_budget(cache_env, local_media, botui):
    """单条消息的缓存预算用完就不再抓后面的资源。"""
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts
    from src.nonebot_plugin_botui.filefetch import cache_message

    budget_cfg = botui.cfg.model_copy(
        update={'botui_cache_max_bytes_per_message': 40}
    )
    record = MessageRecord(
        chat_key='group_1:12345678',
        chat_kind='group',
        chat_id='1',
        chat_name='群',
        direction='in',
        ts=now_ts(),
        user_id='2',
        user_name='x',
        text='',
        segments=[
            {'type': 'file', 'url': local_media + '/pic.png', 'name': 'a.png'},
            {'type': 'file', 'url': local_media + '/doc.pdf', 'name': 'b.pdf'},
        ],
    )
    record.row_id = 5
    done = await cache_message(cache_env, record, cfg=budget_cfg)
    # 预算 40 只够一个 40 字节的资源
    assert done == 1
    assert cache_env.total_files() == 1


async def test_cache_message_outgoing_only_adds_refs(cache_env, botui):
    """机器人发出的消息只登记引用，不重新下载。"""
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts
    from src.nonebot_plugin_botui.filecache import url_of
    from src.nonebot_plugin_botui.filefetch import cache_message

    rec = cache_env.save(b'own', name='a.bin', kind='file', source='http://s/own')
    assert rec is not None
    record = MessageRecord(
        chat_key='group_1:12345678',
        chat_kind='group',
        chat_id='1',
        chat_name='群',
        direction='out',
        ts=now_ts(),
        user_id='12345678',
        user_name='我',
        text='',
        segments=[{'type': 'file', 'url': url_of(rec.id), 'name': 'a.bin'}],
    )
    record.message_id = '999'
    await cache_message(cache_env, record, cfg=botui.cfg)
    entry = cache_env._load_index()[f'cache:{rec.id}']
    assert 'msgid:999' in entry['refs']
    # 没有产生第二份副本
    assert cache_env.total_files() == 1

