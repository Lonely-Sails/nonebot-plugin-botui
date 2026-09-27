"""插件唯一的媒体库：机器人收到的、WebUI 上传的图片 / 语音 / 文件都放这里。

**为什么要有这个统一存储。** 之前有两套东西在做同一件事：``uploads.py`` 存
WebUI 上传的附件、``filecache.py`` 存收到的媒体。两套的布局、过期规则、取回
接口都不一样，于是出现三种地址（``/api/upload``、``/api/cache``、代理原链接），
「自己发的图」和「收到的图」走的是两条互不相干的保命路径 —— 一条断了就查不出
原因。现在合成一个：**一次落盘、一份目录、一张表、一个取回地址**。

**为什么用数据库而不是散落的 JSON。** 每个资源一个 ``meta.json`` 加一个全局
``index.json``，等于把索引状态摊在文件系统上：查询要遍历目录、并发要抢文件锁、
崩溃会留下半个索引（还得写扫盘重建的兜底代码）。这些正是 SQLite 擅长的事，
插件的消息本来就存在同一个库里 —— 媒体元数据表直接建在那条连接上，与消息共享
同一个事务和锁，不再有第二份需要同步的状态。

磁盘布局（``<数据目录>/blobs/<id 前两位>/<id>/<文件名>``）::

    blobs/
        aB/
            aBcD...xyz/            # 一个资源一个目录，便于整目录原子删除
                报告.pdf            # 正文，按原始文件名落盘
    blobs/_tmp/                    # 下载中的临时文件，不参与索引

文件名直接作为落盘名（而不是统一的 ``blob``）是有代价换来的：QQ 适配器发
本地文件时**取的就是磁盘文件名**，叫 ``blob`` 的话对方收到的是「未命名」。

引用（``refs`` 列，JSON 数组）是回收策略的关键：``msg:<会话>:<行号>`` 表示
「哪条聊天记录在用它」，``msgid:<消息 ID>`` 表示自己发出的那条。**有引用的资源
按 ``retention`` 保留，没有引用的按 ``ttl`` 回收** —— 否则聊天记录里的图会莫名
其妙变坏；反过来，选完却没发出去的附件也不会赖着不走。
"""

from __future__ import annotations

import re
import json
import time
import shutil
import asyncio
import secrets
import mimetypes
from typing import Any
from pathlib import Path
from dataclasses import dataclass

import aiosqlite
from nonebot import logger

#: 资源 id 允许的形状（本模块自己生成，取回时仍严格校验，杜绝路径穿越）
ID_PATTERN = r'^[A-Za-z0-9_-]{6,64}$'
_VALID_ID = re.compile(ID_PATTERN)
#: 从取回地址里抠出资源 id（``/api/media/<id>``）
_URL_ID = re.compile(r'/api/media/([A-Za-z0-9_-]{6,64})')

_TMP_DIR = '_tmp'
#: 目录按 id 前两位分桶，避免一个目录塞几万个文件夹
_BUCKET_LEN = 2
#: 落盘文件名（不含后缀）的最大字节数，留出余量避免超出文件系统上限
_DISK_NAME_MAX = 48

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


