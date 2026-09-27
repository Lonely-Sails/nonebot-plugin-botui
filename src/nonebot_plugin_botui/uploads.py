"""WebUI 上传的附件：落盘、登记、取回与过期清理。

为什么需要单独落盘，而不是把上传的字节直接塞进 ``/api/send``：机器人在
发送前往往要先拿到文件内容（QQ 官方适配器要 ``file_data``、alconna 的
``File`` 段要路径或 URL），而 WebUI 的一次「选附件 → 预览 → 发送」是两步
操作。先把附件存成**服务端本地文件**，发送时只需引一个 id，既避免把文件
在浏览器里传来传去，也让发送请求体保持 JSON（前端用 fetch 直接发 body 流，
不用引入 multipart 依赖）。

磁盘布局（每个附件一个目录，便于原子化整目录清理）::

    <数据目录>/uploads/<id>/
        meta.json      # 原始文件名、大小、类型、创建/过期时间
        blob           # 附件正文（原始字节，不改名不转码）

安全约束：
- ``id`` 由本模块用 ``secrets.token_urlsafe`` 生成，取回时再用正则校验，
  杜绝 ``../`` 之类的路径穿越；
- 单个文件不超过 ``max_bytes``（由配置传入），写入前先查 ``Content-Length``；
- 附件带 TTL（默认 1 小时），过期的目录由 :meth:`UploadStore.cleanup` 删除，
  避免数据目录被无人认领的上传堆满。
"""

from __future__ import annotations

import json
import time
import shutil
import secrets
import mimetypes
from typing import Any
from pathlib import Path
from dataclasses import dataclass

from nonebot import logger

#: 附件 id 允许的形状（本模块自己生成，取回时仍严格校验）
ID_PATTERN = r'^[A-Za-z0-9_-]{6,64}$'

_META_NAME = 'meta.json'
_BLOB_NAME = 'blob'
_PART_SUFFIX = '.part'

#: 常见图片后缀（判断附件该按图片还是按文件发送）
IMAGE_SUFFIXES = frozenset(
    {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico', '.tif', '.tiff'}
)


def safe_filename(raw: Any, fallback: str = 'file') -> str:
    """把浏览器给的文件名清洗成可安全写盘的名字。

    只保留基名并剔掉路径分隔符与控制字符 —— 浏览器偶尔会带上完整路径
    （老 IE 的 ``C:\\path\\a.txt``），直接拿来拼路径就是路径穿越。
    """
    text = str(raw or '')
    # 统一分隔符后只取最后一段，Windows 路径也能处理
    text = text.replace('\\', '/').split('/')[-1]
    cleaned = ''.join(
        ch for ch in text if ch.isprintable() and ch not in '<>:"|?*\r\n\t'
    ).strip()
    cleaned = cleaned.strip('. ')
    if not cleaned:
        cleaned = fallback
    # 文件系统对单个文件名的长度有限制，长的部分交给 id 目录兜底
    return cleaned[:120]


def kind_of(name: str, mime: str = '') -> str:
    """判断附件该作为图片（``Image`` 段）还是普通文件（``File`` 段）发送。"""
    if mime.lower().startswith('image/'):
        return 'image'
    suffix = Path(name).suffix.lower()
    return 'image' if suffix in IMAGE_SUFFIXES else 'file'


def guess_type(name: str) -> str:
    """按文件名猜 MIME（猜不出返回空串，交由适配器自行判断）。"""
    return mimetypes.guess_type(name)[0] or ''


@dataclass(slots=True)
class UploadRecord:
    """一条已落盘的上传附件。"""

    id: str
    name: str
    size: int
    mime: str
    kind: str
    created: float
    expires: float
    path: Path
    used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'size': self.size,
            'mime': self.mime or None,
            'kind': self.kind,
            'created': self.created,
            'expires': self.expires,
            'used': self.used,
        }


