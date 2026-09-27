"""收到的媒体与文件的本地缓存：链接失效后仍能在 WebUI 里打开。

机器人的媒体直链（QQ 的图片、语音、文件）大多带防盗链，而且**会过期**：
过一阵子再点开聊天记录，链接就已经 410/403 了。这里的做法是：收到消息时
就把可缓存的段（图片 / 文件 / 语音 / 音频 / 视频）下载到本地，之后 WebUI
一律读本地副本，不再依赖上游链接。

磁盘布局（与 ``uploads.py`` 保持同一套「一个资源一个目录」的形状，
便于整目录原子删除）::

    <cache 目录>/<kind>/<id 前两位>/<id>/
        meta.json      # 原名、类型、大小、来源链接、时间、引用列表
        blob           # 原始字节
    <cache 目录>/index.json   # 来源链接 / 缓存地址 -> 条目索引（丢了会自动重建）

``<kind>`` 只分 ``image`` 与 ``file`` 两类，纯粹为了让「图片缓存」可以直接
用文件管理器翻看；资源 id 是随机串，取回时会严格校验，杜绝路径穿越。

配额与回收（见 :class:`FileCache` 的构造参数）：

- ``max_bytes`` / ``max_files``：总量上限，超出后先淘汰**没人引用**的（LRU），
  仍超就淘汰最旧的 —— 界面上给了「清理缓存」入口，磁盘不会无限涨；
- ``ttl``：没有任何聊天记录引用、也没人再访问的资源，多久后自动删除；
- ``retention``：**被聊天记录引用**的资源最多再保留多久（默认 7 天）。

引用（``refs``）是这套回收策略的关键：``src:<链接>`` 表示「从这条链接取的」，
``msg:<chat_key>:<行号>`` 表示「哪条聊天记录在用它」。有 ``msg:`` 引用的
资源不会被 ttl 回收，否则聊天记录里的图会莫名其妙变坏。
"""

from __future__ import annotations

import re
import json
import time
import shutil
import secrets
import threading
from typing import Any
from pathlib import Path
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Iterator

from nonebot import logger

#: 资源 id 允许的形状（本模块自己生成，取回时仍严格校验）
ID_PATTERN = r'^[A-Za-z0-9_-]{6,64}$'
_VALID_ID = re.compile(ID_PATTERN)
#: 从缓存取回地址里抠出资源 id
_URL_ID = re.compile(r'/api/cache/([A-Za-z0-9_-]{6,64})')

_META_NAME = 'meta.json'
_BLOB_NAME = 'blob'
_PART_SUFFIX = '.part'
_INDEX_NAME = 'index.json'
_TMP_DIR = 'tmp'
#: 目录按 id 前两位分桶，避免一个目录塞几万个文件夹
_BUCKET_LEN = 2
#: 单个资源最多记多少条引用（源链接 + 聊天记录）
MAX_REFS = 64

#: 可以（也值得）缓存到本地的消息段类型
CACHEABLE_TYPES = frozenset({'image', 'file', 'voice', 'audio', 'video'})

#: 源链接引用前缀（与聊天记录引用区分开）
_SRC_PREFIX = 'src:'

#: 文件 IO 的互斥锁：写入来自 ``asyncio.to_thread`` 的多个线程
_LOCK = threading.RLock()


@contextmanager
def _locked() -> Iterator[None]:
    with _LOCK:
        yield


def url_of(fid: str) -> str:
    """资源 id 对应的取回地址（延迟读配置，避免与 config 形成导入环）。"""
    if not fid:
        return ''
    from .config import plugin_config as cfg

    return f'{cfg.botui_route}/api/cache/{fid}'


def id_from_url(url: Any) -> str:
    """从消息段里的地址取出缓存资源 id（不是缓存地址时返回空串）。"""
    if not isinstance(url, str) or not url:
        return ''
    match = _URL_ID.search(url)
    return match.group(1) if match else ''


def kind_of(seg_type: str, mime: str = '', name: str = '') -> str:
    """决定一个段该缓存成 ``image`` 还是 ``file``。"""
    if seg_type == 'image':
        return 'image'
    if mime.lower().startswith('image/'):
        return 'image'
    from .uploads import kind_of as upload_kind

    return upload_kind(name, mime)


@dataclass(slots=True)
class CachedFile:
    """一条已经落到本地磁盘的缓存资源。"""

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