def disk_name(name: str) -> str:
    """落盘用的文件名：清洗后保留后缀、按字节截断。

    前缀截断很容易把一个多字节字符砍成半个，写盘时会直接抛 UnicodeEncodeError，
    所以按字节切完还要退到最近的合法 UTF-8 边界。
    """
    cleaned = safe_filename(name)
    stem, dot, suffix = cleaned.rpartition('.')
    if not dot or not stem or len(suffix) > 12:
        stem, suffix = cleaned, ''
    else:
        suffix = '.' + suffix
    budget = max(_DISK_NAME_MAX - len(suffix.encode('utf-8')), 1)
    data = stem.encode('utf-8')[:budget]
    while data:
        try:
            stem = data.decode('utf-8')
            break
        except UnicodeDecodeError:
            data = data[:-1]
    else:
        stem = 'file'
    return (stem or 'file') + suffix


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
            'url': url_of(self.id),
        }


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
        """挂到消息库的连接上，建表、清掉残留的临时文件并收编旧目录。"""
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
    def _dir(self, fid: str) -> Path:
        if not fid or not _VALID_ID.match(fid):
            raise ValueError('非法的资源 id')
        return self.dir / fid[:_BUCKET_LEN] / fid

    def _file(self, fid: str, name: str) -> Path:
        return self._dir(fid) / disk_name(name)

    def tmp_dir(self) -> Path:
        """给「先下载到临时文件再入库」用的目录（不会被当成资源）。"""
        path = self.dir / _TMP_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── 行 -> 记录 ──────────────────────────────────────────────────────
    def _row_to_record(self, row: aiosqlite.Row) -> MediaRecord | None:
        directory = self._dir(str(row['id']))
        if not directory.is_dir():
            return None
        files = [p for p in directory.iterdir() if p.is_file()]
        if not files:
            return None
        path = max(files, key=lambda p: p.stat().st_size)
        return MediaRecord(
            id=str(row['id']),
            name=str(row['name'] or path.name),
            size=int(row['size'] or 0) or path.stat().st_size,
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
        """按 id 取回资源（id 非法或文件已丢失时返回 None）。"""
        if not fid or not _VALID_ID.match(fid):
            return None
        row = await self._fetch('SELECT * FROM blobs WHERE id = ?', (fid,))
        return self._row_to_record(row) if row is not None else None

    async def lookup(self, key: str) -> MediaRecord | None:
        """按「来源链接」或「取回地址」取回资源，命中时刷新访问时间。

        同一个资源有两个门牌：``https://…``（来源）与 ``/api/media/<id>``
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

    async def delete(self, fid: str) -> bool:
        """删除一个资源（前端移除待发送的附件时用），返回是否真的删掉了。"""
        record = await self.get(fid)
        if record is None:
            return False
        await self._remove(fid)
        return True

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

    # ── 写入 ────────────────────────────────────────────────────────────
    async def save(
        self,
        data: bytes,
        *,
        name: str = 'file',
        mime: str = '',
        kind: str = '',
        source: str = '',
        ref: str = '',
        key: str = '',
    ) -> MediaRecord | None:
        """把一段字节存进媒体库（小文件走这里，不必先落临时文件）。"""
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
            tmp, name=name, mime=mime, kind=kind, source=source, ref=ref, key=key
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
        key: str = '',
    ) -> MediaRecord | None:
        """把一个临时文件移进媒体库；不适合保存时删掉临时文件并返回 None。

        ``key``（缺省取 ``source``）是去重键：已存在同键条目时只追加引用，新
        下载的临时文件直接丢弃，同一个资源不会在磁盘上存好几份。
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

        dedupe = str(key or source or '').strip()
        if dedupe:
            existing = await self._find(dedupe)
            if existing is not None:
                if ref:
                    await self.add_ref(existing.id, ref)
                src.unlink(missing_ok=True)
                return existing

        filename = safe_filename(name)
        mime = (mime or '').split(';')[0].strip() or guess_type(filename)
        fid = secrets.token_urlsafe(16)
        directory = self._dir(fid)
        target = directory / disk_name(filename)
        try:
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(src.replace, target)
        except OSError:
            # 跨设备（临时目录与媒体目录不在同一分区）时退回复制
            try:
                await asyncio.to_thread(shutil.copyfile, src, target)
                src.unlink(missing_ok=True)
            except OSError as e:
                shutil.rmtree(directory, ignore_errors=True)
                src.unlink(missing_ok=True)
                logger.debug(f'BotUI 写入媒体失败：{e}')
                return None

        now = time.time()
        refs = [ref] if ref else []
        await self._execute(
            'INSERT INTO blobs (id, name, mime, kind, size, source, refs, '
            'referenced, created, accessed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                fid,
                filename,
                mime,
                'image' if kind == 'image' else kind_of(filename, mime),
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
            refs = self._parse_refs(row['refs'] if row is not None else None)
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
            refs = self._parse_refs(row['refs'] if row is not None else None)
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
            refs = self._parse_refs(row['refs'])
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
    def _parse_refs(value: Any) -> list[str]:
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
        shutil.rmtree(self._dir(fid), ignore_errors=True)
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
        """按 ttl / retention 回收，并顺带处理配额与孤儿文件，返回删除数量。"""
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
        removed += await asyncio.to_thread(self._prune_orphans)
        if removed:
            logger.debug(f'BotUI 媒体库回收了 {removed} 个资源')
        return removed

    def _prune_orphans(self) -> int:
        """删掉磁盘上没有被记录的目录（写盘成功但入库失败之类留下的残渣）。"""
        if not self.dir.is_dir():
            return 0
        removed = 0
        for bucket in self.dir.iterdir():
            if not bucket.is_dir():
                continue
            for directory in bucket.iterdir():
                if directory.is_dir() and not any(directory.iterdir()):
                    shutil.rmtree(directory, ignore_errors=True)
                    removed += 1
        return removed

    async def clear(self, mode: str = 'all') -> dict[str, int]:
        """清理媒体库，返回 ``{'removed': n, 'bytes': n, 'files': n}``。

        - ``all``：全部清空；
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
    'disk_name',
    'guess_type',
    'id_from_url',
    'is_plugin_url',
    'kind_of',
    'ref_for',
    'ref_of_message_id',
    'safe_filename',
    'url_of',
]
