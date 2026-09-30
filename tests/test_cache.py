"""统一媒体库（mediastore.py）与媒体接口的测试。

媒体库解决的是「媒体直链会过期」这个现实问题，也把原先散落的上传附件与
收到的媒体合成了一份存储，所以这里重点覆盖四件事：

1. 存取与去重：资源按内容 md5 命名，同样的内容反复入库只留一份；
2. 回收策略：配额淘汰、ttl / retention、以及「引用」对保留期的影响；
3. 接口行为：``GET /api/cache``、``POST /api/cache``、``GET/DELETE /api/media/{id}``
   的鉴权与参数校验；
4. 抓取与回填：``ingest.py`` 把消息里可抓的媒体下载入库并改写段里的地址；
5. 待发送暂存：``POST /api/upload`` 只把文件放进系统临时目录，发送时才入库。

媒体库是异步的、且必须 attach 到一条真实的 aiosqlite 连接，所以这里的夹具都用
``media``（见 conftest.py），它在会话级的测试库连接上建表。
"""

from __future__ import annotations

import json
import time
import asyncio
import hashlib

import pytest
import pytest_asyncio
from httpx import AsyncClient

TOKEN = 'test-token'
HEADERS = {'X-BotUI-Token': TOKEN}

SRC = 'http://93.184.216.34/pic.png'


# ── 纯逻辑：地址与类型 ──────────────────────────────────────────────────
def test_url_of_and_id_from_url_roundtrip():
    from src.nonebot_plugin_botui import mediastore

    fid = '0123456789abcdef0123456789abcdef'
    url = mediastore.url_of(fid)
    assert url.endswith(f'/api/media/{fid}')
    assert mediastore.id_from_url(url) == fid
    # 不是媒体地址、或者类型不对时都不该乱认
    assert mediastore.id_from_url('https://example.com/x.png') == ''
    assert mediastore.id_from_url(None) == ''
    assert mediastore.id_from_url(123) == ''


def test_kind_of_classifies_images_and_files():
    from src.nonebot_plugin_botui.mediastore import kind_of

    assert kind_of('pic.png') == 'image'
    assert kind_of('file', 'image/jpeg') == 'image'
    assert kind_of('report.pdf', 'application/pdf') == 'file'
    assert kind_of('archive.zip') == 'file'


def test_safe_filename_strips_paths():
    from src.nonebot_plugin_botui.mediastore import safe_filename

    assert safe_filename('C:\\Users\\a\\图片.png') == '图片.png'
    assert safe_filename('../../etc/passwd') == 'passwd'
    assert safe_filename('') == 'file'


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
async def test_save_and_lookup_by_source_and_url(media):
    from src.nonebot_plugin_botui.mediastore import url_of

    rec = await media.save(
        b'hello', name='a.txt', mime='text/plain', kind='file', source=SRC
    )
    assert rec is not None
    assert rec.path.read_bytes() == b'hello'

    # 来源链接与取回地址都能命中同一条目
    assert await media.lookup(SRC) is not None
    assert await media.lookup(url_of(rec.id)) is not None
    assert await media.get(rec.id) is not None
    assert await media.has(SRC)
    assert await media.lookup('http://nope/x') is None


async def test_put_dedupes_same_content(media):
    """资源按内容 md5 命名：同样的内容第二次入库只留一份，临时文件被丢弃。"""
    tmp = media.tmp_dir()

    first = tmp / 'a.part'
    first.write_bytes(b'0123456789')
    rec1 = await media.put(first, name='a.bin', source=SRC, kind='file')
    assert rec1 is not None
    assert rec1.id == hashlib.md5(b'0123456789').hexdigest()

    second = tmp / 'b.part'
    second.write_bytes(b'0123456789')
    rec2 = await media.put(second, name='b.bin', source=SRC, kind='file')
    assert rec2 is not None
    assert rec2.id == rec1.id
    # 新的临时文件被丢弃，磁盘上只有一份
    assert not second.exists()
    assert (await media.stats())['files'] == 1


async def test_allows_respects_limits(media):
    from src.nonebot_plugin_botui.mediastore import MediaStore

    assert media.allows(50)
    assert not media.allows(0)

    tiny = MediaStore(media.dir, max_bytes=100, max_files=10, file_max_bytes=50)
    assert tiny.allows(50)
    assert not tiny.allows(51)  # 超过单文件上限

    disabled = MediaStore(media.dir, max_bytes=0, max_files=0)
    assert not disabled.enabled
    assert not disabled.allows(1)


