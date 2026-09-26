"""把通用消息序列（uniseg）转成可存储、可序列化的消息段。"""

from __future__ import annotations

from typing import Any

from nonebot import logger

MAX_TEXT = 4000
MAX_RAW = 2000

# 常见的卡片类原始段类型
_HYPER_TYPES = {'json', 'xml'}

# 各适配器对语音/媒体段的原始命名不一，统一映射到通用段类型。
# 例如 OneBot V11 用 ``record`` 表示语音。
_RAW_MEDIA_TYPES = {
    'record': 'voice',
    'voice': 'voice',
    'audio': 'audio',
    'video': 'video',
    'file': 'file',
}


def _clip(value: Any, limit: int = MAX_RAW) -> str:
    text = '' if value is None else str(value)
    return text[:limit]


def _seg_media_dict(stype: str, url: str = '', **extra: Any) -> dict[str, Any]:
    """构造语音/音频/视频段的统一形状。"""
    item: dict[str, Any] = {'type': stype, 'url': url}
    item.update(extra)
    return item


def _seg_image(seg: Any) -> dict[str, Any]:
    return {
        'type': 'image',
        # 优先用 url；只有 file 时也能显示（可能是本地路径或 base64）
        'url': _clip(
            getattr(seg, 'url', None)
            or getattr(seg, 'path', None)
            or getattr(seg, 'file', None)
        ),
        'file': _clip(getattr(seg, 'file', None) or getattr(seg, 'name', None)),
        'name': _clip(getattr(seg, 'name', None)) or None,
        'width': getattr(seg, 'width', None),
        'height': getattr(seg, 'height', None),
    }


def _seg_media(seg: Any, stype: str) -> dict[str, Any]:
    return _seg_media_dict(
        stype,
        _clip(
            getattr(seg, 'url', None)
            or getattr(seg, 'path', None)
            or getattr(seg, 'file', None)
        ),
        file=_clip(getattr(seg, 'file', None) or getattr(seg, 'name', None)),
        name=_clip(getattr(seg, 'name', None)) or None,
        duration=getattr(seg, 'duration', None),
    )


def _seg_at(seg: Any) -> dict[str, Any] | None:
    flag = str(getattr(seg, 'flag', 'user') or 'user')
    target = _clip(getattr(seg, 'target', ''))
    display = _clip(getattr(seg, 'display', None))
    if flag == 'user' and target.lower() in {'all', 'everyone'}:
        return {'type': 'at', 'target': 'all', 'name': '全体成员'}
    if flag == 'role':
        return {'type': 'at', 'target': f'role:{target}', 'name': display or '某身份组'}
    if flag == 'channel':
        return {
            'type': 'at',
            'target': f'channel:{target}',
            'name': display or '某子频道',
        }
    return {'type': 'at', 'target': target, 'name': display or None}


def _flatten_text(value: Any) -> str:
    """把各种「消息内容」压平成一行文本。

    引用消息里带的内容可能是纯字符串、适配器消息段列表（``Message`` 就是
    ``list`` 的子类）、uniseg 段列表，或者是一整个消息对象。这里全部兜住，
    拿不到就返回空串。
    """
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _flatten_text(value.get('message') or value.get('content'))
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                data = item.get('data')
                parts.append(
                    _flatten_text(data.get('text') if isinstance(data, dict) else None)
                    or _flatten_text(item.get('text'))
                )
                continue
            data = getattr(item, 'data', None)
            if isinstance(data, dict):
                # 适配器消息段
                parts.append(_flatten_text(data.get('text')))
                continue
            text = getattr(item, 'text', None)
            parts.append(text if isinstance(text, str) else '')
        return ''.join(parts)
    text = getattr(value, 'text', None)
    if isinstance(text, str):
        return text
    try:
        return str(value.extract_plain_text())
    except Exception:
        return ''


