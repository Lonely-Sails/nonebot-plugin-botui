"""插件唯一的媒体库：机器人收到的、WebUI 上传的图片 / 语音 / 文件都放这里。

**为什么要有这个统一存储。** 之前有两套东西在做同一件事：``uploads.py`` 存
WebUI 上传的附件、``filecache.py`` 存收到的媒体。两套的布局、过期规则、取回
接口都不一样，于是出现三种地址（``/api/upload``、``/api/cache``、代理原链接），
「自己发的图」和「收到的图」走的是两条互不相干的保命路径 —— 一条断了就查不出
原因。现在合成一个：**一次落盘、一份目录、一张表、一个取回地址**。

**为什么按内容（MD5）命名。** 资源 id 就是它内容的 MD5：文件按
``blobs/<前两位>/<md5>`` 落盘，消息里引用的地址是 ``/api/media/<md5>``。
内容相同的东西天然只有一份，不必再维护「同一个链接只存一次」的去重表，也不
会因为在两个消息里出现就各占一份磁盘。文件名与 MIME 属于「展示信息」，存在
数据库里（``name`` / ``names``），不参与落盘。

**为什么用数据库而不是散落的 JSON。** 每个资源一个 ``meta.json`` 加一个全局
``index.json``，等于把索引状态摊在文件系统上：查询要遍历目录、并发要抢文件锁、
崩溃会留下半个索引（还得写扫盘重建的兜底代码）。这些正是 SQLite 擅长的事，
插件的消息本来就存在同一个库里 —— 媒体元数据表直接建在那条连接上，与消息共享
同一个事务和锁，不再有第二份需要同步的状态。

**上传走临时区。** WebUI 选好附件到真正发送之间，文件只是「待发送」的：它
还不是聊天记录的一部分，也就不该进媒体库占配额、更不该被内容去重（同一个
文件改个名字重发是很正常的）。所以 :meth:`MediaStore.stage` 用 :mod:`tempfile`
把它放在系统临时目录，:meth:`MediaStore.cancel` 直接删掉；直到
:meth:`MediaStore.commit`（发送那一刻）才按内容 md5 搬进媒体库。如果用户选了
附件却没发，临时文件随进程结束或按 ttl 回收。

磁盘布局::

    blobs/
        aB/
            aBcD…xyz           # 正文，文件名就是内容的 md5
    blobs/_tmp/                # 下载中等候入库的临时文件，不参与索引

发送文件时为了不影响 QQ 适配器（它取的是**磁盘文件名**）而临时链接出一份带
真实文件名的副本，由调用方发完即删，见 ``api._send_path``。

引用（``refs`` 列，JSON 数组）是回收策略的关键：``msg:<会话>:<行号>`` 表示
「哪条聊天记录在用它」，``msgid:<消息 ID>`` 表示自己发出的那条。**有引用的资源
按 ``retention`` 保留，没有引用的按 ``ttl`` 回收** —— 否则聊天记录里的图会莫名
其妙变坏；反过来，选完却没发出去的附件也不会赖着不走。
"""

from __future__ import annotations

import os
import re
import json
import time
import shutil
import asyncio
import hashlib
import secrets
import tempfile
import mimetypes
from typing import Any
from pathlib import Path
from dataclasses import dataclass

import aiosqlite
from nonebot import logger

#: 资源 id 的形状：内容 MD5（32 位十六进制）。取回时严格校验，杜绝路径穿越。
ID_PATTERN = r'^[0-9a-f]{32}$'
_VALID_ID = re.compile(ID_PATTERN)
#: 从取回地址里抠出资源 id（``/api/media/<md5>``）
_URL_ID = re.compile(r'/api/media/([0-9a-f]{32})')

_TMP_DIR = '_tmp'
#: 目录按 id 前两位分桶，避免一个目录塞几万个文件
_BUCKET_LEN = 2

#: 可以（也值得）缓存到本地的消息段类型
MEDIA_TYPES = frozenset({'image', 'file', 'voice', 'audio', 'video'})

#: 引用前缀：聊天记录 / 已发出的消息
_MSG_PREFIX = 'msg:'
_MSGID_PREFIX = 'msgid:'

