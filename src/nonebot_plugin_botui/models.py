"""记录用的数据结构与序列化工具。"""

from __future__ import annotations

import time
from typing import Any
from dataclasses import field, dataclass

# 会话类型：多聊场景统一为 group，单聊为 private
KIND_GROUP = 'group'
KIND_PRIVATE = 'private'

# 消息方向：机器人收到 / 机器人发出
DIR_IN = 'in'
DIR_OUT = 'out'

# 各消息段在聊天列表里的简写
SEGMENT_LABELS: dict[str, str] = {
    'image': '[图片]',
    'voice': '[语音]',
    'audio': '[音频]',
    'video': '[视频]',
    'file': '[文件]',
    'at': '@',
    'face': '[表情]',
    'reply': '[回复]',
    'json': '[卡片]',
    'xml': '[卡片]',
    'forward': '[合并转发]',
    'music': '[音乐]',
    'poke': '[戳一戳]',
    'button': '[按钮]',
    'unknown': '[消息]',
}


def now_ts() -> float:
    """当前时间戳（秒）"""
    return time.time()


def chat_key(kind: str, chat_id: str) -> str:
    """会话在数据库中的唯一键，例如 ``group_123456``"""
    return f'{kind}_{chat_id}'


def split_chat_key(key: str) -> tuple[str, str]:
    """把 ``group_123456`` 拆成 ``('group', '123456')``"""
    kind, _, chat_id = key.partition('_')
    return (kind or KIND_GROUP), chat_id


def describe_segments(segments: list[dict[str, Any]]) -> str:
    """把消息段概括成一行文本，用于聊天列表预览"""
    parts: list[str] = []
    for seg in segments:
        stype = str(seg.get('type') or '')
        if stype == 'text':
            parts.append(str(seg.get('text') or ''))
        elif stype == 'at':
            target = str(seg.get('target') or '')
            if target == 'all':
                parts.append('@全体成员')
            else:
                parts.append(f'@{seg.get("name") or target}')
        else:
            parts.append(SEGMENT_LABELS.get(stype, '[消息]'))
    text = ''.join(parts).strip()
    return ' '.join(text.split())


@dataclass(slots=True)
class ChatRecord:
    """一个会话（群聊或私聊）"""

    key: str
    kind: str
    chat_id: str
    adapter: str = ''
    scope: str = ''
    self_id: str = ''
    parent_id: str = ''
    name: str = ''
    avatar: str = ''
    member_count: int | None = None
    last_text: str = ''
    last_at: float = 0.0
    last_direction: str = ''
    message_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            'key': self.key,
            'kind': self.kind,
            'id': self.chat_id,
            'name': self.name or self.chat_id,
            'avatar': self.avatar or None,
            'adapter': self.adapter or None,
            'self_id': self.self_id or None,
            'parent_id': self.parent_id or None,
            'member_count': self.member_count,
            'last_text': self.last_text,
            'last_at': self.last_at,
            'last_direction': self.last_direction,
            'unread': 0,
            'message_count': self.message_count,
        }


@dataclass(slots=True)
class MessageRecord:
    """一条消息（机器人收到的或发出的）"""

    chat_key: str
    chat_kind: str
    chat_id: str
    direction: str
    ts: float
    adapter: str = ''
    scope: str = ''
    self_id: str = ''
    parent_id: str = ''
    chat_name: str = ''
    chat_avatar: str = ''
    member_count: int | None = None
    user_id: str = ''
    user_name: str = ''
    user_avatar: str = ''
    role: str | None = None
    is_self: bool = False
    text: str = ''
    segments: list[dict[str, Any]] = field(default_factory=list)
    message_id: str = ''
    # 默认不可撤回：只有机器人自己发出、且拿到了消息 ID 的才可撤回
    recallable: bool = False
    api: str = ''
    recalled: bool = False
    row_id: int = 0

    def __post_init__(self) -> None:
        # 机器人自己发出、且拿到了消息 ID 的记录才算可撤回
        if self.direction == DIR_OUT and self.message_id:
            self.recallable = True

    @property
    def preview(self) -> str:
        """用于聊天列表的预览文本"""
        return self.text.strip() or describe_segments(self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.row_id,
            'chat': self.chat_key,
            'direction': self.direction,
            'time': self.ts,
            'user_id': self.user_id,
            'user_name': self.user_name or self.user_id,
            'user_avatar': self.user_avatar or None,
            'role': self.role,
            'self': self.is_self,
            'text': self.text,
            'message_id': self.message_id or None,
            'recallable': bool(self.recallable and self.direction == DIR_OUT),
            'recalled': self.recalled,
            'segments': self.segments,
            'adapter': self.adapter or None,
        }
