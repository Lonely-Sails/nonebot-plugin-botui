"""把消息里可缓存的媒体/文件下载到本地（见 ``filecache.py``）。

为什么要单独一个模块、而不是直接写在 ``capture.py`` 里：下载是**纯 IO**，
既要能被「收到消息」的钩子调用，也必须能被定时任务、测试单独驱动；把「从
消息段里挑出可缓存的项 → 下载 → 落盘 → 回填链接」这套流程收在一处，采集
侧只需调一个 :func:`cache_message`。

回填（把段里的 ``url`` 换成 ``/api/cache/<id>``）是这里的关键副作用：一旦
换成缓存地址，WebUI 就再也不会去碰那个随时会失效的原始链接 —— 这也正是
「避免 url 失效以后打不开」的落点。
"""

from __future__ import annotations

import time
import asyncio
from typing import TYPE_CHECKING, Any

from nonebot import logger

from .media import Blocked, TooLarge, download
from .models import DIR_OUT, MessageRecord
from .segments import cacheable_segments

if TYPE_CHECKING:
    from .config import Config
    from .filecache import FileCache

#: 媒体直链普遍几分钟就失效，收到消息后多久之内要开始抓（秒）
FETCH_DELAY = 0.5
#: 一次并发下载几个（够快，又不至于同时朝上游开几十条连接）
MAX_CONCURRENCY = 4


def ref_for(chat_key: str, row_id: int) -> str:
    """聊天记录引用标记（``msg:<会话>:<行号>``）。

    有了这条引用，缓存回收就知道「这条资源还有人用」，不会把它当垃圾删掉。
    """
    if not chat_key or row_id <= 0:
        return ''
    return f'msg:{chat_key}:{int(row_id)}'


def msg_ref(message_id: Any) -> str:
    """按消息 ID 的引用标记（机器人发出的消息只有 message_id，没有行号）。"""
    text = str(message_id or '').strip()
    return f'msgid:{text}' if text else ''


def _name_of(seg: dict[str, Any], fallback: str = 'file') -> str:
    from .segments import resolve_file_name

    url = str(seg.get('url') or '')
    return resolve_file_name(seg.get('name'), url, fallback=fallback)


async def cache_segment(
    cache: 'FileCache',
    seg: dict[str, Any],
    *,
    cfg: 'Config',
    ref: str = '',
) -> bool:
    """下载并缓存单个段，成功时把段里的 ``url`` 换成缓存地址。

    返回是否缓存成功；失败一律静默（缓存只是「锦上添花」，绝不能影响消息
    记录本身），但会把原因记进 debug 日志。
    """
    source = str(seg.get('url') or '').strip()
    if not source.startswith(('http://', 'https://')):
        return False
    # 已经是缓存地址了（重复处理 / 自己发出的消息）就不必再抓
    from .filecache import kind_of, id_from_url

    if id_from_url(source):
        return False

    hit = await asyncio.to_thread(cache.lookup, source)
    if hit is not None:
        seg['url'] = hit.to_dict()['url']
        if not seg.get('size'):
            seg['size'] = hit.size
        if not seg.get('mime'):
            seg['mime'] = hit.mime or None
        if ref:
            await asyncio.to_thread(cache.add_ref, source, ref)
        return True

    limit = int(cfg.botui_cache_file_max_bytes)
    name = _name_of(seg)
    kind = kind_of(str(seg.get('type') or 'file'), str(seg.get('mime') or ''), name)
    tmp_dir = await asyncio.to_thread(cache.tmp_dir)
    tmp = tmp_dir / f'{int(time.time() * 1000)}-{abs(hash(source)) % 10**8}.tmp'
    try:
        size, mime = await download(
            source,
            tmp,
            max_bytes=limit,
            timeout=float(cfg.botui_cache_fetch_timeout),
        )
    except TooLarge:
        logger.debug(f'BotUI 跳过缓存：资源超过上限 {source[:64]}')
        return False
    except Blocked as e:
        logger.debug(f'BotUI 跳过缓存：{e}')
        return False
    except Exception as e:
        logger.debug(f'BotUI 缓存下载失败 {source[:64]}：{e}')
        return False

    record = await asyncio.to_thread(
        cache.put,
        tmp,
        name=name,
        mime=str(seg.get('mime') or mime or ''),
        kind=kind,
        source=source,
        ref=ref,
        key=source,
    )
    if record is None:
        return False
    data = record.to_dict()
    seg['url'] = data['url']
    seg['name'] = name
    if not seg.get('mime'):
        seg['mime'] = record.mime or None
    if not seg.get('size'):
        seg['size'] = size
    return True


async def cache_segments(
    cache: 'FileCache | None',
    segments: list[dict[str, Any]],
    *,
    cfg: 'Config',
    ref: str = '',
    budget: int = 0,
) -> int:
    """批量缓存（受并发与总预算限制），返回成功条数。

    传了 ``budget`` 时改成**逐个**下载：下载前不可能知道真实大小，只有串行
    才能保证「这一条消息总共抓的字节数」不越过上限 —— 并发下载会让几个资源
    一起冲进闸门，把上限顶穿。预算是给「一条消息塞几十张图」兜底的，串行一点
    换取硬上限是划算的；不同消息之间仍是并发（各自一个后台任务）。
    """
    if cache is None or not cache.enabled or not segments:
        return 0
    picked = cacheable_segments(segments)
    if not picked:
        return 0

    max_workers = 1 if budget > 0 else MAX_CONCURRENCY
    semaphore = asyncio.Semaphore(max_workers)
    spent = 0
    done = 0
    lock = asyncio.Lock()

    async def _one(seg: dict[str, Any]) -> None:
        nonlocal spent, done
        async with semaphore:
            async with lock:
                if budget > 0 and spent >= budget:
                    return
            ok = await cache_segment(cache, seg, cfg=cfg, ref=ref)
        if ok:
            async with lock:
                done += 1
                spent += int(seg.get('size') or 0)

    await asyncio.gather(*(_one(seg) for seg in picked), return_exceptions=True)
    return done


async def cache_message(
    cache: 'FileCache | None',
    record: MessageRecord,
    *,
    cfg: 'Config',
) -> int:
    """缓存一条记录里可缓存的段，并回填段里的链接。

    - 收到的消息：先落库拿到行号，再抓媒体 —— 抓完用 ``msg:<会话>:<行号>``
      建立引用，回收时就知道这条资源还被聊天记录用着；
    - 自己发出的消息：段里本来就是缓存地址（或本地附件），直接带
      ``msgid:<消息 ID>`` 建立引用即可，不必重新下载。
    """
    if cache is None or not cache.enabled or not record.segments:
        return 0
    if record.direction == DIR_OUT:
        for seg in record.segments:
            if not isinstance(seg, dict):
                continue
            url = str(seg.get('url') or '')
            if not url:
                continue
            ref = msg_ref(record.message_id) or ref_for(record.chat_key, record.row_id)
            if ref:
                await asyncio.to_thread(cache.add_ref, url, ref)
        return 0
    ref = ref_for(record.chat_key, record.row_id)
    try:
        return await cache_segments(
            cache,
            record.segments,
            cfg=cfg,
            ref=ref,
            budget=int(cfg.botui_cache_max_bytes_per_message),
        )
    except Exception as e:  # pragma: no cover - 缓存失败不影响记录
        logger.debug(f'BotUI 缓存消息媒体失败：{e}')
        return 0


__all__ = [
    'FETCH_DELAY',
    'cache_message',
    'cache_segment',
    'cache_segments',
    'msg_ref',
    'ref_for',
]