def _reply_payload(source: Any) -> dict[str, Any]:
    """把引用来源归一化成字典，方便统一取 sender / message。

    ``source`` 形态多变：

    - ``UniMessage.of`` 直接构造时 ``origin`` 是适配器消息段，数据在 ``.data``；
    - ``attach_reply`` 从事件抽取时 ``origin`` 是适配器引用模型，取 ``model_dump``；
    - 也可能本来就是 dict。
    """
    if source is None:
        return {}
    if isinstance(source, dict):
        return source
    data = getattr(source, 'data', None)
    if isinstance(data, dict):
        return data
    for attr in ('model_dump', 'dict'):
        dump = getattr(source, attr, None)
        if not callable(dump):
            continue
        try:
            result = dump()
        except Exception:  # pragma: no cover - 防御性
            continue
        if isinstance(result, dict):
            return result
    return {}


def _reply_identity(source: Any) -> tuple[str, str]:
    """从引用来源里取「被回复的人」，返回 ``(user_id, name)``。"""
    payload = _reply_payload(source)
    sender = payload.get('sender')
    if isinstance(sender, dict):
        name = sender.get('card') or sender.get('nickname') or sender.get('name') or ''
        uid = sender.get('user_id') or sender.get('id') or ''
        return _clip(uid), _clip(name)
    uid = payload.get('user_id') or payload.get('uin') or ''
    name = payload.get('nickname') or payload.get('name') or ''
    return _clip(uid), _clip(name)


def _reply_dict(
    reply_id: Any,
    *,
    text: str = '',
    uid: str = '',
    name: str = '',
) -> dict[str, Any]:
    """构造统一形状的 reply 段，空字段直接省略。

    ``segment_to_dict`` 与 ``_fallback_segments`` 两条路径都走这里，免得
    「字段怎么拼」的逻辑散落两处、改一处漏一处。
    """
    item: dict[str, Any] = {'type': 'reply', 'id': _clip(reply_id)}
    if uid:
        item['user_id'] = _clip(uid)
    if name:
        item['name'] = _clip(name)
    if text:
        item['text'] = text[:MAX_TEXT]
        item['preview'] = text[:120]
    return {k: v for k, v in item.items() if v not in ('', None)}


def _seg_reply(seg: Any) -> dict[str, Any]:
    """``Reply`` 段。

    只存一个 id 的话界面上只能显示「回复 12345」，看不出回复的是什么。uniseg
    在 ``UniMessage.attach_reply`` 之后其实已经把引用消息备好了：

    - 被引用正文在 ``seg.msg`` 上（官方字段，直接展开即可）；
    - 被引用者 ``sender`` 在原始引用对象 ``seg.origin`` 上（uniseg 的 ``Reply``
      段本身不暴露 sender，只有 origin 里有）。

    有些适配器会把 sender / message 直接塞进 reply 段数据里，``origin`` 那份
    也一并兜住。两者都拿不到时只留 id，由前端退化成「点击定位」。
    """
    origin = getattr(seg, 'origin', None)
    payload = _reply_payload(origin)
    text = _flatten_text(getattr(seg, 'msg', None)) or _flatten_text(
        payload.get('message') or payload.get('content') or payload.get('text')
    )
    uid, name = _reply_identity(origin)
    return _reply_dict(getattr(seg, 'id', ''), text=text, uid=uid, name=name)


def _seg_emoji(seg: Any) -> dict[str, Any]:
    """``Emoji`` 段。``url`` 是官方字段（部分平台的表情是图片），有就一起存。"""
    return {
        'type': 'face',
        'id': _clip(getattr(seg, 'id', '')),
        'name': _clip(getattr(seg, 'name', None)) or None,
        'url': _clip(getattr(seg, 'url', None)) or None,
    }