# ── 纯逻辑：引用与回收 ──────────────────────────────────────────────────
async def _age(media, fid: str, when: float) -> None:
    """把某条记录的 accessed / created 拨回过去（避免真实等待）。"""
    assert media._db is not None
    await media._db.execute(
        'UPDATE blobs SET accessed = ?, created = ? WHERE id = ?', (when, when, fid)
    )
    await media._db.commit()


async def test_reference_protects_from_ttl(media):
    """有聊天记录引用的资源，ttl 到了也不删；retention 到了才删。"""
    old = time.time() - 200
    # ttl 100s、retention 1000s（见 media 夹具的默认配置）
    rec = await media.save(b'data', name='a.bin', source=SRC, kind='file')
    assert rec is not None
    # 没引用的：过了 ttl 就该回收
    await _age(media, rec.id, old)
    assert await media.cleanup() == 1
    assert await media.get(rec.id) is None

    # 有引用的：ttl 过了但 retention 未到 → 保留
    rec2 = await media.save(b'data2', name='b.bin', source=SRC + '2', kind='file')
    assert rec2 is not None
    assert await media.add_ref(SRC + '2', 'msg:123:1')
    await _age(media, rec2.id, old)
    assert await media.cleanup() == 0
    assert await media.get(rec2.id) is not None


async def test_add_and_drop_ref_ignore_unknown(media):
    # 资源不在库里时静默失败，不抛异常
    assert await media.add_ref(SRC, 'msg:1:1') is False
    assert await media.drop_ref(SRC, 'msg:1:1') is False

    rec = await media.save(b'x', name='a.bin', source=SRC, kind='file')
    assert rec is not None
    assert await media.add_ref(SRC, 'msg:1:1') is True
    assert await media.drop_ref(SRC, 'msg:1:1') is True


async def test_eviction_prefers_unreferenced(media, botui):
    """超配额时先删没人引用的，被引用的尽量留着。"""
    from src.nonebot_plugin_botui.mediastore import MediaStore

    store = botui._get_store()
    # 单独建一个配额很小的实例（跑在同一个库连接上），更容易触发淘汰
    small = MediaStore(media.dir / 'evict', max_bytes=16)
    await small.attach(store.db, store.lock)
    await small.clear('all')

    # 两个 8 字节资源：一个被引用，一个没有
    kept = await small.save(b'12345678', name='k.bin', source=SRC + '/k', kind='file')
    assert kept is not None
    assert await small.add_ref(SRC + '/k', 'msg:1:1')
    dropped = await small.save(
        b'abcdefgh', name='d.bin', source=SRC + '/d', kind='file'
    )
    assert dropped is not None

    # 再塞一个，触发淘汰：被引用的留着，没引用的先走
    await small.save(b'XYZXYZXY', name='n.bin', source=SRC + '/n', kind='file')
    assert await small.get(kept.id) is not None
    assert await small.get(dropped.id) is None


async def test_clear_modes(media):
    img = await media.save(
        b'img', name='a.png', mime='image/png', kind='image', source=SRC
    )
    fil = await media.save(b'fil', name='a.bin', kind='file', source=SRC + 'f')
    orphan = await media.save(b'orph', name='o.bin', kind='file', source=SRC + 'o')
    for rec in (img, fil, orphan):
        assert rec is not None
    await media.add_ref(SRC, 'msg:1:1')
    await media.add_ref(SRC + 'f', 'msg:1:2')

    # orphans 只删没引用的
    result = await media.clear('orphans')
    assert result['removed'] == 1
    assert await media.get(orphan.id) is None
    assert await media.get(img.id) is not None

    # image 只删图片
    assert (await media.clear('image'))['removed'] == 1
    assert await media.get(img.id) is None
    assert await media.get(fil.id) is not None

    # all 全清
    await media.clear('all')
    assert (await media.stats())['files'] == 0


async def test_stats_shape(media):
    await media.save(b'img', name='a.png', kind='image', source=SRC)
    await media.save(b'file', name='b.bin', kind='file', source=SRC + 'f')
    await media.lookup(SRC)  # 制造一次 hit
    await media.lookup('http://miss/x')  # 制造一次 miss

    stats = await media.stats()
    assert stats['files'] == 2
    assert stats['bytes'] == 7
    assert stats['images']['files'] == 1
    assert stats['others']['files'] == 1
    assert stats['hits'] == 1
    assert stats['misses'] == 1
    assert stats['enabled'] is True
    media.reset_stats()
    assert (await media.stats())['hits'] == 0