class UploadStore:
    """上传附件的存储。整体是纯同步文件 IO，调用方放到线程里执行即可。"""

    def __init__(
        self,
        base: Path,
        max_bytes: int = 0,
        ttl: float = 3600.0,
        retention: float = 7 * 24 * 3600.0,
    ) -> None:
        self.base = Path(base)
        self.max_bytes = int(max_bytes)
        self.ttl = float(ttl)
        self.retention = float(retention)

    # ── 内部工具 ────────────────────────────────────────────────────────
    def _dir(self, uid: str) -> Path:
        """把 id 映射成目录路径，并挡住任何越界可能。"""
        import re

        if not re.fullmatch(ID_PATTERN, uid or ''):
            raise ValueError('非法的附件 id')
        path = (self.base / uid).resolve()
        root = self.base.resolve()
        if path.parent != root:
            raise ValueError('非法的附件 id')
        return path

    def _read_meta(self, uid: str) -> dict[str, Any] | None:
        try:
            directory = self._dir(uid)
        except ValueError:
            return None
        meta_file = directory / _META_NAME
        if not meta_file.is_file():
            return None
        try:
            data = json.loads(meta_file.read_text(encoding='utf-8'))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _record(self, uid: str, meta: dict[str, Any]) -> UploadRecord | None:
        blob = self.base / uid / _BLOB_NAME
        if not blob.is_file():
            return None
        name = str(meta.get('name') or 'file')
        return UploadRecord(
            id=uid,
            name=name,
            size=int(meta.get('size') or blob.stat().st_size),
            mime=str(meta.get('mime') or ''),
            kind=str(meta.get('kind') or kind_of(name)),
            created=float(meta.get('created') or 0.0),
            expires=float(meta.get('expires') or 0.0),
            path=blob,
            used=bool(meta.get('used')),
        )

    # ── 对外接口 ────────────────────────────────────────────────────────
    def save(
        self,
        data: bytes,
        name: str = 'file',
        mime: str = '',
        max_bytes: int | None = None,
    ) -> UploadRecord:
        """把一段字节存成附件并返回记录；超过体积上限时抛 :class:`ValueError`。"""
        limit = self.max_bytes if max_bytes is None else int(max_bytes)
        if limit > 0 and len(data) > limit:
            raise ValueError(
                f'文件过大（{len(data) / 1048576:.1f} MB，上限 {limit // 1048576} MB）'
            )
        if not data:
            raise ValueError('附件内容为空')

        filename = safe_filename(name)
        mime = (mime or '').split(';')[0].strip() or guess_type(filename)
        uid = secrets.token_urlsafe(16)
        directory = self.base / uid
        directory.mkdir(parents=True, exist_ok=True)

        now = time.time()
        record = UploadRecord(
            id=uid,
            name=filename,
            size=len(data),
            mime=mime,
            kind=kind_of(filename, mime),
            created=now,
            expires=now + self.ttl if self.ttl > 0 else 0.0,
            path=directory / _BLOB_NAME,
        )
        part = directory / (_BLOB_NAME + _PART_SUFFIX)
        try:
            part.write_bytes(data)
            part.replace(record.path)  # 先写临时文件再改名，避免读到半个文件
            (directory / _META_NAME).write_text(
                json.dumps(record.to_dict(), ensure_ascii=False),
                encoding='utf-8',
            )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return record

    def get(self, uid: str) -> UploadRecord | None:
        """按 id 取回附件（不存在或已过期则返回 None）。"""
        meta = self._read_meta(uid)
        if meta is None:
            return None
        if not meta.get('used'):
            # 没发出去的按 ttl 判过期；已发出的交给 cleanup 按 retention 回收，
            # 否则发完一小时后又点开聊天记录还看得到、再刷新就没了，很奇怪
            expires = float(meta.get('expires') or 0.0)
            if self.ttl > 0 and expires and expires < time.time():
                self.delete(uid)
                return None
        return self._record(uid, meta)

    def delete(self, uid: str) -> bool:
        """删除一个附件目录，返回是否真的删掉了东西。"""
        try:
            directory = self._dir(uid)
        except ValueError:
            return False
        if not directory.is_dir():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return True

    def mark_used(self, uid: str) -> None:
        """标记附件已经发出去了。

        发出后的附件不能再立刻删：聊天记录里刚记下的消息段还指向
        ``/api/upload?id=...``，马上删掉界面就会显示坏图。所以改为「延长保留
        时间」—— :meth:`cleanup` 对已发出的附件用 ``retention`` 而不是 ``ttl``。
        纯属尽力而为：写 meta 失败不影响发送本身。
        """
        meta = self._read_meta(uid)
        if meta is None:
            return
        meta['used'] = True
        meta['used_at'] = time.time()
        try:
            (self.base / uid / _META_NAME).write_text(
                json.dumps(meta, ensure_ascii=False), encoding='utf-8'
            )
        except Exception:  # pragma: no cover - 磁盘异常
            pass

    def cleanup(self) -> int:
        """清理过期（或残留的半个）附件目录，返回清理数量。

        「过期」分两种：**没发出去**的按 ``ttl``（默认 1 小时）算，选完不用就
        别占着磁盘；**已经发出去**的按 ``retention``（默认 7 天）算，让聊天
        记录里的图片能多显示几天。
        """
        if not self.base.is_dir():
            return 0
        now = time.time()
        removed = 0
        for directory in list(self.base.iterdir()):
            if not directory.is_dir():
                continue
            meta = self._read_meta(directory.name)
            if meta is None:
                # meta 缺失说明上次写入中途失败了（或残留的空目录），直接清掉
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
                continue
            expires = float(meta.get('expires') or 0.0)
            if meta.get('used'):
                # 已发出：从「发出时刻」起按 retention 计
                base = float(meta.get('used_at') or meta.get('created') or 0.0)
                if self.retention > 0 and base and base + self.retention < now:
                    shutil.rmtree(directory, ignore_errors=True)
                    removed += 1
            elif self.ttl > 0 and expires and expires < now:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        if removed:
            logger.debug(f'BotUI 清理了 {removed} 个过期上传附件')
        return removed