def _seg_button(seg: Any) -> dict[str, Any]:
    """``Button`` / ``Keyboard`` 段。

    uniseg 里 ``Keyboard`` 是「一行按钮」，``Button`` 是单个按钮。这里统一压成
    一条 ``button`` 段：保留可见文字与行为类型，界面上给个「[按钮]」占位，
    至少不会整段凭空消失。
    """
    from nonebot_plugin_alconna.uniseg import Button, Keyboard

    if isinstance(seg, Keyboard):
        labels = [
            _clip(getattr(b.label, 'text', b.label))
            for b in getattr(seg, 'children', [])
        ]
        label = ' / '.join(x for x in labels if x)
        return {'type': 'button', 'label': label or None, 'flag': 'keyboard'}

    label = getattr(seg, 'label', None)
    if isinstance(label, Button):  # pragma: no cover - 防御性
        label = None
    return {
        'type': 'button',
        'label': _clip(getattr(label, 'text', label)) or None,
        'flag': str(getattr(seg, 'flag', '') or ''),
        'url': _clip(getattr(seg, 'url', None)) or None,
    }


def _seg_reference(seg: Any) -> dict[str, Any]:
    return {'type': 'forward', 'id': _clip(getattr(seg, 'id', None)), 'count': None}


def _seg_other(seg: Any) -> dict[str, Any]:
    """适配器私有消息段：尽量保留原始信息，方便前端兜底展示。"""
    origin = getattr(seg, 'origin', None)
    raw_type = ''
    data: dict[str, Any] = {}
    if origin is not None:
        raw_type = str(getattr(origin, 'type', '') or '')
        raw_data = getattr(origin, 'data', None)
        if isinstance(raw_data, dict):
            data = {str(k): _clip(v) for k, v in raw_data.items()}
    stype = (
        raw_type
        if raw_type in _HYPER_TYPES
        else ('unknown' if not raw_type else raw_type)
    )
    item: dict[str, Any] = {'type': stype, 'raw': data or _clip(origin)}
    if raw_type == 'json':
        item['data'] = data.get('data', '')
    return item


def segment_to_dict(seg: Any) -> dict[str, Any] | None:
    """把单个通用消息段转成字典；无法识别的返回 None。

    覆盖 uniseg 文档里列出的段类型（见 ``docs/.../uniseg/segment.md``）：
    ``Text`` / ``At`` / ``AtAll`` / ``Emoji`` / ``Image`` / ``Audio`` /
    ``Voice`` / ``Video`` / ``File`` / ``Reply`` / ``Reference`` / ``Hyper`` /
    ``Button`` / ``Keyboard``，以及适配器私有的 ``Other`` 兜底。
    """
    # 延迟导入，避免与 alconna 的循环依赖
    from nonebot_plugin_alconna.uniseg import (
        At,
        File,
        Text,
        AtAll,
        Audio,
        Emoji,
        Hyper,
        Image,
        Other,
        Reply,
        Video,
        Voice,
        Button,
        Keyboard,
        Reference,
    )

    try:
        if isinstance(seg, Text):
            text = str(getattr(seg, 'text', '') or '')
            return {'type': 'text', 'text': text[:MAX_TEXT]} if text else None
        if isinstance(seg, AtAll):
            return {'type': 'at', 'target': 'all', 'name': '全体成员'}
        if isinstance(seg, At):
            return _seg_at(seg)
        if isinstance(seg, Image):
            return _seg_image(seg)
        if isinstance(seg, (Voice, Video, Audio)):
            stype = str(getattr(seg, 'type', 'voice') or 'voice')
            return _seg_media(seg, stype)
        if isinstance(seg, File):
            return {
                'type': 'file',
                'name': _clip(getattr(seg, 'name', None)) or '文件',
                'url': _clip(getattr(seg, 'url', None)),
                'file': _clip(getattr(seg, 'file', None)),
            }
        if isinstance(seg, Reply):
            return _seg_reply(seg)
        if isinstance(seg, Reference):
            return _seg_reference(seg)
        if isinstance(seg, Emoji):
            return _seg_emoji(seg)
        if isinstance(seg, (Button, Keyboard)):
            return _seg_button(seg)
        if isinstance(seg, Hyper):
            raw = _clip(getattr(seg, 'raw', None))
            content = getattr(seg, 'content', None)
            return {
                'type': str(getattr(seg, 'format', 'json') or 'json'),
                'data': raw or _clip(content),
            }
        if isinstance(seg, Other):
            return _seg_other(seg)
    except Exception as e:  # pragma: no cover - 防御性兜底
        logger.opt(exception=True).debug(f'Failed to convert segment {seg!r}: {e}')
        return None
    return None


