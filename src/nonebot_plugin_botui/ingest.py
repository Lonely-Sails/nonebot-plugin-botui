"""把消息里可抓的媒体 / 文件收进媒体库（见 ``mediastore.py``）。

为什么要单独一个模块、而不是直接写在 ``capture.py`` 里：下载是**纯 IO**，
既要能被「收到消息」的钩子调用，也必须能被定时任务、测试单独驱动；把「从
消息段里挑出可入库的项 → 下载 → 落盘 → 回填地址」这套流程收在一处，采集侧
只需调一个 :func:`ingest_message`。

回填（把段里的 ``url`` 换成 ``/api/media/<id>``）是这里的关键副作用：一旦换成
媒体库地址，WebUI 就再也不会去碰那个随时会失效的原始链接 —— 这也正是「避免
url 失效以后打不开」的落点。
"""

from __future__ import annotations

import time
import asyncio
from typing import TYPE_CHECKING, Any

from nonebot import logger

from .media import Blocked, TooLarge, download
from .models import DIR_OUT, MessageRecord
from .segments import cacheable_segments
from .mediastore import (
    url_of,
    kind_of,
    ref_for,
    id_from_url,
    ref_of_message_id,
)

if TYPE_CHECKING:
    from .config import Config
    from .mediastore import MediaStore

#: 媒体直链普遍几分钟就失效，收到消息后多久之内要开始抓（秒）
FETCH_DELAY = 0.5
#: 一次并发下载几个（够快，又不至于同时朝上游开几十条连接）
MAX_CONCURRENCY = 4


async def ingest_segment(
    media: 'MediaStore',
    seg: dict[str, Any],
    *,
    cfg: 'Config',
    ref: str = '',
) -> bool:
    """下载并保存单个段，成功时把段里的 ``url`` 换成媒体库地址。

    返回是否「已经就位」；失败一律静默（媒体库只是「锦上添花」，绝不能影响
    消息记录本身），但会把原因记进 debug 日志。

    先查库再决定要不要下载 —— 一次查询就同时覆盖了三种情况：原链接已有本地
    副本（命中）、段里本来就是媒体库地址（重复处理）、以及自己发出的附件
    （发送时已入库）。命中时把 ``url`` 统一换成媒体库地址：它是这套东西里最稳
    的门牌，图片、文件、音视频都由同一个接口带着 Range 支持供出。

    注意回填的是**内存里的段**（返回给前端 / WebSocket 推送的那份，也因此第一
    时间就是稳定地址）。数据库里存的仍是原始地址：万一媒体库被清空，库里如果
    只剩媒体库地址就再也取不回来了，而存原始地址还能回源重下。
    """
    source = str(seg.get('url') or '').strip()
    if not source:
        return False

    hit = await media.lookup(source)
    if hit is not None:
        seg['url'] = url_of(hit.id)
        if not seg.get('size'):
            seg['size'] = hit.size
        if not seg.get('mime'):
            seg['mime'] = hit.mime or None
        if ref:
            await media.add_ref(source, ref)
        return True

    # 媒体库地址但取不到条目（被清理了）：没有可抓的原始链接，放弃
    if id_from_url(source) or not source.startswith(('http://', 'https://')):
        return False

    limit = int(cfg.botui_media_file_max_bytes)
    name = _name_of(seg)
    kind = kind_of(name, str(seg.get('mime') or ''))
    tmp = await asyncio.to_thread(media.tmp_dir)
    target = tmp / f'{int(time.time() * 1000)}-{abs(hash(source)) % 10**8}.part'
    try:
        size, mime = await download(
            source,
            target,
            max_bytes=limit,
            timeout=float(cfg.botui_media_fetch_timeout),
        )
    except TooLarge:
        logger.debug(f'BotUI 跳过媒体：资源超过上限 {source[:64]}')
        return False
    except Blocked as e:
        logger.debug(f'BotUI 跳过媒体：{e}')
        return False
    except Exception as e:
        logger.debug(f'BotUI 下载媒体失败 {source[:64]}：{e}')
        return False

    record = await media.put(
        target,
        name=name,
        mime=str(seg.get('mime') or mime or ''),
        kind=kind,
        source=source,
        ref=ref,
    )
    if record is None:
        return False
    seg['url'] = url_of(record.id)
    seg['name'] = name
    if not seg.get('mime'):
        seg['mime'] = record.mime or None
    if not seg.get('size'):
        seg['size'] = size
    return True


def _name_of(seg: dict[str, Any], fallback: str = 'file') -> str:
    from .segments import resolve_file_name

    return resolve_file_name(
        seg.get('name'), str(seg.get('url') or ''), fallback=fallback
    )


async def ingest_segments(
    media: 'MediaStore | None',
    segments: list[dict[str, Any]],
    *,
    cfg: 'Config',
    ref: str = '',
    budget: int = 0,
) -> int:
    """批量入库（受并发与总预算限制），返回成功条数。

    传了 ``budget`` 时改成**逐个**下载：下载前不可能知道真实大小，只有串行才能
    保证「这一条消息总共抓的字节数」不越过上限 —— 并发会让几个资源一起冲进
    闸门，把上限顶穿。预算是给「一条消息塞几十张图」兜底的，串行一点换取硬
    上限是划算的；不同消息之间仍是并发（各自一个后台任务）。
    """
    if media is None or not media.enabled or not segments:
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
            ok = await ingest_segment(media, seg, cfg=cfg, ref=ref)
        if ok:
            async with lock:
                done += 1
                spent += int(seg.get('size') or 0)

    await asyncio.gather(*(_one(seg) for seg in picked), return_exceptions=True)
    return done


async def ingest_message(
    media: 'MediaStore | None',
    record: MessageRecord,
    *,
    cfg: 'Config',
) -> int:
    """把一条记录里可入库的段收进媒体库，并回填段里的链接。

    - 收到的消息：先落库拿到行号，再抓媒体 —— 抓完用 ``msg:<会话>:<行号>``
      建立引用，回收时就知道这条资源还被聊天记录用着；
    - 自己发出的消息：段里本来就是媒体库地址或本地附件（**不需要也不想**重新
      下载），带 ``msgid:<消息 ID>`` 建立引用即可。

    两条路径共用 :func:`ingest_segments`：:func:`ingest_segment` 自己会跳过
    已经指向媒体库的段、命中已有副本时复用本地文件，所以这里不必分叉。
    """
    if media is None or not media.enabled or not record.segments:
        return 0
    if record.direction == DIR_OUT:
        ref = ref_of_message_id(record.message_id) or ref_for(
            record.chat_key, record.row_id
        )
    else:
        ref = ref_for(record.chat_key, record.row_id)
    try:
        return await ingest_segments(
            media,
            record.segments,
            cfg=cfg,
            ref=ref,
            budget=int(cfg.botui_media_max_bytes_per_message),
        )
    except Exception as e:  # pragma: no cover - 入库失败不影响记录
        logger.debug(f'BotUI 保存消息媒体失败：{e}')
        return 0


__all__ = [
    'FETCH_DELAY',
    'ingest_message',
    'ingest_segment',
    'ingest_segments',
]