# ── 接口 ────────────────────────────────────────────────────────────────
async def test_cache_endpoints_require_token(client: AsyncClient, media):
    assert (await client.get('/botui/api/cache')).status_code == 401
    assert (await client.post('/botui/api/cache')).status_code == 401


async def test_meta_exposes_media_capability(client: AsyncClient, media):
    resp = await client.get('/botui/api/meta', headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body['cache_enabled'] is True
    assert body['upload_enabled'] is True
    assert body['upload_max_bytes'] > 0


async def test_get_cache_stats(client: AsyncClient, media):
    await media.save(b'hello', name='a.txt', kind='file', source=SRC)
    resp = await client.get('/botui/api/cache', headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data['ok'] is True
    assert data['cache']['files'] == 1


async def test_post_cache_clear_and_reset(client: AsyncClient, media):
    rec = await media.save(b'hello', name='a.txt', kind='file', source=SRC)
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


async def test_post_cache_rejects_bad_mode(client: AsyncClient, media):
    resp = await client.post(
        '/botui/api/cache',
        json={'action': 'clear', 'mode': 'nonsense'},
        headers=HEADERS,
    )
    assert resp.status_code == 400


async def test_post_cache_rejects_bad_action(client: AsyncClient, media):
    resp = await client.post(
        '/botui/api/cache', json={'action': 'drop-database'}, headers=HEADERS
    )
    assert resp.status_code == 400


async def test_media_endpoint_returns_file_and_download(client: AsyncClient, media):
    rec = await media.save(
        b'payload', name='笔记.txt', mime='text/plain', kind='file', source=SRC
    )
    assert rec is not None

    resp = await client.get(f'/botui/api/media/{rec.id}', headers=HEADERS)
    assert resp.status_code == 200
    assert resp.content == b'payload'
    assert resp.headers['x-botui-cache'] == 'hit'
    assert resp.headers['content-disposition'].startswith('inline')

    resp = await client.get(
        f'/botui/api/media/{rec.id}', params={'download': 1}, headers=HEADERS
    )
    assert resp.headers['content-disposition'].startswith('attachment')
    assert "filename*=UTF-8''" in resp.headers['content-disposition']


async def test_media_bad_id_is_404(client: AsyncClient, media):
    route = '/botui/api/media'
    # 路径穿越与不存在的 id 都返回 404（不是 500，也不去读文件）
    assert (await client.get(f'{route}/zzzzzzzz', headers=HEADERS)).status_code == 404
    assert (
        await client.get(f'{route}/../../etc/passwd', headers=HEADERS)
    ).status_code == 404


async def test_file_endpoint_serves_from_media(client: AsyncClient, media):
    """原始链接已经有本地副本时，/api/file 不再去碰那个会过期的链接。"""
    await media.save(
        b'cached-body', name='note.txt', mime='text/plain', kind='file', source=SRC
    )
    resp = await client.get('/botui/api/file', params={'u': SRC}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.content == b'cached-body'
    assert resp.headers['x-botui-cache'] == 'hit'


async def test_preview_endpoint_serves_from_media(client: AsyncClient, media):
    await media.save(
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
    from src.nonebot_plugin_botui.api import MEDIA_CLEAR_MODES

    assert mode in MEDIA_CLEAR_MODES


# ── 下载并回填段（ingest.py） ───────────────────────────────────────────
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


async def _refs(media, fid: str) -> list[str]:
    assert media._db is not None
    async with media._db.execute('SELECT refs FROM blobs WHERE id = ?', (fid,)) as cur:
        row = await cur.fetchone()
    return json.loads(row['refs']) if row is not None else []


async def test_ingest_segment_downloads_and_rewrites_url(media, local_media, botui):
    """抓回媒体后，段里的 ``url`` 会被换成媒体库地址并建立聊天记录引用。"""
    from src.nonebot_plugin_botui.ingest import ingest_segment
    from src.nonebot_plugin_botui.mediastore import ref_for, id_from_url

    ref = ref_for('group_1:12345678', 7)
    seg = {'type': 'image', 'url': local_media + '/pic.png', 'name': 'pic.png'}
    ok = await ingest_segment(media, seg, cfg=botui.cfg, ref=ref)
    assert ok is True
    # 链接已改写成媒体库地址，WebUI 以后不会再碰原始链接
    fid = id_from_url(seg['url'])
    assert fid
    assert seg['size'] == 40
    record = await media.get(fid)
    assert record is not None
    assert record.path.read_bytes().startswith(b'\x89PNG')
    # 引用落进库里，回收时能识别出「还有人用」
    assert 'msg:group_1:12345678:7' in await _refs(media, fid)


async def test_ingest_segment_skips_already_stored(media, botui):
    """已经是媒体库地址的段：命中即算就位，且地址保持原样。"""
    from src.nonebot_plugin_botui.ingest import ingest_segment
    from src.nonebot_plugin_botui.mediastore import url_of

    rec = await media.save(b'x', name='a.bin', kind='file', source='http://s/a')
    assert rec is not None

    url = url_of(rec.id)
    seg = {'type': 'file', 'url': url}
    assert await ingest_segment(media, seg, cfg=botui.cfg) is True
    # 地址没被改写（改了前端会丢掉 /api/media 那条路径）
    assert seg['url'] == url

    # 不在库里的媒体库地址：没有可抓的原始链接，直接放弃
    dead = {'type': 'file', 'url': url_of('deadbeefcafe')}
    assert await ingest_segment(media, dead, cfg=botui.cfg) is False
    # 非 http 链接（base64:// 之类）也不抓
    seg = {'type': 'file', 'url': 'base64://xx'}
    assert await ingest_segment(media, seg, cfg=botui.cfg) is False


async def test_ingest_message_respects_budget(media, local_media, botui):
    """单条消息的抓取预算用完就不再抓后面的资源。"""
    from src.nonebot_plugin_botui.ingest import ingest_message
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts

    budget_cfg = botui.cfg.model_copy(update={'botui_media_max_bytes_per_message': 40})
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
    done = await ingest_message(media, record, cfg=budget_cfg)
    # 预算 40 只够一个 40 字节的资源
    assert done == 1
    assert (await media.stats())['files'] == 1


async def test_ingest_message_outgoing_only_adds_refs(media, botui):
    """机器人发出的消息只登记引用，不重新下载。"""
    from src.nonebot_plugin_botui.ingest import ingest_message
    from src.nonebot_plugin_botui.models import MessageRecord, now_ts
    from src.nonebot_plugin_botui.mediastore import url_of

    rec = await media.save(b'own', name='a.bin', kind='file', source='http://s/own')
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
    done = await ingest_message(media, record, cfg=botui.cfg)
    # 命中已有副本（不必联网下载），且建立了 msgid 引用
    assert done == 1
    assert record.segments[0]['url'] == url_of(rec.id)
    assert 'msgid:999' in await _refs(media, rec.id)
    # 没有产生第二份副本
    assert (await media.stats())['files'] == 1


# ── 发出的消息：整条链路（上传 → 取回 → 删除） ──────────────────────────
async def test_upload_then_fetch_and_delete(client: AsyncClient, media, botui):
    """上传 → 取回 → 删除的完整链路（附件现在就存在媒体库里）。"""
    png = b'\x89PNG\r\n\x1a\n' + b'0' * 16
    up = await client.post(
        '/botui/api/upload?name=%E5%9B%BE%E7%89%87.png&type=image/png',
        headers=HEADERS,
        content=png,
    )
    assert up.status_code == 200
    info = up.json()['file']
    assert info['name'] == '图片.png'
    assert info['kind'] == 'image'
    assert info['size'] == len(png)
    assert info['url'].endswith(f'/api/media/{info["id"]}')

    # 上传后拿到的地址就是媒体库取回地址，带令牌即可取回
    got = await client.get(f'/botui/api/media/{info["id"]}', headers=HEADERS)
    assert got.status_code == 200
    assert got.content == png
    assert got.headers['content-type'].startswith('image/png')

    # 鉴权同样是硬要求：图片是 <img src> 直接取的，只能靠 query
    assert (await client.get(f'/botui/api/media/{info["id"]}')).status_code == 401

    rm = await client.request(
        'DELETE', f'/botui/api/media/{info["id"]}', headers=HEADERS
    )
    assert rm.status_code == 200
    assert rm.json()['removed'] is True
    assert (
        await client.get(f'/botui/api/media/{info["id"]}', headers=HEADERS)
    ).status_code == 404


async def test_upload_rejects_path_traversal(client: AsyncClient, media):
    """id 里带路径分隔符一律拒绝，避免变成读文件接口。"""
    for uid in ('../../etc/passwd', 'a/b', '..'):
        resp = await client.get(f'/botui/api/media/{uid}', headers=HEADERS)
        # 含分隔符的路由不匹配、其余由媒体库的 id 校验拦截，都是 404
        assert resp.status_code == 404, uid


async def test_sent_attachment_stays_reachable(client: AsyncClient, media, botui):
    """发出的附件：消息段里存的是媒体库地址，库清空后才真的取不到。

    这正是「自己发的图过期打不开」的修复路径 —— 发出的图片在记录里指向
    ``/api/media/<id>``，指向的就是媒体库自己那份文件。
    """
    from src.nonebot_plugin_botui.mediastore import url_of, id_from_url

    png = b'\x89PNG\r\n\x1a\nbody'
    rec = await media.save(png, name='pic.png', mime='image/png', kind='image')
    assert rec is not None
    url = url_of(rec.id)
    assert id_from_url(url) == rec.id

    got = await client.get(url, headers=HEADERS)
    assert got.status_code == 200
    assert got.content == png
    assert got.headers['x-botui-cache'] == 'hit'

    # 库被清空之后才真的 404（证明之前那条就是库里的文件）
    await media.clear('all')
    assert (await client.get(url, headers=HEADERS)).status_code == 404


# ── 待发送附件：系统临时目录 + 发送时才入库 ─────────────────────────────
async def test_stage_keeps_file_out_of_media_store(media):
    """选中的附件只放在系统临时目录，没有进媒体库、也没有落盘到 blobs/。"""
    rec = await media.stage(b'hello', name='a.txt', mime='text/plain')
    assert rec is not None
    assert rec.pending is True
    assert rec.id.startswith('p')
    # 正文在系统临时目录里，而不是媒体库的 blobs/ 下
    assert media.dir not in rec.path.parents
    assert rec.path.read_bytes() == b'hello'
    assert (await media.stats())['files'] == 0
    assert (await media.stats())['pending'] == 1
    # pending 时按 id 也能取回（缩略图预览依赖它）
    assert await media.resolve(rec.id) is not None


async def test_commit_moves_staged_file_into_media_store(media):
    """发送那一刻 commit：按内容 md5 入库，临时文件被收走。"""
    rec = await media.stage(b'payload', name='报告.txt', mime='text/plain')
    assert rec is not None
    assert rec.pending is True

    stored = await media.commit(rec.id)
    assert stored is not None
    assert stored.pending is False
    assert stored.id == hashlib.md5(b'payload').hexdigest()
    assert stored.name == '报告.txt'
    assert stored.path.read_bytes() == b'payload'
    # 临时文件已不在原处，媒体库里有了一份
    assert not rec.path.exists()
    assert (await media.stats())['files'] == 1
    assert (await media.stats())['pending'] == 0
    # commit 幂等：已入库的 id 直接返回
    again = await media.commit(stored.id)
    assert again is not None
    assert again.id == stored.id


async def test_cancel_and_delete_staged_file(media):
    """点「移除」时删掉临时文件；已入库的资源删除走媒体库。"""
    rec = await media.stage(b'gone', name='a.bin')
    assert rec is not None
    assert await media.cancel(rec.id) is True
    assert not rec.path.exists()
    assert (await media.stats())['pending'] == 0
    # 再删一次已经没了
    assert await media.cancel(rec.id) is False
    assert await media.delete(rec.id) is False


async def test_cleanup_reclaims_stale_staged_files(media):
    """选了却一直不发的附件会按 ttl 被回收（临时文件也删掉）。"""
    rec = await media.stage(b'stale', name='a.bin')
    assert rec is not None
    # ttl 100s（见 media 夹具的配置），把时间拨回过去即可触发
    media._pending[rec.id].created = time.time() - 200
    assert await media.cleanup() == 1
    assert (await media.stats())['pending'] == 0
    assert not rec.path.exists()


async def test_upload_then_send_commits_to_media_store(client: AsyncClient, media):
    """上传接口只暂存；发送后同一 id 变成内容 md5 的媒体库地址。"""
    from src.nonebot_plugin_botui import api

    png = b'\x89PNG\r\n\x1a\n' + b'z' * 16
    up = await client.post(
        '/botui/api/upload?name=pic.png&type=image/png',
        headers=HEADERS,
        content=png,
    )
    assert up.status_code == 200
    info = up.json()['file']
    assert info['pending'] is True

    if api._media is not None:
        stored = await api._media.commit(info['id'])
        assert stored is not None
        assert stored.id == hashlib.md5(png).hexdigest()
        got = await client.get(f'/botui/api/media/{stored.id}', headers=HEADERS)
        assert got.status_code == 200
        assert got.content == png
