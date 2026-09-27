"""基于 SQLite 的消息存储（写入队列 + 内存热缓存）。"""

from __future__ import annotations

import json
import time
import asyncio
from typing import Any
from pathlib import Path
from dataclasses import dataclass

import aiosqlite
from nonebot import logger

from .media import avatar_of
from .config import Config
from .models import (
    DIR_IN,
    KIND_GROUP,
    KIND_PRIVATE,
    BotRecord,
    ChatRecord,
    MessageRecord,
    now_ts,
    chat_key,
    self_id_of,
    split_chat_key,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    key             TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    chat_id         TEXT NOT NULL,
    adapter         TEXT DEFAULT '',
    scope           TEXT DEFAULT '',
    self_id         TEXT DEFAULT '',
    parent_id       TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    avatar          TEXT DEFAULT '',
    member_count    INTEGER,
    last_text       TEXT DEFAULT '',
    last_at         REAL DEFAULT 0,
    last_direction  TEXT DEFAULT '',
    message_count   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chats_last_at ON chats(last_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key    TEXT NOT NULL,
    chat_kind   TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    direction   TEXT NOT NULL,
    ts          REAL NOT NULL,
    adapter     TEXT DEFAULT '',
    scope       TEXT DEFAULT '',
    self_id     TEXT DEFAULT '',
    parent_id   TEXT DEFAULT '',
    chat_name   TEXT DEFAULT '',
    chat_avatar TEXT DEFAULT '',
    member_count INTEGER,
    user_id     TEXT DEFAULT '',
    user_name   TEXT DEFAULT '',
    user_avatar TEXT DEFAULT '',
    role        TEXT,
    is_self     INTEGER DEFAULT 0,
    text        TEXT DEFAULT '',
    segments    TEXT DEFAULT '[]',
    message_id  TEXT DEFAULT '',
    recallable  INTEGER DEFAULT 1,
    api         TEXT DEFAULT '',
    recalled    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_key, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages(message_id);

-- 会话里出现过的成员名册。
-- 目的是让 WebUI 的「@」能点选成员，而不是让用户自己敲 QQ 号；名册来自
-- 实际收到的消息（发送者）、@ 过的目标，以及 uninfo 拿到 @ 昵称的缓存。
CREATE TABLE IF NOT EXISTS members (
    chat_key    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    name        TEXT DEFAULT '',
    role        TEXT DEFAULT '',
    avatar      TEXT DEFAULT '',
    last_at     REAL DEFAULT 0,
    appearances INTEGER DEFAULT 0,
    PRIMARY KEY (chat_key, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_chat ON members(chat_key, last_at DESC);

-- 连接过的机器人账号。WebUI 右上角的切换器用它列出所有机器人并标记在线状态；
-- 机器人断开后记录仍然保留（online=0），所以「连接过的」都能看到。
CREATE TABLE IF NOT EXISTS bots (
    self_id     TEXT PRIMARY KEY,
    adapter     TEXT DEFAULT '',
    scope       TEXT DEFAULT '',
    name        TEXT DEFAULT '',
    avatar      TEXT DEFAULT '',
    online      INTEGER DEFAULT 0,
    first_seen  REAL DEFAULT 0,
    last_seen   REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bots_last_seen ON bots(last_seen DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 会话热缓存条数上限
_CHAT_CACHE_LIMIT = 2000

# 成员热缓存条数上限（跨所有会话的总条数，超出后丢掉最久没出现的）
_MEMBER_CACHE_LIMIT = 20000

# 单个会话内存里保留的成员数上限
_MEMBER_PER_CHAT_LIMIT = 500

# 写入消息时顺带更新会话摘要。
# 行拆得多是为了让每行都不超过 88 列（ruff 的 E501），同时保持列对齐便于阅读。
_UPSERT_CHAT_MESSAGE_SQL = """
INSERT INTO chats (key, kind, chat_id, adapter, scope, self_id, parent_id,
                   name, avatar, member_count, last_text, last_at,
                   last_direction, message_count)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
ON CONFLICT(key) DO UPDATE SET
    adapter        = COALESCE(NULLIF(excluded.adapter, ''), chats.adapter),
    scope          = COALESCE(NULLIF(excluded.scope, ''), chats.scope),
    self_id        = COALESCE(NULLIF(excluded.self_id, ''), chats.self_id),
    parent_id      = COALESCE(NULLIF(excluded.parent_id, ''), chats.parent_id),
    name           = COALESCE(NULLIF(excluded.name, ''), chats.name),
    avatar         = COALESCE(NULLIF(excluded.avatar, ''), chats.avatar),
    member_count   = COALESCE(excluded.member_count, chats.member_count),
    last_text      = excluded.last_text,
    last_at        = MAX(excluded.last_at, chats.last_at),
    last_direction = excluded.last_direction,
    message_count  = chats.message_count + 1
"""

# 更新会话资料（不动消息计数与摘要）
_UPSERT_CHAT_SQL = """
INSERT INTO chats (key, kind, chat_id, adapter, scope, self_id,
                   parent_id, name, avatar, member_count)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(key) DO UPDATE SET
    adapter      = COALESCE(NULLIF(excluded.adapter, ''), chats.adapter),
    scope        = COALESCE(NULLIF(excluded.scope, ''), chats.scope),
    self_id      = COALESCE(NULLIF(excluded.self_id, ''), chats.self_id),
    parent_id    = COALESCE(NULLIF(excluded.parent_id, ''), chats.parent_id),
    name         = COALESCE(NULLIF(excluded.name, ''), chats.name),
    avatar       = COALESCE(NULLIF(excluded.avatar, ''), chats.avatar),
    member_count = COALESCE(excluded.member_count, chats.member_count)
"""


def _safe_json(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or '[]')
    except Exception:
        return []
    return data if isinstance(data, list) else []


@dataclass(slots=True)
class MemberRecord:
    """会话成员名册里的一项（用于 WebUI 的 @ 选择菜单）"""

    user_id: str
    name: str = ''
    role: str = ''
    avatar: str = ''
    last_at: float = 0.0
    appearances: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.user_id,
            'name': self.name or self.user_id,
            'role': self.role or None,
            'avatar': self.avatar or None,
            'last_at': self.last_at,
            'appearances': self.appearances,
        }


class MessageStore:
    """消息与会话的持久化存储。

    写入走内部队列，避免直接在事件钩子里阻塞；
    读取主要依赖内存中的会话热缓存 + SQLite 查询。
    """

    def __init__(self, config: Config, db_file: Path):
        self._cfg = config
        self._path = db_file
        # 路径通常来自 localstore（目录已建好）；这里再兜一次底，
        # 好让 store 也能直接用在任意路径上（例如测试里的 tmp_path）。
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None
        self._queue: asyncio.Queue[MessageRecord | None] = asyncio.Queue()
        self._writer: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._ready = False
        self._since_start = now_ts()
        # 会话热缓存：WebUI 的聊天列表直接读它
        self._chats: dict[str, ChatRecord] = {}
        # 会话成员热缓存：chat_key -> (user_id -> MemberRecord)
        self._members: dict[str, dict[str, MemberRecord]] = {}
        # 连接过的机器人：self_id -> BotRecord（在线状态由连接钩子维护）
        self._bots: dict[str, BotRecord] = {}
        # 每条记录落库后回调（用于推送给 WebUI 的增量事件流）
        self.on_insert: Any = None

    # ── 生命周期 ────────────────────────────────────────────────────────
    @property
    def path(self) -> Path:
        return self._path

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute('PRAGMA journal_mode=WAL')
        await self._db.execute('PRAGMA synchronous=NORMAL')
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._load_chats()
        self._writer = asyncio.create_task(self._write_loop())
        self._ready = True
        logger.debug(f'BotUI store ready at {self._path}')

    async def stop(self) -> None:
        if self._writer is not None:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._writer, timeout=5)
            except (TimeoutError, asyncio.TimeoutError):
                self._writer.cancel()
            self._writer = None
        if self._db is not None:
            try:
                await self._db.commit()
            finally:
                await self._db.close()
                self._db = None
        self._ready = False

    async def _load_chats(self) -> None:
        if self._db is None:
            return
        try:
            async with self._db.execute(
                'SELECT * FROM chats ORDER BY last_at DESC LIMIT ?',
                (_CHAT_CACHE_LIMIT,),
            ) as cur:
                rows = await cur.fetchall()
        except Exception as e:
            logger.warning(f'Failed to load chat cache: {e}')
            return
        # fetchall() 在类型上是 Iterable[Row]，len() 不接受；转成 list 即可，
        # 顺便让下面的循环有个具体容器
        rows = list(rows)
        for row in rows:
            chat = ChatRecord(
                key=row['key'],
                kind=row['kind'],
                chat_id=row['chat_id'],
                adapter=row['adapter'] or '',
                scope=row['scope'] or '',
                self_id=row['self_id'] or '',
                parent_id=row['parent_id'] or '',
                name=row['name'] or '',
                avatar=row['avatar'] or '',
                member_count=row['member_count'],
                last_text=row['last_text'] or '',
                last_at=row['last_at'] or 0.0,
                last_direction=row['last_direction'] or '',
                message_count=row['message_count'] or 0,
            )
            self._chats[chat.key] = chat
        logger.debug(f'Loaded {len(rows)} chat(s) into cache')
        await self._load_members()
        await self._load_bots()

    async def _load_bots(self) -> None:
        """把连接过的机器人读进内存（启动时一律先标记为离线）。

        在线状态由 ``Bot.on_connect`` / ``on_disconnect`` 钩子维护：进程刚起来
        时还没有任何连接，所以先全部置为离线，等钩子把当前连上的机器人点亮。
        """
        if self._db is None:
            return
        try:
            async with self._db.execute(
                'SELECT * FROM bots ORDER BY last_seen DESC LIMIT 200'
            ) as cur:
                rows = list(await cur.fetchall())
        except Exception as e:
            logger.debug(f'Failed to load bot cache: {e}')
            return
        for row in rows:
            self._bots[row['self_id']] = BotRecord(
                self_id=row['self_id'],
                adapter=row['adapter'] or '',
                scope=row['scope'] or '',
                name=row['name'] or '',
                avatar=row['avatar'] or '',
                online=False,
                first_seen=row['first_seen'] or 0.0,
                last_seen=row['last_seen'] or 0.0,
            )
        logger.debug(f'Loaded {len(self._bots)} bot(s) into cache')

    async def _load_members(self) -> None:
        """把最近活跃的成员名册读进内存（供 WebUI 的 @ 菜单用）"""
        if self._db is None:
            return
        try:
            async with self._db.execute(
                'SELECT * FROM members ORDER BY last_at DESC LIMIT ?',
                (_MEMBER_CACHE_LIMIT,),
            ) as cur:
                rows = list(await cur.fetchall())
        except Exception as e:
            logger.debug(f'Failed to load member cache: {e}')
            return
        for row in rows:
            bucket = self._members.setdefault(row['chat_key'], {})
            bucket[row['user_id']] = MemberRecord(
                user_id=row['user_id'],
                name=row['name'] or '',
                role=row['role'] or '',
                avatar=row['avatar'] or '',
                last_at=row['last_at'] or 0.0,
                appearances=row['appearances'] or 0,
            )
        logger.debug(f'Loaded {len(rows)} member(s) into cache')

    # ── 写入 ────────────────────────────────────────────────────────────
    def enqueue(self, record: MessageRecord) -> None:
        """把消息放进写入队列（同步、非阻塞）"""
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:  # pragma: no cover - 队列无上限
            logger.warning('BotUI write queue is full, dropping a message record')

    async def _write_loop(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            batch = [item]
            # 尽量批量落库，降低 IO 次数
            while len(batch) < 50:
                try:
                    nxt = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is None:
                    await self._flush(batch)
                    self._queue.task_done()
                    return
                batch.append(nxt)
            await self._flush(batch)
            for _ in batch:
                self._queue.task_done()

    async def _flush(self, batch: list[MessageRecord]) -> None:
        if self._db is None or not batch:
            return
        async with self._lock:
            try:
                for rec in batch:
                    await self._insert(rec)
                await self._db.commit()
            except Exception as e:
                logger.opt(exception=True).error(f'Failed to persist messages: {e}')

    async def _upsert_chat(self, rec: MessageRecord) -> None:
        assert self._db is not None
        preview = rec.preview
        await self._db.execute(
            _UPSERT_CHAT_MESSAGE_SQL,
            (
                rec.chat_key,
                rec.chat_kind,
                rec.chat_id,
                rec.adapter,
                rec.scope,
                rec.self_id,
                rec.parent_id,
                rec.chat_name,
                rec.chat_avatar,
                rec.member_count,
                preview[:500],
                rec.ts,
                rec.direction,
            ),
        )

    async def _upsert_members(self, rec: MessageRecord) -> None:
        """从一条消息里收集成员，写进名册（WebUI 的 @ 菜单靠它列人）。

        记两类人：消息的发送者，以及消息里 @ 到的目标。机器人自己也会进名册
        —— 群里 @ 机器人是很常见的需求。「全体成员」没有 user_id，跳过。
        """
        if self._db is None:
            return
        seen: dict[str, tuple[str, str, str]] = {}
        if rec.user_id:
            seen[rec.user_id] = (rec.user_name, rec.role or '', rec.user_avatar)
        for seg in rec.segments:
            if not isinstance(seg, dict) or seg.get('type') != 'at':
                continue
            target = str(seg.get('target') or '')
            if not target or target == 'all' or ':' in target:
                continue
            name = str(seg.get('name') or '')
            # 解析不到昵称时不要用「已有名字」以外的空串覆盖旧值
            seen.setdefault(target, (name, '', ''))
        if not seen:
            return

        rows = []
        for user_id, (name, role, avatar) in seen.items():
            rows.append(
                (
                    rec.chat_key,
                    user_id,
                    self._member_name(rec.chat_key, user_id) or name,
                    role,
                    avatar,
                    rec.ts,
                )
            )
            self._remember_member(
                rec.chat_key, user_id, name=name, role=role, avatar=avatar, ts=rec.ts
            )
        try:
            await self._db.executemany(
                """
                INSERT INTO members (chat_key, user_id, name, role, avatar,
                                     last_at, appearances)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(chat_key, user_id) DO UPDATE SET
                    name        = COALESCE(NULLIF(excluded.name, ''), members.name),
                    role        = COALESCE(NULLIF(excluded.role, ''), members.role),
                    avatar      = COALESCE(NULLIF(excluded.avatar, ''), members.avatar),
                    last_at     = MAX(excluded.last_at, members.last_at),
                    appearances = members.appearances + 1
                """,
                rows,
            )
        except Exception as e:  # pragma: no cover - 名册失败不影响消息落库
            logger.debug(f'Failed to upsert members for {rec.chat_key}: {e}')

    def _member_name(self, key: str, user_id: str) -> str:
        """内存里已有的成员名（避免 @ 解析结果被空串覆盖）"""
        member = self._members.get(key, {}).get(user_id)
        return member.name if member is not None else ''

    def _remember_member(
        self,
        key: str,
        user_id: str,
        *,
        name: str = '',
        role: str = '',
        avatar: str = '',
        ts: float = 0.0,
    ) -> MemberRecord:
        bucket = self._members.setdefault(key, {})
        member = bucket.get(user_id)
        if member is None:
            if len(bucket) >= _MEMBER_PER_CHAT_LIMIT:
                oldest = min(bucket.values(), key=lambda m: m.last_at)
                bucket.pop(oldest.user_id, None)
            member = MemberRecord(user_id=user_id)
            bucket[user_id] = member
        if name:
            member.name = name
        if role:
            member.role = role
        if avatar:
            member.avatar = avatar
        member.last_at = max(ts, member.last_at)
        member.appearances += 1
        return member

    async def _insert(self, rec: MessageRecord) -> None:
        assert self._db is not None
        await self._upsert_chat(rec)
        await self._upsert_members(rec)
        cursor = await self._db.execute(
            """
            INSERT INTO messages (chat_key, chat_kind, chat_id, direction, ts, adapter,
                                  scope, self_id, parent_id, chat_name, chat_avatar,
                                  member_count, user_id, user_name, user_avatar, role,
                                  is_self, text, segments, message_id, recallable, api)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.chat_key,
                rec.chat_kind,
                rec.chat_id,
                rec.direction,
                rec.ts,
                rec.adapter,
                rec.scope,
                rec.self_id,
                rec.parent_id,
                rec.chat_name,
                rec.chat_avatar,
                rec.member_count,
                rec.user_id,
                rec.user_name,
                rec.user_avatar,
                rec.role,
                1 if rec.is_self else 0,
                rec.text,
                json.dumps(rec.segments, ensure_ascii=False),
                rec.message_id,
                1 if rec.recallable else 0,
                rec.api,
            ),
        )
        rec.row_id = int(cursor.lastrowid or 0)

        # 更新内存热缓存
        key = rec.chat_key
        chat = self._chats.get(key)
        if chat is None:
            chat = ChatRecord(
                key=key, kind=rec.chat_kind, chat_id=rec.chat_id, self_id=rec.self_id
            )
            self._chats[key] = chat
            while len(self._chats) > _CHAT_CACHE_LIMIT:
                oldest = min(self._chats.values(), key=lambda c: c.last_at)
                self._chats.pop(oldest.key, None)
        chat.kind = rec.chat_kind or chat.kind
        chat.chat_id = rec.chat_id or chat.chat_id
        chat.adapter = rec.adapter or chat.adapter
        chat.scope = rec.scope or chat.scope
        chat.self_id = rec.self_id or chat.self_id
        chat.parent_id = rec.parent_id or chat.parent_id
        chat.name = rec.chat_name or chat.name
        chat.avatar = rec.chat_avatar or chat.avatar
        if rec.member_count is not None:
            chat.member_count = rec.member_count
        chat.last_text = rec.preview[:500]
        chat.last_at = max(rec.ts, chat.last_at)
        chat.last_direction = rec.direction
        chat.message_count += 1

        if self.on_insert is not None:
            try:
                self.on_insert(rec)
            except Exception as e:  # pragma: no cover - 推送失败不影响存储
                logger.debug(f'BotUI on_insert callback failed: {e}')

    async def flush(self) -> None:
        """等待队列中的写入完成（测试与关闭时使用）"""
        await self._queue.join()

    # ── 会话元信息 ──────────────────────────────────────────────────────
    async def touch_chat(
        self,
        key: str,
        *,
        name: str = '',
        avatar: str = '',
        member_count: int | None = None,
        adapter: str = '',
        scope: str = '',
        self_id: str = '',
        parent_id: str = '',
    ) -> None:
        """更新会话名称/头像等元信息（不产生消息记录）"""
        kind, chat_id = split_chat_key(key)
        chat = self._chats.get(key)
        if chat is None:
            chat = ChatRecord(
                key=key, kind=kind, chat_id=chat_id, self_id=self_id_of(key)
            )
            self._chats[key] = chat
        chat.name = name or chat.name
        chat.avatar = avatar or chat.avatar
        chat.adapter = adapter or chat.adapter
        chat.scope = scope or chat.scope
        chat.self_id = self_id or chat.self_id
        chat.parent_id = parent_id or chat.parent_id
        if member_count is not None:
            chat.member_count = member_count
        if self._db is None:
            return
        try:
            async with self._lock:
                await self._db.execute(
                    _UPSERT_CHAT_SQL,
                    (
                        key,
                        kind,
                        chat_id,
                        adapter,
                        scope,
                        self_id,
                        parent_id,
                        name,
                        avatar,
                        member_count,
                    ),
                )
                await self._db.commit()
        except Exception as e:
            logger.debug(f'Failed to touch chat {key}: {e}')

    # ── 读取 ────────────────────────────────────────────────────────────
    def chats(
        self, query: str = '', limit: int = 200, self_id: str | None = None
    ) -> list[ChatRecord]:
        """从热缓存里取会话列表（已按最后消息时间倒序）。

        ``self_id`` 不为 None 时只返回属于该机器人的会话。
        """
        items = sorted(self._chats.values(), key=lambda c: c.last_at, reverse=True)
        if self_id is not None:
            items = [c for c in items if c.self_id == self_id]
        if query:
            q = query.lower()
            items = [
                c
                for c in items
                if q in (c.name or '').lower()
                or q in c.chat_id.lower()
                or q in c.last_text.lower()
            ]
        return items[:limit]

    def chat(self, key: str) -> ChatRecord | None:
        return self._chats.get(key)

    # ── 机器人账号 ──────────────────────────────────────────────────────
    def bots(self) -> list[BotRecord]:
        """所有连接过的机器人，在线的排前面，其次按最近活跃倒序。"""
        items = sorted(
            self._bots.values(),
            key=lambda b: (b.online, b.last_seen),
            reverse=True,
        )
        return items

    def bot(self, self_id: str) -> BotRecord | None:
        return self._bots.get(str(self_id))

    async def upsert_bot(
        self,
        self_id: str,
        *,
        adapter: str = '',
        scope: str = '',
        name: str = '',
        avatar: str = '',
        online: bool | None = None,
    ) -> BotRecord:
        """记录/更新一个机器人账号，返回更新后的记录。

        ``online`` 为 None 表示不改动在线状态（例如只是补昵称）；字段为空的
        参数不会覆盖已有值（COALESCE 风格），免得一次降级的适配器信息把之前
        记下的昵称、头像抹掉。
        """
        self_id = str(self_id or '')
        if not self_id:
            raise ValueError('self_id is required')
        now = now_ts()
        record = self._bots.get(self_id)
        if record is None:
            record = BotRecord(self_id=self_id, first_seen=now, last_seen=now)
            self._bots[self_id] = record
        record.adapter = adapter or record.adapter
        record.scope = scope or record.scope
        record.name = name or record.name
        record.avatar = avatar or record.avatar
        if online is not None:
            record.online = bool(online)
        record.last_seen = max(now, record.last_seen)

        if self._db is None:
            return record
        try:
            async with self._lock:
                await self._db.execute(
                    """
                    INSERT INTO bots (self_id, adapter, scope, name, avatar,
                                      online, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(self_id) DO UPDATE SET
                        adapter = COALESCE(NULLIF(excluded.adapter, ''), bots.adapter),
                        scope = COALESCE(NULLIF(excluded.scope, ''), bots.scope),
                        name = COALESCE(NULLIF(excluded.name, ''), bots.name),
                        avatar = COALESCE(NULLIF(excluded.avatar, ''), bots.avatar),
                        online = excluded.online,
                        last_seen = MAX(excluded.last_seen, bots.last_seen)
                    """,
                    (
                        self_id,
                        record.adapter,
                        record.scope,
                        record.name,
                        record.avatar,
                        1 if record.online else 0,
                        record.first_seen,
                        record.last_seen,
                    ),
                )
                await self._db.commit()
        except Exception as e:  # pragma: no cover - 机器人信息写入失败不影响主流程
            logger.debug(f'Failed to upsert bot {self_id}: {e}')
        return record

    async def set_bot_online(self, self_id: str, online: bool) -> BotRecord | None:
        """更新机器人在线状态（连接钩子调用）"""
        self_id = str(self_id or '')
        if not self_id:
            return None
        return await self.upsert_bot(self_id, online=online)

    # ── 成员名册 ────────────────────────────────────────────────────────
    def members(
        self, key: str, query: str = '', limit: int = 200
    ) -> list[MemberRecord]:
        """某个会话里出现过的成员，按最近活跃排序。

        ``query`` 同时匹配昵称和 id，方便前端做「输入过滤 + 点选」，
        而不是让用户自己记住并敲 QQ 号。
        """
        items = sorted(
            self._members.get(key, {}).values(),
            key=lambda m: (m.last_at, m.appearances),
            reverse=True,
        )
        if query:
            q = query.strip().lower()
            items = [m for m in items if q in m.name.lower() or q in m.user_id.lower()]
        return items[: max(1, min(1000, int(limit)))]

    def member(self, key: str, user_id: str) -> MemberRecord | None:
        """按 id 取单个成员（没有就返回 None）"""
        return self._members.get(key, {}).get(str(user_id))

    async def rename_member(self, key: str, user_id: str, name: str) -> None:
        """给成员补一个名字（@ 解析出昵称后回填，省得下次再查）"""
        if not name:
            return
        self._remember_member(key, user_id, name=name)
        if self._db is None:
            return
        try:
            async with self._lock:
                await self._db.execute(
                    'INSERT INTO members (chat_key, user_id, name, last_at) '
                    'VALUES (?, ?, ?, ?) '
                    'ON CONFLICT(chat_key, user_id) DO UPDATE SET '
                    "name = CASE WHEN excluded.name != '' "
                    'THEN excluded.name ELSE members.name END',
                    (key, user_id, name, now_ts()),
                )
                await self._db.commit()
        except Exception as e:  # pragma: no cover - 回填失败不影响主流程
            logger.debug(f'Failed to rename member {key}/{user_id}: {e}')

    def _row_to_message(self, row: aiosqlite.Row) -> MessageRecord:
        keys = row.keys()
        return MessageRecord(
            row_id=int(row['id']),
            chat_key=row['chat_key'],
            chat_kind=row['chat_kind'],
            chat_id=row['chat_id'],
            direction=row['direction'],
            ts=float(row['ts']),
            adapter=row['adapter'] or '',
            scope=row['scope'] or '',
            self_id=row['self_id'] or '',
            parent_id=row['parent_id'] or '',
            chat_name=row['chat_name'] or '',
            chat_avatar=row['chat_avatar'] or '',
            member_count=row['member_count'] if 'member_count' in keys else None,
            user_id=row['user_id'] or '',
            user_name=row['user_name'] or '',
            user_avatar=row['user_avatar'] or '',
            role=row['role'],
            is_self=bool(row['is_self']),
            text=row['text'] or '',
            segments=_safe_json(row['segments']),
            message_id=row['message_id'] or '',
            recallable=bool(row['recallable']),
            api=row['api'] or '',
            recalled=bool(row['recalled']),
        )

    async def messages(
        self,
        key: str,
        limit: int = 50,
        before: float | None = None,
        before_id: int | None = None,
    ) -> list[MessageRecord]:
        """按时间正序返回某个会话的消息。

        ``before`` / ``before_id`` 用于翻页加载更早的记录，按 (ts, id) 严格向前，
        因此不会重复返回已经加载过的消息。
        """
        if self._db is None:
            return []
        limit = max(1, min(500, int(limit)))
        if before is None:
            sql = (
                'SELECT * FROM messages WHERE chat_key = ? AND recalled = 0 '
                'ORDER BY ts DESC, id DESC LIMIT ?'
            )
            params: tuple[Any, ...] = (key, limit)
        elif before_id is None:
            # 只给了时间：返回严格更早的消息
            sql = (
                'SELECT * FROM messages WHERE chat_key = ? AND recalled = 0 '
                'AND ts < ? ORDER BY ts DESC, id DESC LIMIT ?'
            )
            params = (key, before, limit)
        else:
            # 给了时间 + 行号：按 (ts, id) 严格向前，避免同一秒内的消息重复
            sql = (
                'SELECT * FROM messages WHERE chat_key = ? AND recalled = 0 '
                'AND (ts < ? OR (ts = ? AND id < ?)) '
                'ORDER BY ts DESC, id DESC LIMIT ?'
            )
            params = (key, before, before, before_id, limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        items = [self._row_to_message(r) for r in rows]
        items.reverse()
        return items

    async def stream_messages(
        self,
        key: str,
        limit: int = 2000,
        after: float | None = None,
        after_id: int | None = None,
    ) -> list[MessageRecord]:
        """按时间正序返回某会话的消息，用于「导出聊天记录」。

        与 :meth:`messages` 的区别是方向相反：这边从最早开始向后取，供导出时
        按顺序写入；``after`` / ``after_id`` 用于分批继续取下一页。
        """
        if self._db is None:
            return []
        limit = max(1, min(2000, int(limit)))
        if after is None:
            sql = (
                'SELECT * FROM messages WHERE chat_key = ? AND recalled = 0 '
                'ORDER BY ts ASC, id ASC LIMIT ?'
            )
            params: tuple[Any, ...] = (key, limit)
        elif after_id is None:
            sql = (
                'SELECT * FROM messages WHERE chat_key = ? AND recalled = 0 '
                'AND ts > ? ORDER BY ts ASC, id ASC LIMIT ?'
            )
            params = (key, after, limit)
        else:
            sql = (
                'SELECT * FROM messages WHERE chat_key = ? AND recalled = 0 '
                'AND (ts > ? OR (ts = ? AND id > ?)) '
                'ORDER BY ts ASC, id ASC LIMIT ?'
            )
            params = (key, after, after, after_id, limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_message(r) for r in rows]

    async def search(
        self,
        keyword: str,
        limit: int = 100,
        chat_key: str | None = None,
        self_id: str | None = None,
    ) -> list[MessageRecord]:
        """按关键词搜索消息内容（不区分大小写），按时间倒序返回。

        ``keyword`` 里的 ``%`` ``_`` 是 LIKE 的通配符，必须转义掉，否则用户
        搜一个 ``%`` 会把所有消息都捞出来。

        ``self_id`` 不为 None 时只在指定机器人的消息里搜：会话 key 以
        ``self_id:`` 开头，因此用前缀匹配即可。
        """
        if self._db is None:
            return []
        keyword = (keyword or '').strip()
        if not keyword:
            return []
        limit = max(1, min(500, int(limit)))
        # 转义 LIKE 元字符：先用 \ 转义，再声明 ESCAPE '\'
        escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        pattern = f'%{escaped}%'
        clauses = ['recalled = 0', "text LIKE ? ESCAPE '\\'"]
        params: list[Any] = [pattern]
        if chat_key:
            clauses.append('chat_key = ?')
            params.append(chat_key)
        elif self_id is not None:
            clauses.append('chat_key LIKE ?')
            params.append(f'{self_id}:%')
        params.append(limit)
        sql = (
            f'SELECT * FROM messages WHERE {" AND ".join(clauses)} '
            'ORDER BY ts DESC, id DESC LIMIT ?'
        )
        async with self._db.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_message(r) for r in rows]

    async def message_by_id(self, row_id: int) -> MessageRecord | None:
        if self._db is None:
            return None
        async with self._db.execute(
            'SELECT * FROM messages WHERE id = ?', (row_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_message(row) if row else None

    async def forward_nodes(
        self, key: str | None, forward_id: str
    ) -> list[dict[str, Any]]:
        """在已记录的消息里找某条合并转发内联的节点。

        有些适配器（Satori 等）把转发内容直接放在段里，那就无需再调接口。
        只扫最近的一批消息：转发内容只在原消息附近可查，全表扫描不划算。
        """
        if self._db is None or not forward_id:
            return []
        if key:
            sql = (
                'SELECT segments FROM messages WHERE chat_key = ? AND recalled = 0 '
                'ORDER BY id DESC LIMIT 200'
            )
            params: tuple[Any, ...] = (key,)
        else:
            sql = (
                'SELECT segments FROM messages WHERE recalled = 0 '
                'ORDER BY id DESC LIMIT 200'
            )
            params = ()
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        for row in rows:
            for seg in _safe_json(row['segments']):
                if not isinstance(seg, dict) or seg.get('type') != 'forward':
                    continue
                if str(seg.get('id') or '') != str(forward_id):
                    continue
                nodes = seg.get('nodes')
                if isinstance(nodes, list) and nodes:
                    return nodes
        return []

    async def message_by_message_id(self, message_id: str) -> MessageRecord | None:
        """按适配器的消息 ID 反查记录（用于给「回复」补上被引用内容）"""
        if self._db is None or not message_id:
            return None
        async with self._db.execute(
            'SELECT * FROM messages WHERE message_id = ? AND recalled = 0 '
            'ORDER BY id DESC LIMIT 1',
            (str(message_id),),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_message(row) if row else None

    async def mark_recalled(self, row_id: int) -> None:
        if self._db is None:
            return
        async with self._lock:
            await self._db.execute(
                'UPDATE messages SET recalled = 1 WHERE id = ?', (row_id,)
            )
            await self._db.commit()

    async def count(self, self_id: str | None = None) -> int:
        if self._db is None:
            return 0
        if self_id is None:
            sql, params = 'SELECT COUNT(*) AS n FROM messages', ()
        else:
            sql = 'SELECT COUNT(*) AS n FROM messages WHERE chat_key LIKE ?'
            params = (f'{self_id}:%',)
        async with self._db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row['n']) if row else 0

    # ── 维护 ────────────────────────────────────────────────────────────
    async def cleanup(self) -> int:
        """按条数/天数上限清理旧记录，返回删除条数"""
        if self._db is None:
            return 0
        removed = 0
        try:
            async with self._lock:
                if self._cfg.botui_max_records > 0:
                    count = await self.count()
                    extra = count - self._cfg.botui_max_records
                    if extra > 0:
                        await self._db.execute(
                            'DELETE FROM messages WHERE id IN '
                            '(SELECT id FROM messages ORDER BY id ASC LIMIT ?)',
                            (extra,),
                        )
                        removed += extra
                if self._cfg.botui_retention_days > 0:
                    cutoff = time.time() - self._cfg.botui_retention_days * 86400
                    cur = await self._db.execute(
                        'DELETE FROM messages WHERE ts < ?', (cutoff,)
                    )
                    removed += cur.rowcount or 0
                    # 名册跟着保留期一起清：否则长期跑下来 members 会无限增长
                    await self._db.execute(
                        'DELETE FROM members WHERE last_at < ?', (cutoff,)
                    )
                if removed:
                    await self._db.commit()
        except Exception as e:
            logger.warning(f'Failed to clean up old messages: {e}')
        return removed

    async def vacuum(self) -> None:
        if self._db is None:
            return
        try:
            await self._db.execute('VACUUM')
            await self._db.commit()
        except Exception as e:
            logger.debug(f'Vacuum skipped: {e}')

    async def reset(self) -> None:
        """清空全部记录（会话与消息）"""
        if self._db is None:
            return
        try:
            await self.flush()
        except Exception:  # pragma: no cover - 队列异常时仍然尝试清空
            pass
        async with self._lock:
            await self._db.execute('DELETE FROM messages')
            await self._db.execute('DELETE FROM chats')
            await self._db.execute('DELETE FROM members')
            # 机器人记录保留：它表示「连接过的账号」，不属于聊天内容
            # 让行号从头开始，避免「清空后第一条消息的 id 还是很大」
            await self._db.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('messages', 'chats')"
            )
            await self._db.commit()
        self._chats.clear()
        self._members.clear()
        logger.info('BotUI 已清空全部聊天记录')

    async def get_meta(self, key: str, default: str = '') -> str:
        if self._db is None:
            return default
        async with self._db.execute(
            'SELECT value FROM meta WHERE key = ?', (key,)
        ) as cur:
            row = await cur.fetchone()
        return str(row['value']) if row else default

    async def set_meta(self, key: str, value: str) -> None:
        if self._db is None:
            return
        async with self._lock:
            await self._db.execute(
                'INSERT INTO meta (key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (key, value),
            )
            await self._db.commit()


# 便捷构造：从 uninfo 的 Session 生成一条记录
def record_from_session(session: Any, direction: str = DIR_IN) -> MessageRecord:
    kind = KIND_PRIVATE if getattr(session.scene, 'is_private', False) else KIND_GROUP
    chat_id = str(session.scene.id)
    self_id = str(getattr(session, 'self_id', '') or '')
    # 会话 key 带上机器人 ID：同一平台的多个机器人各自一份会话，互不混淆
    key = chat_key(kind, chat_id, self_id)
    parent_id = str(getattr(session.scene.parent, 'id', '') or '')
    member_count = getattr(session.scene, 'member_count', None)
    return MessageRecord(
        chat_key=key,
        chat_kind=kind,
        chat_id=chat_id,
        direction=direction,
        ts=now_ts(),
        adapter=str(getattr(session, 'adapter', '') or ''),
        scope=str(getattr(session, 'scope', '') or ''),
        self_id=self_id,
        parent_id=parent_id,
        chat_name=str(getattr(session.scene, 'name', '') or ''),
        chat_avatar=avatar_of(getattr(session.scene, 'avatar', None)),
        member_count=member_count if isinstance(member_count, int) else None,
        user_id=str(getattr(session.user, 'id', '') or ''),
        user_name=str(getattr(session.user, 'name', '') or ''),
        user_avatar=avatar_of(getattr(session.user, 'avatar', None)),
    )