#: 常见图片后缀（决定附件该按图片还是按文件发送）
IMAGE_SUFFIXES = frozenset(
    {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico', '.tif', '.tiff'}
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    id          TEXT PRIMARY KEY,
    name        TEXT DEFAULT '',
    names       TEXT DEFAULT '[]',
    mime        TEXT DEFAULT '',
    kind        TEXT DEFAULT 'file',
    size        INTEGER DEFAULT 0,
    source      TEXT DEFAULT '',
    refs        TEXT DEFAULT '[]',
    referenced  INTEGER DEFAULT 0,
    created     REAL DEFAULT 0,
    accessed    REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_blobs_accessed ON blobs(accessed);
CREATE INDEX IF NOT EXISTS idx_blobs_source ON blobs(source);
"""


# ── 文件名与类型 ────────────────────────────────────────────────────────
def safe_filename(raw: Any, fallback: str = 'file') -> str:
    """把外部给的文件名清洗成可安全展示/落盘的名字。

    只保留基名并剔掉路径分隔符与控制字符：浏览器偶尔会带上完整路径
    （老 IE 的 ``C:\\path\\a.txt``），直接拿来拼路径就是路径穿越。
    """
    text = str(raw or '').replace('\\', '/').split('/')[-1]
    cleaned = ''.join(
        ch for ch in text if ch.isprintable() and ch not in '<>:"|?*\r\n\t'
    ).strip()
    cleaned = cleaned.strip('. ')
    return (cleaned or fallback)[:120]


def kind_of(name: str, mime: str = '') -> str:
    """判断资源该按图片还是按文件对待（决定发送时的段类型）。"""
    if mime.lower().startswith('image/'):
        return 'image'
    return 'image' if Path(name).suffix.lower() in IMAGE_SUFFIXES else 'file'


def guess_type(name: str) -> str:
    """按文件名猜 MIME（猜不出返回空串，交由适配器自行判断）。"""
    return mimetypes.guess_type(name)[0] or ''


# ── 地址 ────────────────────────────────────────────────────────────────
def url_of(fid: str) -> str:
    """资源 id 对应的取回地址（延迟读配置，避免与 config 形成导入环）。"""
    if not fid:
        return ''
    from .config import plugin_config as cfg

    return f'{cfg.botui_route}/api/media/{fid}'


def id_from_url(url: Any) -> str:
    """从消息段里的地址取出资源 id（不是本插件地址时返回空串）。"""
    if not isinstance(url, str) or not url:
        return ''
    match = _URL_ID.search(url)
    return match.group(1) if match else ''


def is_plugin_url(url: Any) -> bool:
    """是不是本插件自己的媒体地址（不必下载，但要登记引用）。"""
    if not isinstance(url, str) or not url.startswith('/'):
        return False
    from .config import plugin_config as cfg

    route = str(cfg.botui_route or '').rstrip('/')
    if not route:
        return False
    return url.startswith(f'{route}/api/media/')


def ref_for(chat_key: str, row_id: int) -> str:
    """聊天记录引用标记（``msg:<会话>:<行号>``）。"""
    if not chat_key or row_id <= 0:
        return ''
    return f'{_MSG_PREFIX}{chat_key}:{int(row_id)}'


def ref_of_message_id(message_id: Any) -> str:
    """按消息 ID 的引用标记（机器人发出的消息可能只有 message_id）。"""
    text = str(message_id or '').strip()
    return f'{_MSGID_PREFIX}{text}' if text else ''


def _is_real_ref(ref: str) -> bool:
    return ref.startswith(_MSG_PREFIX) or ref.startswith(_MSGID_PREFIX)


def _md5_of(path: Path) -> str:
    """分块计算文件内容的 MD5（大文件也不占内存）。"""
    digest = hashlib.md5()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class MediaRecord:
    """一条已经落到本地磁盘的媒体资源。"""

    id: str
    name: str
    size: int
    mime: str
    kind: str
    source: str
    created: float
    accessed: float
    path: Path
    #: 是否还处在「待发送」的临时区（尚未搬进媒体库）
    pending: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'size': self.size,
            'mime': self.mime or None,
            'kind': self.kind,
            'source': self.source or None,
            'created': self.created,
            'accessed': self.accessed,
            'pending': self.pending,
            'url': url_of(self.id),
        }


@dataclass(slots=True)
class PendingUpload:
    """一份「选了但还没发」的附件，正放在系统临时目录里。

    元数据只在内存里 —— 它不是聊天记录的一部分，重启后本就该丢；正文由
    :mod:`tempfile` 保管，取消或过期即删。
    """

    id: str
    name: str
    size: int
    mime: str
    kind: str
    created: float
    path: Path

    def to_record(self) -> MediaRecord:
        return MediaRecord(
            id=self.id,
            name=self.name,
            size=self.size,
            mime=self.mime,
            kind=self.kind,
            source='',
            created=self.created,
            accessed=self.created,
            path=self.path,
            pending=True,
        )


class MediaStore:
    """媒体资源的持久化与回收。

    跑在 ``MessageStore`` 的那条 SQLite 连接上（见 :meth:`attach`）：媒体元数据
    与消息记录因此共享同一个事务、同一把锁，不存在「两份状态谁先写」的问题。
    """

    def __init__(
        self,
        directory: Path,
        *,
        max_bytes: int = 0,
        max_files: int = 0,
        ttl: float = 0.0,
        retention: float = 7 * 24 * 3600.0,
        file_max_bytes: int = 0,
    ) -> None:
        self.dir = Path(directory)
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.ttl = float(ttl)
        self.retention = float(retention)
        #: 单个资源超过这个大小就不入库（0 表示只受总量限制）
        self.file_max_bytes = int(file_max_bytes)
        self._db: aiosqlite.Connection | None = None
        self._lock: asyncio.Lock | None = None
        #: 待发送附件（内存索引，正文在系统临时目录里，见 :meth:`stage`）
        self._pending: dict[str, PendingUpload] = {}
        #: 本次进程内的运行统计（重启归零，界面上给个近似值即可）
        self._counters: dict[str, int] = {
            'fetched': 0,
            'hits': 0,
            'misses': 0,
            'dropped': 0,
        }
        #: 最近一次刷新 accessed 的时间，避免每次读都写库
        self._touched: dict[str, float] = {}

    # ── 生命周期 ────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        """是否有配额（两个上限都为 0 表示不保存媒体）。"""
        return self.max_bytes > 0 or self.max_files > 0

    @property
    def ready(self) -> bool:
        return self._db is not None

    async def attach(self, db: aiosqlite.Connection, lock: asyncio.Lock) -> None:
        """挂到消息库的连接上，建表并清掉上次运行残留的临时文件。"""
        self._db = db
        self._lock = lock
        await db.executescript(SCHEMA)
        await db.commit()
        await asyncio.to_thread(self._prepare_dirs)

    def _prepare_dirs(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.dir / _TMP_DIR, ignore_errors=True)

    def allows(self, size: int) -> bool:
        """单个资源是否值得保存。"""
        if not self.enabled or size <= 0:
            return False
        if self.file_max_bytes > 0 and size > self.file_max_bytes:
            return False
        return not (self.max_bytes > 0 and size > self.max_bytes)

    def reset_stats(self) -> None:
        """把「本次运行」的统计清零（界面上的重置按钮用）。"""
        for key in self._counters:
            self._counters[key] = 0

    # ── 路径 ────────────────────────────────────────────────────────────
    def _path(self, fid: str) -> Path:
        if not fid or not _VALID_ID.match(fid):
            raise ValueError('非法的资源 id')
        return self.dir / fid[:_BUCKET_LEN] / fid

    def tmp_dir(self) -> Path:
        """给「先下载到临时文件再入库」用的目录（不会被当成资源）。"""
        path = self.dir / _TMP_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── 行 -> 记录 ──────────────────────────────────────────────────────
    def _row_to_record(self, row: aiosqlite.Row) -> MediaRecord | None:
        fid = str(row['id'])
        try:
            path = self._path(fid)
        except ValueError:  # pragma: no cover - 库里不该有非法 id
            return None
        if not path.is_file():
            return None
        return MediaRecord(
            id=fid,
            name=str(row['name'] or ''),
            size=int(row['size'] or 0),
            mime=str(row['mime'] or ''),
            kind='image' if str(row['kind']) == 'image' else 'file',
            source=str(row['source'] or ''),
            created=float(row['created'] or 0.0),
            accessed=float(row['accessed'] or 0.0),
            path=path,
        )

    async def _fetch(self, sql: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
        if self._db is None:
            return None
        async with self._db.execute(sql, params) as cur:
            return await cur.fetchone()

    # ── 读取 ────────────────────────────────────────────────────────────
    async def get(self, fid: str) -> MediaRecord | None:
        """按 id 取回媒体库里的资源（id 非法或文件已丢失时返回 None）。"""
        if not fid or not _VALID_ID.match(fid):
            return None
        row = await self._fetch('SELECT * FROM blobs WHERE id = ?', (fid,))
        return self._row_to_record(row) if row is not None else None

    def pending(self, fid: str) -> PendingUpload | None:
        """按 id 取一份待发送附件（只在内存索引里找）。"""
        return self._pending.get(str(fid or ''))

    async def resolve(self, fid: str) -> MediaRecord | None:
        """按 id 取回资源，媒体库与待发送临时区**都查**（取回接口用这个）。"""
        record = await self.get(fid)
        if record is not None:
            return record
        item = self._pending.get(str(fid or ''))
        return item.to_record() if item is not None else None

    async def lookup(self, key: str) -> MediaRecord | None:
        """按「来源链接」或「取回地址」取回资源，命中时刷新访问时间。

        同一个资源有两个门牌：``https://…``（来源）与 ``/api/media/<md5>``
        （消息段里存的地址）。消息段去重比对的是前者，读取时拿到的是后者，
        两种情况都要能查到同一条目。

        「命中 / 未命中」只在这里计数：``has`` 之类的内部存在性检查改走
        :meth:`_find`，免得一次「取回」被内部查询重复计成好几次。
        """
        record = await self._find(key)
        if record is None:
            self._counters['misses'] += 1
            return None
        self._counters['hits'] += 1
        await self._touch(record.id)
        return record

    async def _find(self, key: str) -> MediaRecord | None:
        """按来源链接或取回地址查一条记录（不计入命中统计、不刷新访问时间）。"""
        text = str(key or '').strip()
        if not text:
            return None
        fid = id_from_url(text)
        if fid:
            return await self.get(fid)
        row = await self._fetch(
            'SELECT * FROM blobs WHERE source = ? ORDER BY created DESC LIMIT 1',
            (text,),
        )
        return self._row_to_record(row) if row is not None else None

    async def has(self, key: str) -> bool:
        return await self._find(key) is not None

    async def _touch(self, fid: str) -> None:
        """刷新 accessed（同一分钟内只写一次，避免读图片就打一次库）。"""
        now = time.time()
        if now - self._touched.get(fid, 0.0) < 60.0:
            return
        self._touched[fid] = now
        if not self.ready:
            return
        assert self._db is not None
        assert self._lock is not None
        async with self._lock:
            await self._db.execute(
                'UPDATE blobs SET accessed = ? WHERE id = ?', (now, fid)
            )
            await self._db.commit()

    # ── 待发送附件（临时区） ────────────────────────────────────────────
    async def stage(
        self, data: bytes, *, name: str = 'file', mime: str = '', kind: str = ''
    ) -> MediaRecord | None:
        """把一份待发送的附件放进系统临时区，返回一个可直接预览 / 取回的 id。

        这里刻意**不**写进媒体库：还没发送的东西不该占配额，也不该被内容去重
        （同一个文件改名重发是常事）。真正的入库发生在 :meth:`commit`。
        """
        if not data or not self.enabled or not self.allows(len(data)):
            if data:
                self._counters['dropped'] += 1
            return None
        filename = safe_filename(name)
        mime = (mime or '').split(';')[0].strip() or guess_type(filename)
        uid = f'p{secrets.token_urlsafe(16)}'
        handle, raw_path = await asyncio.to_thread(
            tempfile.mkstemp, prefix='botui-upload-', suffix='.part'
        )
        path = Path(raw_path)
        try:
            await asyncio.to_thread(os.write, handle, data)
        except OSError as e:  # pragma: no cover - 磁盘异常
            logger.debug(f'BotUI 写上传临时文件失败：{e}')
            await asyncio.to_thread(os.close, handle)
            await asyncio.to_thread(path.unlink, True)
            return None
        await asyncio.to_thread(os.close, handle)
        item = PendingUpload(
            id=uid,
            name=filename,
            size=len(data),
            mime=mime,
            kind=kind if kind == 'image' else kind_of(filename, mime),
            created=time.time(),
            path=path,
        )
        self._pending[uid] = item
        return item.to_record()

    async def save(
        self,
        data: bytes,
        *,
        name: str = 'file',
        mime: str = '',
        kind: str = '',
        source: str = '',
        ref: str = '',
    ) -> MediaRecord | None:
        """把一段字节直接存进媒体库（测试与内部调用走这里）。"""
        if not data or not self.allows(len(data)):
            if data:
                self._counters['dropped'] += 1
            return None
        tmp = self.tmp_dir() / f'{secrets.token_urlsafe(8)}.part'
        try:
            await asyncio.to_thread(tmp.write_bytes, data)
        except OSError as e:  # pragma: no cover - 磁盘异常
            logger.debug(f'BotUI 写媒体临时文件失败：{e}')
            tmp.unlink(missing_ok=True)
            return None
        return await self.put(
            tmp, name=name, mime=mime, kind=kind, source=source, ref=ref
        )

    async def put(
        self,
        src: Path,
        *,
        name: str = 'file',
        mime: str = '',
        kind: str = '',
        source: str = '',
        ref: str = '',
    ) -> MediaRecord | None:
        """把一个临时文件按内容 md5 搬进媒体库；不适合保存时删掉并返回 None。

        落盘名就是内容 MD5，因此内容相同的文件天然共用一份；命中已有条目时
        只把「文件名 / 来源 / 引用」这些展示信息并进去，临时文件直接丢弃。
        """
        src = Path(src)
        try:
            size = src.stat().st_size
        except OSError:
            return None
        if not self.allows(size):
            self._counters['dropped'] += 1
            src.unlink(missing_ok=True)
            return None

        fid = await asyncio.to_thread(_md5_of, src)
        existing = await self.get(fid)
        if existing is not None:
            src.unlink(missing_ok=True)
            await self._merge(existing, name=name, mime=mime, kind=kind, source=source)
            if ref:
                await self.add_ref(fid, ref)
            return await self.get(fid)

        filename = safe_filename(name)
        mime = (mime or '').split(';')[0].strip() or guess_type(filename)
        kind = 'image' if kind == 'image' else kind_of(filename, mime)
        target = self._path(fid)
        try:
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            # 同分区时 replace 是原子的；跨设备（临时目录在别的挂载点）退回复制
            try:
                await asyncio.to_thread(os.replace, src, target)
            except OSError:
                await asyncio.to_thread(shutil.copyfile, src, target)
                src.unlink(missing_ok=True)
        except OSError as e:
            shutil.rmtree(target.parent, ignore_errors=True)
            src.unlink(missing_ok=True)
            logger.debug(f'BotUI 写入媒体失败：{e}')
            return None

        now = time.time()
        refs = [ref] if ref else []
        await self._execute(
            'INSERT OR REPLACE INTO blobs (id, name, names, mime, kind, size, source, '
            'refs, referenced, created, accessed) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                fid,
                filename,
                json.dumps([filename], ensure_ascii=False),
                mime,
                kind,
                size,
                str(source or ''),
                json.dumps(refs, ensure_ascii=False),
                1 if any(_is_real_ref(r) for r in refs) else 0,
                now,
                now,
            ),
        )
        self._counters['fetched'] += 1
        await self._evict()
        return await self.get(fid)

    async def commit(self, fid: str) -> MediaRecord | None:
        """把一份待发送附件从临时区搬进媒体库（按内容 md5 命名）。

        已经是媒体库里的资源时直接返回；返回 None 表示 id 不存在或已过期。
        """
        record = await self.get(fid)
        if record is not None:
            return record
        item = self._pending.pop(str(fid or ''), None)
        if item is None:
            return None
        return await self.put(item.path, name=item.name, mime=item.mime, kind=item.kind)

    async def cancel(self, fid: str) -> bool:
        """取消一份待发送附件（删掉临时文件），返回是否真的删掉了。"""
        item = self._pending.pop(str(fid or ''), None)
        if item is None:
            return False
        await asyncio.to_thread(item.path.unlink, True)
        return True

    async def discard_pending(self) -> int:
        """清空全部待发送附件（关闭插件时用，别把临时文件留在系统里）。"""
        items = list(self._pending.values())
        self._pending.clear()
        for item in items:
            await asyncio.to_thread(item.path.unlink, True)
        return len(items)

    async def _merge(
        self, record: MediaRecord, *, name: str, mime: str, kind: str, source: str
    ) -> None:
        """把新出现的展示信息并进已有条目（内容相同 = 同一个资源）。"""
        new_name = safe_filename(name)
        assert self._db is not None
        assert self._lock is not None
        async with self._lock:
            row = await self._fetch(
                'SELECT names, mime, kind, source FROM blobs WHERE id = ?',
                (record.id,),
            )
            if row is None:  # pragma: no cover - 并发删除
                return
            names = self._parse_list(row['names'])
            if new_name not in names:
                names.append(new_name)
            new_mime = str(row['mime'] or '') or (
                (mime or '').split(';')[0].strip() or guess_type(new_name)
            )
            new_kind = str(row['kind'] or '') or (
                'image' if kind == 'image' else kind_of(new_name, new_mime)
            )
            await self._db.execute(
                'UPDATE blobs SET names = ?, name = ?, mime = ?, kind = ?, source = ? '
                'WHERE id = ?',
                (
                    json.dumps(names, ensure_ascii=False),
                    record.name or new_name,
                    new_mime,
                    new_kind,
                    str(row['source'] or '') or str(source or ''),
                    record.id,
                ),
            )
            await self._db.commit()

    async def delete(self, fid: str) -> bool:
        """删除一条资源（前端移除待发送附件、或手动删除媒体时用）。"""
        if await self.cancel(fid):
            return True
        record = await self.get(fid)
        if record is None:
            return False
        await self._remove(fid)
        return True

    async def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        if not self.ready:
            return
        assert self._db is not None
        assert self._lock is not None
        async with self._lock:
            await self._db.execute(sql, params)
            await self._db.commit()

    # ── 引用 ────────────────────────────────────────────────────────────
    async def add_ref(self, key: str, ref: str) -> bool:
        """给某个资源追加一条引用（资源不存在时静默跳过）。"""
        if not ref:
            return False
        record = await self._find(key)
        if record is None:
            return False
        assert self._db is not None
        assert self._lock is not None
        async with self._lock:
            row = await self._fetch('SELECT refs FROM blobs WHERE id = ?', (record.id,))
            refs = self._parse_list(row['refs'] if row is not None else None)
            if ref in refs:
                return False
            refs.append(ref)
            await self._db.execute(
                'UPDATE blobs SET refs = ?, referenced = ? WHERE id = ?',
                (
                    json.dumps(refs, ensure_ascii=False),
                    1 if any(_is_real_ref(r) for r in refs) else 0,
                    record.id,
                ),
            )
            await self._db.commit()
        return True

    async def drop_ref(self, key: str, ref: str) -> bool:
        """撤销一条引用（消息被删掉时用）。"""
        record = await self._find(key)
        if record is None:
            return False
        assert self._db is not None
        assert self._lock is not None
        async with self._lock:
            row = await self._fetch('SELECT refs FROM blobs WHERE id = ?', (record.id,))
            refs = self._parse_list(row['refs'] if row is not None else None)
            if ref not in refs:
                return False
            refs.remove(ref)
            await self._db.execute(
                'UPDATE blobs SET refs = ?, referenced = ? WHERE id = ?',
                (
                    json.dumps(refs, ensure_ascii=False),
                    1 if any(_is_real_ref(r) for r in refs) else 0,
                    record.id,
                ),
            )
            await self._db.commit()
        return True

    async def drop_message_refs(self, chat_key: str, row_ids: list[int]) -> None:
        """删掉若干条聊天记录时，同步摘掉它们留下的引用。

        引用不跟着消息走的话，媒体会被永久当成「还有人用」，占着配额不放。
        """
        if not row_ids:
            return
        prefixes = [f'{_MSG_PREFIX}{chat_key}:{int(rid)}' for rid in row_ids]
        await self._drop_refs_matching(prefixes)

    async def drop_refs_for(self, chat_key: str) -> None:
        """删掉整个会话时，摘掉该会话的全部引用。"""
        prefix = f'{_MSG_PREFIX}{chat_key}:'
        await self._drop_refs_matching([prefix])

    async def _drop_refs_matching(self, prefixes: list[str]) -> None:
        if not self.ready:
            return
        assert self._db is not None
        rows = await self._all_rows()
        changed: list[tuple[str, str, int]] = []
        for row in rows:
            refs = self._parse_list(row['refs'])
            kept = [
                r for r in refs if not any(r == p or r.startswith(p) for p in prefixes)
            ]
            if len(kept) != len(refs):
                changed.append(
                    (
                        json.dumps(kept, ensure_ascii=False),
                        1 if any(_is_real_ref(r) for r in kept) else 0,
                        str(row['id']),
                    )
                )
        if not changed:
            return
        assert self._lock is not None
        async with self._lock:
            await self._db.executemany(
                'UPDATE blobs SET refs = ?, referenced = ? WHERE id = ?', changed
            )
            await self._db.commit()

    @staticmethod
    def _parse_list(value: Any) -> list[str]:
        if isinstance(value, str):
            try:
                value = json.loads(value or '[]')
            except ValueError:
                value = []
        if not isinstance(value, list):
            return []
        return [str(r) for r in value if r]

    # ── 淘汰与清理 ──────────────────────────────────────────────────────
    async def _all_rows(self) -> list[aiosqlite.Row]:
        if not self.ready:
            return []
        assert self._db is not None
        async with self._db.execute('SELECT * FROM blobs') as cur:
            return list(await cur.fetchall())

    async def _remove(self, fid: str) -> None:
        assert self._db is not None
        assert self._lock is not None
        async with self._lock:
            await self._db.execute('DELETE FROM blobs WHERE id = ?', (fid,))
            await self._db.commit()
        try:
            self._path(fid).unlink(missing_ok=True)
        except ValueError:  # pragma: no cover - 非法 id
            pass
        self._touched.pop(fid, None)
        self._counters['dropped'] += 1

    async def _totals(self) -> tuple[int, int]:
        if not self.ready:
            return 0, 0
        assert self._db is not None
        async with self._db.execute(
            'SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM blobs'
        ) as cur:
            row = await cur.fetchone()
        return (int(row['n']), int(row['b'])) if row is not None else (0, 0)

    async def _over_quota(self) -> bool:
        count, size = await self._totals()
        if self.max_files > 0 and count > self.max_files:
            return True
        return bool(self.max_bytes > 0 and size > self.max_bytes)

    async def _evict(self) -> int:
        """超出配额时按「先删没人引用的、再删最旧的」淘汰，返回删除数量。

        被聊天记录引用的资源尽量留着 —— 删掉它聊天里的图就真的坏了；但配额是
        硬约束，实在超了就只好牺牲最旧的那批（记一条 debug 日志）。
        """
        if not self.enabled or not await self._over_quota():
            return 0
        removed = 0
        for referenced in (0, 1):
            while await self._over_quota():
                rows = await self._all_rows()
                candidates = [
                    r for r in rows if int(r['referenced'] or 0) == referenced
                ]
                if not candidates:
                    break
                if referenced:
                    logger.debug('BotUI 媒体库超出配额，删除了仍被聊天记录引用的资源')
                oldest = min(candidates, key=lambda r: float(r['accessed'] or 0.0))
                await self._remove(str(oldest['id']))
                removed += 1
        return removed

    async def cleanup(self) -> int:
        """按 ttl / retention 回收，并顺带清掉过期的待发送附件，返回删除数量。"""
        if not self.ready:
            return 0
        now = time.time()
        removed = 0
        for row in await self._all_rows():
            fid = str(row['id'])
            base = float(row['accessed'] or row['created'] or 0.0)
            if not base:
                continue
            window = self.retention if int(row['referenced'] or 0) else self.ttl
            if window > 0 and base + window < now:
                await self._remove(fid)
                removed += 1
        removed += await self._evict()
        removed += await self._cleanup_pending(now)
        if removed:
            logger.debug(f'BotUI 媒体库回收了 {removed} 个资源')
        return removed

    async def _cleanup_pending(self, now: float) -> int:
        """回收「选了但一直没发」的临时附件（按 ttl）。"""
        if self.ttl <= 0:
            return 0
        removed = 0
        for uid, item in list(self._pending.items()):
            if item.created + self.ttl < now:
                self._pending.pop(uid, None)
                await asyncio.to_thread(item.path.unlink, True)
                removed += 1
        return removed

    async def clear(self, mode: str = 'all') -> dict[str, int]:
        """清理媒体库，返回 ``{'removed': n, 'bytes': n, 'files': n}``。

        - ``all``：全部清空（含待发送临时附件）；
        - ``orphans``：只删**没有被任何聊天记录引用**的资源；
        - ``image`` / ``file``：只清某一类。
        """
        rows = await self._all_rows()
        removed = 0
        freed = 0
        for row in rows:
            if mode == 'orphans' and int(row['referenced'] or 0):
                continue
            if mode in ('image', 'file') and str(row['kind']) != mode:
                continue
            freed += int(row['size'] or 0)
            await self._remove(str(row['id']))
            removed += 1
        if mode == 'all':
            await self.discard_pending()
            await asyncio.to_thread(shutil.rmtree, self.dir, True)
            await asyncio.to_thread(self._prepare_dirs)
        if removed:
            freed_mb = freed / 1048576
            logger.info(
                f'BotUI 清理媒体库：删除 {removed} 个资源（释放 {freed_mb:.1f} MB）'
            )
        self.reset_stats()
        files, _ = await self._totals()
        return {'removed': removed, 'bytes': freed, 'files': files}

    # ── 统计 ────────────────────────────────────────────────────────────
    async def stats(self) -> dict[str, Any]:
        """给界面用的媒体库概况（含每类的条数与体积）。"""
        buckets = {'image': {'files': 0, 'bytes': 0}, 'file': {'files': 0, 'bytes': 0}}
        referenced = 0
        for row in await self._all_rows():
            kind = 'image' if str(row['kind']) == 'image' else 'file'
            buckets[kind]['files'] += 1
            buckets[kind]['bytes'] += int(row['size'] or 0)
            if int(row['referenced'] or 0):
                referenced += 1
        count, size = await self._totals()
        return {
            'enabled': self.enabled,
            'dir': str(self.dir),
            'files': count,
            'bytes': size,
            'images': buckets['image'],
            'others': buckets['file'],
            'referenced': referenced,
            'pending': len(self._pending),
            'max_bytes': self.max_bytes,
            'max_files': self.max_files,
            'file_max_bytes': self.file_max_bytes,
            'ttl': self.ttl,
            'retention': self.retention,
            'fetched': self._counters['fetched'],
            'hits': self._counters['hits'],
            'misses': self._counters['misses'],
            'dropped': self._counters['dropped'],
        }


__all__ = [
    'ID_PATTERN',
    'IMAGE_SUFFIXES',
    'MEDIA_TYPES',
    'MediaRecord',
    'MediaStore',
    'PendingUpload',
    'guess_type',
    'id_from_url',
    'is_plugin_url',
    'kind_of',
    'ref_for',
    'ref_of_message_id',
    'safe_filename',
    'url_of',
]