def _convert(uni: Any) -> list[dict[str, Any]]:
    """把 ``UniMessage`` 逐段转成字典，转不出来的段直接跳过。"""
    segments: list[dict[str, Any]] = []
    for seg in uni:
        item = segment_to_dict(seg)
        if item:
            segments.append(item)
    return segments


def _build_uni(message: Any, bot: Any, adapter: str | None) -> Any:
    """构造 ``UniMessage``；拿不到就返回 None，由调用方走兜底路径。"""
    from nonebot_plugin_alconna.uniseg import UniMessage

    return UniMessage.of(message, bot=bot, adapter=adapter)


def to_segments(
    message: Any,
    bot: Any = None,
    adapter: str | None = None,
) -> list[dict[str, Any]]:
    """把适配器消息转换成通用消息段列表。

    优先使用 alconna 的跨平台转换；失败时退化为通用兜底。
    没有 bot 时可以通过 ``adapter`` 指定适配器名称（例如 ``'OneBot V11'``）。

    注意：多数适配器的 reply 段只带一个消息 id，被引用的「谁、什么内容」挂在
    事件上。要拿全引用信息请用 :func:`to_segments_async` 并传入 ``event``。
    """
    try:
        segments = _convert(_build_uni(message, bot, adapter))
    except Exception as e:
        logger.debug(f'UniMessage.of failed ({e!r}), falling back to raw conversion')
        segments = []
    return segments or _fallback_segments(message)


async def to_segments_async(
    message: Any,
    bot: Any = None,
    adapter: str | None = None,
    event: Any = None,
) -> list[dict[str, Any]]:
    """异步版 :func:`to_segments`。

    多了一步 ``UniMessage.attach_reply``：reply 段本身只有一个 id，被引用消息
    的发送者与内容在事件里，需要异步抽取。拿不到事件时行为与同步版一致。
    """
    if event is None:
        return to_segments(message, bot=bot, adapter=adapter)

    try:
        uni = _build_uni(message, bot, adapter)
        try:
            await uni.attach_reply(event, bot)
        except Exception as e:
            logger.debug(f'UniMessage.attach_reply failed ({e!r})')
        segments = _convert(uni)
    except Exception as e:
        logger.debug(f'UniMessage.of failed ({e!r}), falling back to raw conversion')
        segments = []
    if not segments:
        segments = _fallback_segments(message)
    # 兜底：attach_reply 没拿到（或适配器不支持）时，自己从事件上补一次。
    _fill_reply_from_event(segments, event)
    return segments


