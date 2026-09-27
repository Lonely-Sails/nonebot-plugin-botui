"""消息采集：拦截机器人收发的消息并写入存储。

- 收到的消息：通过 ``event_preprocessor`` 钩子获取事件与会话信息；
- 发出的消息：通过 ``Bot.on_calling_api`` 钩子拦截 send_msg 一类的接口。

之所以能在发出前就记录，是因为适配器正常发送消息时都会经由 ``Bot.call_api``。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from contextvars import Token, ContextVar

from nonebot import logger
from nonebot.typing import T_State
from nonebot.message import event_preprocessor
from nonebot.adapters import Bot, Event
from nonebot_plugin_uninfo import get_session

from .media import avatar_of
from .config import NICKNAME, Config
from .models import (
    DIR_IN,
    DIR_OUT,
    KIND_GROUP,
    KIND_PRIVATE,
    ChatRecord,
    MessageRecord,
    now_ts,
    chat_key,
    self_id_of,
    describe_segments,
    split_chat_key_parts,
)
from .segments import to_segments, extract_text, to_segments_async

if TYPE_CHECKING:
    from .store import MessageStore
    from .filecache import FileCache

_store: MessageStore | None = None
_cfg: Config | None = None
_cache: 'FileCache | None' = None

# 待关联结果的发送调用：id(payload) -> 记录
_pending_sends: dict[int, MessageRecord] = {}
_PENDING_LIMIT = 256

# 抑制采集的标记。
# WebUI 自己发消息时会先调 API、再手工补一条记录（因为要拿到行号返回给前端），
# 如果不抑制，采集钩子会再记一条，导致同一条消息在界面上出现两次、
# 会话的 message_count 也会多算一次。
#
# 用 ContextVar 而不是全局集合：NoneBot 的 call_api 里 ``data`` 是临时构造的
# dict，调用方拿不到它的 id；而 contextvar 在 anyio 任务组派生子任务时会被继承，
# 且天然按请求隔离，多个 WebUI 请求并发发送时不会互相误伤。
_suppress_sent: ContextVar[bool] = ContextVar('botui_suppress_sent', default=False)

# @昵称 的解析缓存：{chat_key}:{user_id} -> 昵称
_at_names: dict[str, str] = {}
_AT_NAME_LIMIT = 4096

_prune_counter = 0
_PRUNE_EVERY = 200

# 后台任务的强引用集合。
# 事件循环对任务只持弱引用（见 asyncio.create_task 文档），不自己留一份引用的话，
# 任务可能在跑完之前就被 GC 回收——清理任务正好是「发完就不管」的那类。
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    """跑一个后台任务并持有引用，避免中途被 GC 回收。

    只在 already-running 的事件循环里调用（``_persist`` 由消息钩子触发）。
    """
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def setup(
    store: 'MessageStore', config: Config, cache: 'FileCache | None' = None
) -> None:
    """注册采集钩子"""
    global _store, _cfg, _cache
    _store = store
    _cfg = config
    _cache = cache


def store_ref() -> 'MessageStore | None':
    return _store


def cache_ref() -> 'FileCache | None':
    return _cache


def suppress_sent() -> Token[bool]:
    """在当前上下文中关闭「发出的消息」采集，返回用于恢复的 token。

    供 WebUI 自己发送消息时使用：它发送后会手工补一条记录（需要行号），
    如果不关闭采集就会重复记录。
    """
    return _suppress_sent.set(True)


def resume_sent(token: Token[bool]) -> None:
    """恢复采集（配合 :func:`suppress_sent` 使用）"""
    _suppress_sent.reset(token)


def _sent_suppressed() -> bool:
    return _suppress_sent.get()


def _enabled_for(adapter: str) -> bool:
    """该适配器的消息要不要记（``BOTUI_EXCLUDE_ADAPTERS``）。

    适配器名可能取不到（空串），那种情况下照常记录 —— 排除名单是用户显式
    写下的，不该因为拿不到名字就把消息全丢掉。
    """
    if _cfg is None:
        return False
    return not (adapter and adapter in _cfg.botui_exclude_adapters)


def _persist(record: MessageRecord) -> None:
    """入队并周期性做清理"""
    global _prune_counter
    if _store is None:
        return
    _store.enqueue(record)
    # 落库后立刻把媒体缓存下来：这些直链普遍只有几分钟有效期，等清理任务
    # 顺路处理就已经失效了（这也是「缓冲一下避免 url 失效」的核心时机）。
    # 缓存失败/没开缓存都不影响记录本身，所以放后台跑，不 await。
    if _cache is not None and _cfg is not None and _cache.enabled:
        _spawn(_cache_media_of(record))
    _prune_counter += 1
    if _prune_counter >= _PRUNE_EVERY:
        _prune_counter = 0
        _cfg_now = _cfg
        if _cfg_now and (_cfg_now.botui_max_records or _cfg_now.botui_retention_days):
            _spawn(_store.cleanup())


async def _cache_media_of(record: MessageRecord) -> None:
    """等记录落库拿到行号，再把它里面的媒体下载到本地缓存。"""
    assert _store is not None
    assert _cfg is not None
    try:
        await _store.flush()
    except Exception as e:  # pragma: no cover - 落库异常时跳过缓存
        logger.debug(f'BotUI 落库前无法缓存媒体：{e}')
        return
    from .filefetch import cache_message

    try:
        await cache_message(_cache, record, cfg=_cfg)
    except Exception as e:  # pragma: no cover - 缓存失败不影响机器人
        logger.debug(f'BotUI 缓存媒体失败：{e}')


# ── 收到的消息 ──────────────────────────────────────────────────────────
@event_preprocessor
async def _capture_received(bot: Bot, event: Event, state: T_State) -> None:
    if _store is None or _cfg is None or not _store.ready:
        return
    if not _cfg.botui_capture_received:
        return
    adapter = bot.adapter.get_name()
    if not _enabled_for(adapter):
        return
    try:
        await _record_received(bot, event)
    except Exception as e:  # 采集失败不能影响机器人本身
        logger.opt(exception=True).warning(f'BotUI failed to capture message: {e}')


async def _record_received(bot: Bot, event: Event) -> None:
    assert _store is not None
    assert _cfg is not None  # setup() 时一定会赋值，这里只是给类型检查看
    session = await get_session(bot, event)
    if session is None:
        record = _fallback_record(bot, event)
        if record is None:
            return
    else:
        from .store import record_from_session

        record = record_from_session(session, DIR_IN)
        record.role = _role_of(session)
        # 会话元信息（成员数等）随消息一起刷新
        await _store.touch_chat(
            record.chat_key,
            name=record.chat_name,
            avatar=record.chat_avatar,
            member_count=record.member_count,
            adapter=record.adapter,
            scope=record.scope,
            self_id=record.self_id,
            parent_id=record.parent_id,
        )

    # 默认忽略「自己发的消息被适配器回传成事件」这种情况：否则同一条消息会
    # 既被发送钩子记成 out，又被这里记成 in，界面上出现两份。
    # 打开 BOTUI_CAPTURE_SELF 就按收到的消息照常记录。
    if not _cfg.botui_capture_self and _is_from_self(bot, session, record):
        return

    message = None
    try:
        message = event.get_message()
    except Exception as e:
        logger.debug(f'BotUI: event has no message content ({e})')
    if message is not None:
        # 传 event 是为了把 reply 段补全：适配器的 reply 段只有 id，
        # 被引用的发送者与正文在事件上（attach_reply 会取）。
        record.segments = await to_segments_async(message, bot, event=event)
    else:
        record.segments = []
    record.text = extract_text(message) if message is not None else ''
    record.message_id = _message_id_of(event)
    record.recallable = False
    record.api = 'event'

    # 尝试把 @某人 解析成群名片
    for seg in record.segments:
        if seg.get('type') == 'at' and not seg.get('name'):
            target = str(seg.get('target') or '')
            if target and target != 'all':
                name = await resolve_at_name(bot, record.chat_key, target)
                if name:
                    seg['name'] = name
                    # 顺手回填到成员名册：下次在 WebUI 里 @ 就有昵称可选了
                    if _store is not None:
                        await _store.rename_member(record.chat_key, target, name)

    if _cfg is not None and _cfg.botui_debug:
        logger.debug(
            f'BotUI captured [{record.chat_key}] '
            f'{record.user_name}: {record.preview[:80]!r}'
        )
    _persist(record)


def _is_from_self(bot: Bot, session: Any, record: MessageRecord) -> bool:
    """判断这条「收到的」消息是不是机器人自己发的。

    有些适配器（或配置了自收自发）会把机器人刚发出去的消息再作为事件推回来，
    不排除的话同一条消息会被记两次。
    """
    self_id = str(getattr(bot, 'self_id', '') or '')
    if not self_id:
        return False
    user_id = ''
    try:
        user = getattr(session, 'user', None)
        user_id = str(getattr(user, 'id', '') or '') if user is not None else ''
    except Exception:
        user_id = ''
    user_id = user_id or record.user_id
    return bool(user_id) and user_id == self_id


def _fallback_record(bot: Bot, event: Event) -> MessageRecord | None:
    """拿不到 uninfo 会话信息时的兜底（部分适配器/通知事件）"""
    try:
        user_id = str(event.get_user_id())
    except Exception:
        return None
    group_id = ''
    try:
        group_id = str(getattr(event, 'group_id', '') or '')
    except Exception:
        group_id = ''
    if group_id:
        kind, chat_id, name = KIND_GROUP, group_id, ''
    else:
        kind, chat_id, name = KIND_PRIVATE, user_id, ''
    try:
        adapter = bot.adapter.get_name()
    except Exception:
        adapter = ''
    self_id = str(getattr(bot, 'self_id', '') or '')
    return MessageRecord(
        chat_key=chat_key(kind, chat_id, self_id),
        chat_kind=kind,
        chat_id=chat_id,
        direction=DIR_IN,
        ts=now_ts(),
        adapter=adapter,
        self_id=self_id,
        chat_name=name,
        user_id=user_id,
        user_name='',
    )


def _role_of(session: Any) -> str | None:
    member = getattr(session, 'member', None)
    role = getattr(member, 'role', None) if member is not None else None
    if role is None:
        return None
    name = getattr(role, 'name', None)
    return str(name).lower() if name else None


def _message_id_of(event: Event) -> str:
    for attr in ('message_id', 'msg_id', 'message_seq', 'id'):
        value = getattr(event, attr, None)
        if value not in (None, ''):
            return str(value)
    return ''


async def resolve_at_name(bot: Bot, key: str, user_id: str) -> str:
    """把 @ 的用户 id 解析成昵称（带缓存，失败则返回空串）"""
    assert _cfg is not None
    if not _cfg.botui_resolve_at_name:
        return ''
    cache_key = f'{key}:{user_id}'
    if cache_key in _at_names:
        return _at_names[cache_key]
    name = ''
    try:
        _, kind, chat_id = split_chat_key_parts(key)
        if kind == KIND_GROUP:
            info = await asyncio.wait_for(
                bot.call_api(
                    'get_group_member_info',
                    group_id=int(chat_id),
                    user_id=int(user_id),
                    no_cache=True,
                ),
                timeout=_cfg.botui_api_timeout,
            )
            if isinstance(info, dict):
                name = str(info.get('card') or info.get('nickname') or '')
    except Exception as e:
        logger.debug(f'BotUI: cannot resolve name of {user_id}: {e}')
        return ''
    if name:
        if len(_at_names) > _AT_NAME_LIMIT:
            _at_names.clear()
        _at_names[cache_key] = name
    return name


# ── 发出的消息 ──────────────────────────────────────────────────────────
def register_hooks() -> None:
    """注册发送相关钩子（由插件在启动时调用）"""
    Bot.on_calling_api(_on_calling_api)
    Bot.on_called_api(_on_called_api)


def _extract_target(
    api: str, data: dict[str, Any], self_id: str
) -> tuple[str, str] | None:
    """从调用参数里推断发送目标，返回 (会话key, 目标id)；认不出就返回 None。

    各适配器的参数形状差别很大，这里只认明确表示会话的字段。**不要**去猜
    「名字里有 group 就随便挑一个数字字段」：``upload_group_file`` 这类接口
    根本没有会话参数，猜出来的会是文件名或文件夹序号，于是聊天列表里凭空
    多出一堆幽灵会话。
    """
    group_id = data.get('group_id')
    user_id = data.get('user_id')
    channel_id = data.get('channel_id') or data.get('guild_id')

    if group_id not in (None, ''):
        return chat_key(KIND_GROUP, str(group_id), self_id), str(group_id)
    if channel_id not in (None, ''):
        return chat_key(KIND_GROUP, str(channel_id), self_id), str(channel_id)
    if user_id not in (None, ''):
        return chat_key(KIND_PRIVATE, str(user_id), self_id), str(user_id)
    return None


async def _on_calling_api(bot: Bot, api: str, data: dict[str, Any]) -> None:
    if _store is None or _cfg is None or not _store.ready:
        return
    if not _cfg.botui_capture_sent:
        return
    if _sent_suppressed():
        return
    from .config import SEND_APIS

    if api not in SEND_APIS:
        return
    adapter = bot.adapter.get_name()
    if not _enabled_for(adapter):
        return
    try:
        self_id = str(getattr(bot, 'self_id', '') or '')
        target = _extract_target(api, data, self_id)
        if target is None:
            logger.debug(f'BotUI: cannot infer target for api {api}, skip')
            return
        key, target_id = target
        _, kind, _ = split_chat_key_parts(key)
        record = MessageRecord(
            chat_key=key,
            chat_kind=kind,
            chat_id=target_id,
            direction=DIR_OUT,
            ts=now_ts(),
            adapter=adapter,
            self_id=self_id,
            user_id=self_id,
            user_name=NICKNAME or self_id,
            is_self=True,
            recallable=True,
            api=api,
        )
        cached = _store.chat(key)
        if cached is not None:
            record.chat_name = cached.name
            record.chat_avatar = cached.avatar
            record.scope = cached.scope
            record.parent_id = cached.parent_id
            record.member_count = cached.member_count
        message = data.get('message')
        if message is not None:
            record.segments = _segments_of_payload(message, bot)
            record.text = _text_of_payload(message)
        else:
            # 文件/合并转发等没有 message 字段的接口
            name = str(data.get('name') or data.get('file') or '')
            record.segments = [
                {'type': 'file', 'name': name or '文件', 'url': '', 'file': name}
            ]
        if len(_pending_sends) > _PENDING_LIMIT:
            _pending_sends.clear()
        _pending_sends[id(data)] = record
    except Exception as e:
        logger.opt(exception=True).warning(f'BotUI failed to prepare sent message: {e}')


def _message_class_of(payload: list[Any]) -> type[Any] | None:
    """从列表里的消息段推出该适配器的 ``Message`` 类，推不出返回 None。

    ``get_message_class()`` 是 ``MessageSegment`` 上的 classmethod（alconna 里
    也是这么用的：``message.get_message_class()``），``Adapter`` 上并没有这个
    方法，所以只能从消息段本身问。
    """
    from nonebot.adapters import Message as BaseMessage

    for item in payload:
        getter = getattr(item, 'get_message_class', None)
        if not callable(getter):
            continue
        try:
            cls = getter()
        except Exception:
            continue
        if isinstance(cls, type) and issubclass(cls, BaseMessage):
            return cls
    return None


def _segments_of_payload(message: Any, bot: Bot) -> list[dict[str, Any]]:
    """把待发送的消息内容转成消息段"""
    from nonebot.adapters import Message as BaseMessage
    from nonebot.adapters import MessageSegment

    # 注意：适配器的 Message 本身是 list 的子类，所以必须先判断 MessageSegment，
    # 再判断 Message，最后才轮到 list，否则会被当成普通列表处理。
    if isinstance(message, MessageSegment):
        return to_segments([message], adapter=_adapter_name(bot))
    if isinstance(message, BaseMessage):
        return to_segments(message, bot=bot)
    if isinstance(message, str):
        return [{'type': 'text', 'text': message}]
    if isinstance(message, list):
        message_class = _message_class_of(message)
        if message_class is not None:
            try:
                return to_segments(message_class(message), bot=bot)
            except Exception:
                pass
    return [{'type': 'unknown', 'raw': str(message)[:500]}]


def _adapter_name(bot: Bot) -> str | None:
    try:
        return bot.adapter.get_name()
    except Exception:
        return None


def _text_of_payload(message: Any) -> str:
    """取出待发送内容里的纯文本（用于聊天列表预览）"""
    from nonebot.adapters import Message as BaseMessage

    if isinstance(message, str):
        return message[:4000]
    if isinstance(message, BaseMessage):
        return extract_text(message)
    if isinstance(message, list):
        parts = [
            str(getattr(seg, 'data', {}).get('text', ''))
            for seg in message
            if getattr(seg, 'type', '') == 'text'
        ]
        return ''.join(parts).strip()[:4000]
    return ''


async def _on_called_api(
    bot: Bot, exception: Exception | None, api: str, data: dict[str, Any], result: Any
) -> None:
    record = _pending_sends.pop(id(data), None)
    if record is None:
        return
    if exception is not None:
        logger.debug(f'BotUI: api {api} failed, message not recorded: {exception}')
        return
    try:
        record.message_id = _result_message_id(result)
        if not record.segments:
            record.segments = []
        if _cfg is not None and _cfg.botui_debug:
            logger.debug(
                f'BotUI captured sent [{record.chat_key}]: {record.preview[:80]!r}'
            )
        _persist(record)
    except Exception as e:
        logger.opt(exception=True).warning(f'BotUI failed to record sent message: {e}')


def _result_message_id(result: Any) -> str:
    if isinstance(result, dict):
        for attr in ('message_id', 'msg_id', 'id'):
            value = result.get(attr)
            if value not in (None, ''):
                return str(value)
    if isinstance(result, (str, int)):
        return str(result)
    for attr in ('message_id', 'msg_id', 'id'):
        value = getattr(result, attr, None)
        if value not in (None, ''):
            return str(value)
    return ''


# ── 供 WebUI 使用的构造逻辑 ─────────────────────────────────────────────
def build_outgoing(
    chat: ChatRecord,
    segments: list[dict[str, Any]],
    text: str,
    self_id: str = '',
    adapter: str = '',
    scope: str = '',
) -> MessageRecord:
    """根据 WebUI 的发送请求构造一条“发出”记录"""
    kind = chat.kind or (
        KIND_GROUP if split_chat_key_parts(chat.key)[1] == KIND_GROUP else KIND_PRIVATE
    )
    bot_id = self_id or chat.self_id or self_id_of(chat.key)
    resolved_key = chat.key or chat_key(kind, chat.chat_id, bot_id)
    name = NICKNAME or bot_id or 'BOT'
    return MessageRecord(
        chat_key=resolved_key,
        chat_kind=kind,
        chat_id=chat.chat_id,
        direction=DIR_OUT,
        ts=now_ts(),
        adapter=adapter or chat.adapter,
        scope=scope or chat.scope,
        self_id=bot_id,
        parent_id=chat.parent_id,
        chat_name=chat.name,
        chat_avatar=chat.avatar,
        member_count=chat.member_count,
        user_id=bot_id,
        user_name=name,
        is_self=True,
        text=text,
        segments=segments,
        recallable=True,
        api='webui',
    )


__all__ = [
    'avatar_of',
    'build_outgoing',
    'cache_ref',
    'describe_segments',
    'register_hooks',
    'resolve_at_name',
    'setup',
    'store_ref',
]