class FileCache:
    """媒体/文件缓存。整体是纯同步文件 IO，调用方放到线程里执行即可。"""

    def __init__(
        self,
        base: Path,
        max_bytes: int = 0,
        max_files: int = 0,
        ttl: float = 0.0,
        retention: float = 7 * 24 * 3600.0,
        file_max_bytes: int = 0,
    ) -> None:
        self.base = Path(base)
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.ttl = float(ttl)
        self.retention = float(retention)
        #: 单个资源超过这个大小就不缓存（0 表示只受总量限制）
        self.file_max_bytes = int(file_max_bytes)
        #: key(来源链接 / 缓存地址) -> 条目；懒加载，首次访问时从磁盘重建
        self._index: dict[str, dict[str, Any]] | None = None
        #: 本次进程内的运行统计（重启归零，界面上给个近似值即可）
        self._counters: dict[str, int] = {
            'fetched': 0,
            'hits': 0,
            'misses': 0,
            'dropped': 0,
        }

    # ── 基本信息 ────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        """是否有配额（两个上限都为 0 表示不缓存）。"""
        return self.max_bytes > 0 or self.max_files > 0

    def allows(self, size: int) -> bool:
        """单个资源是否值得缓存。"""
        if not self.enabled or size <= 0:
            return False
        if self.file_max_bytes > 0 and size > self.file_max_bytes:
            return False
        if self.max_bytes > 0 and size > self.max_bytes:
            return False
        return True

    def reset_stats(self) -> None:
        """把「本次运行」的统计清零（界面上的重置按钮用）。"""
        for key in self._counters:
            self._counters[key] = 0

    # ── 索引 ────────────────────────────────────────────────────────────
    def _norm_key(self, key: str) -> str:
        """把来源链接 / 缓存地址归一到索引键。

        同一个资源有两个「门牌」：**来源链接**（``https://…``）与**缓存地址**
        （``/botui/api/cache/<id>``）。消息段里存的是后者，而去重时比对的是
        前者，两种情况都要能查到同一条目，所以统一在这里归一。
        """
        text = str(key or '').strip()
        if not text:
            return ''
        fid = id_from_url(text)
        return f'cache:{fid}' if fid else text

    def _dir_path(self, kind: str, fid: str) -> Path:
        return self.base / kind / fid[:_BUCKET_LEN] / fid

    def _iter_meta_files(self) -> Iterator[Path]:
        if not self.base.is_dir():
            return
        pattern = '*/' + ('*' * _BUCKET_LEN) + '/*/' + _META_NAME
        for path in self.base.glob(pattern):
            if path.is_file():
                yield path

    def _read_meta_file(self, meta_file: Path) -> dict[str, Any] | None:
        try:
            meta = json.loads(meta_file.read_text(encoding='utf-8'))
        except Exception:
            return None
        if not isinstance(meta, dict):
            return None
        fid = str(meta.get('id') or meta_file.parent.name)
        if not _VALID_ID.match(fid):
            return None
        refs = [str(r) for r in (meta.get('refs') or []) if str(r)]
        return {
            'id': fid,
            'name': str(meta.get('name') or 'file'),
            'mime': str(meta.get('mime') or ''),
            'kind': 'image' if str(meta.get('kind')) == 'image' else 'file',
            'size': int(meta.get('size') or 0),
            'source': str(meta.get('source') or ''),
            'created': float(meta.get('created') or 0.0),
            'accessed': float(meta.get('accessed') or meta.get('created') or 0.0),
            'refs': refs[:MAX_REFS],
            'key': str(meta.get('key') or ''),
        }

    def _scan(self) -> dict[str, dict[str, Any]]:
        """扫描磁盘上的 ``meta.json`` 重建索引。

        索引的键必须与平时写入的一致（缓存地址 + 来源链接），否则重建后
        ``lookup`` / ``add_ref`` 会找不到条目 —— 只有 ``get(id)`` 还能用。
        """
        found: dict[str, dict[str, Any]] = {}
        for meta_file in self._iter_meta_files():
            entry = self._read_meta_file(meta_file)
            if entry is None or not (meta_file.parent / _BLOB_NAME).is_file():
                continue
            found[f'cache:{entry["id"]}'] = entry
            source = str(entry.get('source') or '')
            if source:
                found.setdefault(self._norm_key(source), entry)
            key = str(entry.get('key') or '')
            if key:
                found.setdefault(self._norm_key(key), entry)
        return found

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if self._index is not None:
            return self._index
        index: dict[str, dict[str, Any]] = {}
        index_file = self.base / _INDEX_NAME
        if index_file.is_file():
            try:
                raw = json.loads(index_file.read_text(encoding='utf-8'))
            except Exception:
                raw = None
            if isinstance(raw, dict):
                for key, value in raw.items():
                    fid = str(value.get('id') or '') if isinstance(value, dict) else ''
                    if _VALID_ID.match(fid):
                        index[str(key)] = value
        # 索引丢了（首次运行 / 被写坏 / 用户手删过）就扫盘重建：缓存能自愈
        if not index:
            index = self._scan()
        self._index = index
        return index

    def _entries(self) -> list[dict[str, Any]]:
        """去重后的全部条目（同一资源可能在索引里有多个键）。"""
        seen: dict[str, dict[str, Any]] = {}
        for entry in self._load_index().values():
            seen[str(entry.get('id') or '')] = entry
        return [e for k, e in seen.items() if k]

    def _flush_index(self) -> None:
        index = self._index
        if index is None:
            return
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            (self.base / _INDEX_NAME).write_text(
                json.dumps(index, ensure_ascii=False), encoding='utf-8'
            )
        except Exception as e:  # pragma: no cover - 磁盘异常
            logger.debug(f'BotUI 写缓存索引失败：{e}')

    def _write_meta(self, entry: dict[str, Any]) -> None:
        directory = self._dir_path(str(entry['kind']), str(entry['id']))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / _META_NAME).write_text(
                json.dumps(entry, ensure_ascii=False), encoding='utf-8'
            )
        except Exception as e:  # pragma: no cover - 磁盘异常
            logger.debug(f'BotUI 写缓存元信息失败：{e}')

    def _blob_of(self, entry: dict[str, Any]) -> Path:
        directory = self._dir_path(str(entry['kind']), str(entry['id']))
        return directory / _BLOB_NAME

    def _record(self, entry: dict[str, Any]) -> CachedFile | None:
        blob = self._blob_of(entry)
        if not blob.is_file():
            return None
        return CachedFile(
            id=str(entry['id']),
            name=str(entry.get('name') or 'file'),
            size=int(entry.get('size') or blob.stat().st_size),
            mime=str(entry.get('mime') or ''),
            kind=str(entry.get('kind') or 'file'),
            source=str(entry.get('source') or ''),
            created=float(entry.get('created') or 0.0),
            accessed=float(entry.get('accessed') or 0.0),
            path=blob,
        )

    # ── 读取 ────────────────────────────────────────────────────────────
    def lookup(self, key: str) -> CachedFile | None:
        """按来源链接（或缓存地址）取回缓存，命中时刷新访问时间。"""
        norm = self._norm_key(key)
        if not norm:
            return None
        with _locked():
            index = self._load_index()
            entry = index.get(norm)
            record = self._record(entry) if entry is not None else None
            if record is None:
                if entry is not None:
                    index.pop(norm, None)
                self._counters['misses'] += 1
                return None
            self._counters['hits'] += 1
            entry['accessed'] = time.time()
            self._write_meta(entry)
            self._flush_index()
            return record

    def get(self, fid: str) -> CachedFile | None:
        """按 id 取回缓存资源（不存在返回 None）。"""
        if not fid or not _VALID_ID.match(fid):
            return None
        with _locked():
            for entry in self._entries():
                if str(entry.get('id')) == fid:
                    return self._record(entry)
        return None

    def has(self, key: str) -> bool:
        norm = self._norm_key(key)
        if not norm:
            return False
        with _locked():
            entry = self._load_index().get(norm)
            return entry is not None and self._record(entry) is not None

    # ── 引用 ────────────────────────────────────────────────────────────
    @staticmethod
    def _is_referenced(entry: dict[str, Any]) -> bool:
        """是否被某条聊天记录引用（``src:`` 只是「从哪取的」，不算引用）。"""
        for ref in entry.get('refs') or []:
            if not ref.startswith(_SRC_PREFIX):
                return True
        return False

    @staticmethod
    def _src_ref(source: str) -> str:
        """源链接引用标记（没有来源时返回空串）。"""
        return f'{_SRC_PREFIX}{source}' if source else ''

    @staticmethod
    def _add_ref(entry: dict[str, Any], ref: str) -> None:
        if not ref:
            return
        refs: list[str] = entry.setdefault('refs', [])
        if ref in refs:
            return
        refs.append(ref)
        if len(refs) > MAX_REFS:
            del refs[: len(refs) - MAX_REFS]

    @staticmethod
    def _drop_ref(entry: dict[str, Any], ref: str) -> None:
        refs: list[str] = entry.get('refs') or []
        if ref in refs:
            refs.remove(ref)

    def add_ref(self, key: str, ref: str) -> bool:
        """给某个缓存资源追加一条引用（资源不在缓存里时静默跳过）。"""
        norm = self._norm_key(key)
        if not norm or not ref:
            return False
        with _locked():
            index = self._load_index()
            entry = index.get(norm)
            if entry is None or self._record(entry) is None:
                return False
            self._add_ref(entry, ref)
            self._write_meta(entry)
            self._flush_index()
        return True

    def drop_ref(self, key: str, ref: str) -> bool:
        """撤销一条引用（消息被删掉时用）。"""
        norm = self._norm_key(key)
        if not norm or not ref:
            return False
        with _locked():
            entry = self._load_index().get(norm)
            if entry is None:
                return False
            self._drop_ref(entry, ref)
            self._write_meta(entry)
            self._flush_index()
        return True

    # ── 写入 ────────────────────────────────────────────────────────────
    def tmp_dir(self) -> Path:
        """给「先下载到临时文件再入库」用的目录（不会被当成缓存条目）。"""
        path = self.base / _TMP_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    def put(
        self,
        src: Path,
        *,
        name: str = 'file',
        mime: str = '',
        kind: str = 'file',
        source: str = '',
        ref: str = '',
        key: str = '',
    ) -> CachedFile | None:
        """把一个已下载好的临时文件移进缓存；不适合缓存时删除临时文件并返回 None。

        ``key`` 是去重键（一般是原始链接）：已存在同键条目时只追加引用，
        新下载的临时文件直接丢弃，避免同一个图片在磁盘上存好几份。
        """
        src = Path(src)
        norm = self._norm_key(key or source)
        try:
            size = src.stat().st_size
        except OSError:
            return None
        if not self.allows(size):
            self._counters['dropped'] += 1
            src.unlink(missing_ok=True)
            return None

        with _locked():
            now = time.time()
            index = self._load_index()
            entry = index.get(norm) if norm else None
            if entry is not None and self._record(entry) is not None:
                entry['accessed'] = now
                self._add_ref(entry, ref or self._src_ref(source))
                self._write_meta(entry)
                self._flush_index()
                self._counters['hits'] += 1
                src.unlink(missing_ok=True)
                return self._record(entry)

            fid = secrets.token_urlsafe(16)
            kind = 'image' if kind == 'image' else 'file'
            directory = self._dir_path(kind, fid)
            try:
                directory.mkdir(parents=True, exist_ok=True)
                src.replace(directory / _BLOB_NAME)
            except OSError:
                # 跨设备（临时目录与缓存目录不在同一分区）时退回复制
                try:
                    shutil.copyfile(src, directory / _BLOB_NAME)
                    src.unlink(missing_ok=True)
                except Exception as e:
                    shutil.rmtree(directory, ignore_errors=True)
                    src.unlink(missing_ok=True)
                    logger.debug(f'BotUI 写入缓存失败：{e}')
                    return None

            new_entry: dict[str, Any] = {
                'id': fid,
                'name': name or 'file',
                'mime': mime or '',
                'kind': kind,
                'size': size,
                'source': source or '',
                'created': now,
                'accessed': now,
                'refs': [],
                'key': norm if norm and not norm.startswith('cache:') else '',
            }
            self._add_ref(new_entry, ref or self._src_ref(source))
            if norm:
                index[norm] = new_entry
            # 缓存地址也要能被查到（消息段里存的就是它）
            index[f'cache:{fid}'] = new_entry
            self._write_meta(new_entry)
            self._counters['fetched'] += 1
            self._evict()
            self._flush_index()
            return self._record(new_entry)

    def save(self, data: bytes, **kwargs: Any) -> CachedFile | None:
        """把一段字节存进缓存（小文件走这里，不必先落临时文件）。"""
        if not data or not self.allows(len(data)):
            if data:
                self._counters['dropped'] += 1
            return None
        tmp = self.tmp_dir() / f'{secrets.token_urlsafe(8)}{_PART_SUFFIX}'
        try:
            tmp.write_bytes(data)
        except Exception as e:  # pragma: no cover - 磁盘异常
            logger.debug(f'BotUI 写缓存临时文件失败：{e}')
            tmp.unlink(missing_ok=True)
            return None
        return self.put(tmp, **kwargs)

    # ── 淘汰与清理 ──────────────────────────────────────────────────────
    def _remove(self, entry: dict[str, Any]) -> None:
        shutil.rmtree(
            self._dir_path(str(entry['kind']), str(entry['id'])), ignore_errors=True
        )
        index = self._index
        if index is not None:
            fid = str(entry.get('id') or '')
            for key in [
                k for k, v in index.items() if str(v.get('id') or '') == fid
            ]:
                index.pop(key, None)
        self._counters['dropped'] += 1

    def _over_quota(self) -> bool:
        if self.max_files > 0 and self.total_files() > self.max_files:
            return True
        if self.max_bytes > 0 and self.total_bytes() > self.max_bytes:
            return True
        return False

    def _evict(self) -> int:
        """超出配额时按「先删没人引用的、再删最旧的」淘汰，返回删除数量。

        被聊天记录引用的资源尽量留着 —— 删掉它，聊天里的图就真的坏了；
        但配额是硬约束，实在超了就只好牺牲最旧的那批（记一条 debug 日志）。
        """
        if not self._over_quota():
            return 0
        removed = 0
        for protect_referenced in (True, False):
            while self._over_quota():
                candidates = [
                    e
                    for e in self._entries()
                    if self._is_referenced(e) != protect_referenced
                ]
                if not candidates:
                    break
                oldest = min(candidates, key=lambda e: float(e.get('accessed') or 0.0))
                if not protect_referenced:
                    logger.debug('BotUI 缓存超出配额，删除了仍被聊天记录引用的最旧资源')
                self._remove(oldest)
                removed += 1
        return removed

    def cleanup(self) -> int:
        """按 ttl / retention 回收缓存，并顺带处理配额，返回删除数量。"""
        now = time.time()
        removed = 0
        with _locked():
            self._load_index()
            for entry in self._entries():
                base = float(entry.get('accessed') or entry.get('created') or 0.0)
                if not base:
                    continue
                if self._is_referenced(entry):
                    if self.retention > 0 and base + self.retention < now:
                        self._remove(entry)
                        removed += 1
                elif self.ttl > 0 and base + self.ttl < now:
                    self._remove(entry)
                    removed += 1
            removed += self._evict()
            # 清掉写入中途留下的临时文件
            shutil.rmtree(self.base / _TMP_DIR, ignore_errors=True)
            self._flush_index()
        if removed:
            logger.debug(f'BotUI 缓存回收了 {removed} 个资源')
        return removed

    def clear(self, mode: str = 'all') -> dict[str, int]:
        """清理缓存，返回 ``{'removed': n, 'bytes': n, 'files': n}``。

        - ``all``：整个缓存目录清空；
        - ``orphans``：只删**没有被任何聊天记录引用**的资源（靠 meta 里的
          ``refs`` 判定，不必再扫一遍数据库）；
        - ``image`` / ``file``：只清某一类。
        """
        removed = 0
        freed = 0
        with _locked():
            if mode == 'all':
                for entry in self._entries():
                    freed += int(entry.get('size') or 0)
                    removed += 1
                shutil.rmtree(self.base, ignore_errors=True)
                self._index = {}
            else:
                for entry in self._entries():
                    if mode == 'orphans' and self._is_referenced(entry):
                        continue
                    if mode in ('image', 'file') and str(entry.get('kind')) != mode:
                        continue
                    freed += int(entry.get('size') or 0)
                    self._remove(entry)
                    removed += 1
                self._flush_index()
        if removed:
            freed_mb = freed / 1048576
            msg = f'BotUI 清理缓存：删除 {removed} 个资源（释放 {freed_mb:.1f} MB）'
            logger.info(msg)
        self.reset_stats()
        return {'removed': removed, 'bytes': freed, 'files': self.total_files()}

    # ── 统计 ────────────────────────────────────────────────────────────
    def total_files(self) -> int:
        return len(self._entries())

    def total_bytes(self) -> int:
        return sum(int(e.get('size') or 0) for e in self._entries())

    def stats(self) -> dict[str, Any]:
        """给界面用的缓存概况（含每类的条数与体积）。"""
        buckets = {'image': {'files': 0, 'bytes': 0}, 'file': {'files': 0, 'bytes': 0}}
        referenced = 0
        for entry in self._entries():
            kind = 'image' if str(entry.get('kind')) == 'image' else 'file'
            buckets[kind]['files'] += 1
            buckets[kind]['bytes'] += int(entry.get('size') or 0)
            if self._is_referenced(entry):
                referenced += 1
        return {
            'enabled': self.enabled,
            'dir': str(self.base),
            'files': self.total_files(),
            'bytes': self.total_bytes(),
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
    'CACHEABLE_TYPES',
    'ID_PATTERN',
    'MAX_REFS',
    'CachedFile',
    'FileCache',
    'id_from_url',
    'kind_of',
    'url_of',
]