def _event_reply(event: Any) -> Any:
    """从事件上取引用对象：优先 ``reply`` 属性，退回 ``get_reply()``。"""
    reply = getattr(event, 'reply', None)
    if reply is not None:
        return reply
    getter = getattr(event, 'get_reply', None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # pragma: no cover - 防御性
        return None


def _fill_reply_from_event(segments: list[dict[str, Any]], event: Any) -> None:
    """兜底路径：把事件上的引用信息补进 reply 段。

    适配器消息段转出来的 reply 只有 id（OneBot V11 就是这种情况），这里从
    ``event.reply`` / ``event.get_reply()`` 里取发送者与正文补上。仅填空缺字段，
    不覆盖已有值。
    """
    reply = _event_reply(event)
    if reply is None:
        return
    target = next((s for s in segments if s.get('type') == 'reply'), None)
    if target is None:
        return

    payload = _reply_payload(reply)
    uid, name = _reply_identity(reply)
    if uid and not target.get('user_id'):
        target['user_id'] = uid
    if name and not target.get('name'):
        target['name'] = name
    if not target.get('text'):
        text = _flatten_text(payload.get('message') or payload.get('content'))
        if text:
            target['text'] = text[:MAX_TEXT]
            target['preview'] = text[:120]


def _fallback_segments(message: Any) -> list[dict[str, Any]]:
    """拿不到通用消息段时，直接按原始消息段做最小转换。"""
    out: list[dict[str, Any]] = []
    # 传入的是单个消息段时，包一层方便统一处理
    if hasattr(message, 'type') and hasattr(message, 'data'):
        message = [message]
    try:
        news = list(message)
    except TypeError:
        text = message if isinstance(message, str) else ''
        if text:
            out.append({'type': 'text', 'text': text[:MAX_TEXT]})
        return out
    for raw in news:
        rtype = str(getattr(raw, 'type', '') or '')
        data = getattr(raw, 'data', None)
        data = data if isinstance(data, dict) else {}
        if rtype == 'text':
            out.append({'type': 'text', 'text': _clip(data.get('text'), MAX_TEXT)})
        elif rtype == 'at':
            target = _clip(data.get('qq', ''))
            if target.lower() in {'all', 'everyone'}:
                out.append({'type': 'at', 'target': 'all', 'name': '全体成员'})
            else:
                out.append({'type': 'at', 'target': target, 'name': None})
        elif rtype == 'image':
            out.append(
                {
                    'type': 'image',
                    # OneBot V11 的图片地址可能只放在 file 字段里
                    'url': _clip(data.get('url') or data.get('file') or ''),
                    'file': _clip(data.get('file', '')),
                    'name': None,
                    'width': None,
                    'height': None,
                }
            )
        elif rtype in _RAW_MEDIA_TYPES:
            # 各适配器对语音/媒体段命名不一（OneBot 用 record），统一成通用名，
            # 免得前端只认 voice/video 而把 audio 段当未知段丢掉。
            kind = _RAW_MEDIA_TYPES[rtype]
            url = _clip(data.get('url') or data.get('file') or '')
            if kind == 'file':
                out.append(
                    {
                        'type': 'file',
                        'name': _clip(data.get('name') or data.get('file')) or '文件',
                        'url': url,
                        'file': _clip(data.get('file', '')),
                    }
                )
            else:
                out.append(_seg_media_dict(kind, url))
        elif rtype == 'face':
            out.append(
                {
                    'type': 'face',
                    'id': _clip(data.get('id', '')),
                    'name': _clip(data.get('name', '')),
                }
            )
        elif rtype == 'reply':
            # 兜底路径拿到的也是原始段：同样把 sender / message 一起带上，
            # 界面上才能显示「回复 谁：什么内容」而不只是一串 id
            uid, name = _reply_identity(data)
            text = _flatten_text(data.get('message') or data.get('content'))
            out.append(_reply_dict(data.get('id', ''), text=text, uid=uid, name=name))
        else:
            out.append(
                {
                    'type': rtype or 'unknown',
                    'raw': {str(k): _clip(v) for k, v in data.items()},
                }
            )
    return out


def extract_text(message: Any) -> str:
    """提取消息的纯文本内容"""
    try:
        return str(message.extract_plain_text()).strip()
    except Exception:
        try:
            return ''.join(
                str(getattr(seg, 'data', {}).get('text', ''))
                for seg in message
                if getattr(seg, 'type', '') == 'text'
            ).strip()
        except Exception:
            return str(message)[:MAX_TEXT]


def at_targets(segments: list[dict[str, Any]]) -> list[str]:
    """取出消息里 @ 的用户 id（不含全体成员）"""
    out: list[str] = []
    for seg in segments:
        if seg.get('type') != 'at':
            continue
        target = str(seg.get('target') or '')
        if target and target != 'all' and ':' not in target:
            out.append(target)
    return out
