"""存储层单测：写入、读取、翻页、撤回标记、清理与重置。

注意：插件包在 import 阶段就会 `require` 其他插件并读取配置，
因此所有 import 都必须放在测试函数/夹具内部（NoneBot 初始化之后）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from pathlib import Path

import pytest

if TYPE_CHECKING:
    pass


def _record(key: str, text: str, direction: str = 'in', ts: float | None = None):
    from nonebot_plugin_botui.models import (
        DIR_IN,
        DIR_OUT,
        MessageRecord,
        split_chat_key_parts,
    )

    _, kind, chat_id = split_chat_key_parts(key)
    return MessageRecord(
        chat_key=key,
        chat_kind=kind,
        chat_id=chat_id,
        direction='in' if direction == DIR_IN else DIR_OUT,
        ts=ts if ts is not None else time.time(),
        adapter='OneBot V11',
        self_id='12345678',
        chat_name='测试群',
        user_id='10001' if direction == DIR_IN else '12345678',
        user_name='小明' if direction == DIR_IN else 'BOT',
        is_self=direction == DIR_OUT,
        text=text,
        segments=[{'type': 'text', 'text': text}],
        message_id=f'm{int(ts or 0)}' if direction == DIR_OUT else '',
    )


def _chat_key(kind: str, chat_id: str) -> str:
    from nonebot_plugin_botui.models import chat_key

    # 会话 key 现在带上机器人 ID：同一平台多个机器人各自一份会话
    return chat_key(kind, chat_id, '12345678')


def _new_store(path: Path, **overrides):
    from nonebot_plugin_botui.store import MessageStore
    from nonebot_plugin_botui.config import Config

    cfg = Config(**overrides)
    return MessageStore(cfg, path / 'botui.sqlite3')


@pytest.fixture
async def store(tmp_path: Path):
    item = _new_store(tmp_path / 'data', botui_max_records=0, botui_retention_days=0)
    await item.start()
    try:
        yield item
    finally:
        await item.stop()


async def test_store_insert_and_query(store):
    key = _chat_key('group', '87654321')
    base = time.time() - 100
    store.enqueue(_record(key, '第一条', ts=base))
    store.enqueue(_record(key, '第二条', direction='out', ts=base + 1))
    store.enqueue(_record(key, '第三条', ts=base + 2))
    await store.flush()

    assert await store.count() == 3

    messages = await store.messages(key)
    assert [m.text for m in messages] == ['第一条', '第二条', '第三条']
    assert messages[1].direction == 'out'
    assert messages[1].is_self is True
    # 只有机器人自己发的、且带消息 id 的才可撤回
    assert messages[1].recallable is True
    assert messages[0].recallable is False


async def test_store_chat_cache(store):
    key = _chat_key('group', '87654321')
    base = time.time() - 100
    store.enqueue(_record(key, '第一条', ts=base))
    store.enqueue(_record(key, '最新一条', direction='out', ts=base + 5))
    await store.flush()

    chat = store.chat(key)
    assert chat is not None
    assert chat.name == '测试群'
    assert chat.last_text == '最新一条'
    assert chat.last_direction == 'out'
    assert chat.message_count == 2

    chats = store.chats()
    assert [c.key for c in chats] == [key]
    # 搜索命中名称与消息内容
    assert store.chats('测试群')
    assert store.chats('最新')
    assert store.chats('不存在的关键词') == []


async def test_member_roster_collects_senders_and_mentions(store):
    """成员名册要收下「发消息的人」和「被 @ 的人」，供 WebUI 的 @ 菜单用。"""
    from nonebot_plugin_botui.models import MessageRecord

    key = _chat_key('group', '87654321')
    base = time.time() - 100
    store.enqueue(_record(key, '我来了', ts=base))
    store.enqueue(
        MessageRecord(
            chat_key=key,
            chat_kind='group',
            chat_id='87654321',
            direction='in',
            ts=base + 1,
            self_id='12345678',
            user_id='10002',
            user_name='小红',
            role='admin',
            segments=[
                {'type': 'at', 'target': '10003', 'name': '小刚'},
                {'type': 'at', 'target': 'all', 'name': '全体成员'},
                {'type': 'text', 'text': '看这个'},
            ],
        )
    )
    await store.flush()

    members = {m.user_id: m for m in store.members(key)}
    assert set(members) == {'10001', '10002', '10003'}
    assert members['10001'].name == '小明'
    assert members['10003'].name == '小刚'
    # 「全体成员」没有 user_id，不该进名册
    assert 'all' not in members
    # 角色也一并记下来（前端显示群主/管理员徽标）
    assert members['10002'].role == 'admin'

    # 最近活跃的排在前面
    top = store.members(key)[0].user_id
    assert top in {'10002', '10003'}

    # 关键词同时匹配昵称与 id
    assert [m.user_id for m in store.members(key, '小红')] == ['10002']
    assert [m.user_id for m in store.members(key, '10003')] == ['10003']
    assert store.members(key, '查无此人') == []


async def test_member_name_backfill_keeps_existing(store):
    """@ 解析出昵称后回填；用空串回填不能把已有名字抹掉。"""
    key = _chat_key('group', '87654321')
    store.enqueue(_record(key, '你好'))
    await store.flush()

    await store.rename_member(key, '10001', '小明（群名片）')
    assert store.member(key, '10001').name == '小明（群名片）'

    # 空名字不应覆盖
    await store.rename_member(key, '10001', '')
    assert store.member(key, '10001').name == '小明（群名片）'


async def test_member_cache_survives_reload(tmp_path):
    """名册要落库：进程重启后 @ 菜单仍然有人可选。"""
    path = tmp_path / 'data'
    key = _chat_key('group', '87654321')

    first = _new_store(path)
    await first.start()
    first.enqueue(_record(key, '你好'))
    await first.flush()
    await first.stop()

    second = _new_store(path)
    await second.start()
    try:
        members = second.members(key)
        assert [m.user_id for m in members] == ['10001']
        assert members[0].name == '小明'
    finally:
        await second.stop()


async def test_message_by_message_id(store):
    """按适配器消息 ID 反查记录（回复时要用它补上被引用内容）。"""
    key = _chat_key('group', '87654321')
    store.enqueue(_record(key, '发出的', direction='out', ts=time.time()))
    await store.flush()

    messages = await store.messages(key)
    mid = messages[0].message_id
    found = await store.message_by_message_id(mid)
    assert found is not None
    assert found.text == '发出的'

    assert await store.message_by_message_id('不存在') is None
    assert await store.message_by_message_id('') is None


async def test_store_pagination(store):
    key = _chat_key('group', '87654321')
    base = time.time() - 100
    for i in range(10):
        store.enqueue(_record(key, f'消息{i}', ts=base + i))
    await store.flush()

    latest = await store.messages(key, limit=4)
    assert [m.text for m in latest] == ['消息6', '消息7', '消息8', '消息9']

    older = await store.messages(key, limit=4, before=latest[0].ts)
    assert [m.text for m in older] == ['消息2', '消息3', '消息4', '消息5']


async def test_store_message_by_id_and_recall(store):
    key = _chat_key('group', '87654321')
    store.enqueue(_record(key, '可以撤回', direction='out', ts=time.time()))
    await store.flush()

    messages = await store.messages(key)
    row_id = messages[0].row_id
    assert row_id > 0

    found = await store.message_by_id(row_id)
    assert found is not None
    assert found.text == '可以撤回'

    await store.mark_recalled(row_id)
    # 撤回后的消息不再出现在聊天记录里
    assert await store.messages(key) == []


async def test_store_reset(store):
    key = _chat_key('group', '87654321')
    store.enqueue(_record(key, '待清空', ts=time.time()))
    await store.flush()
    assert await store.count() == 1

    await store.reset()
    assert await store.count() == 0
    assert store.chats() == []
    assert store.chat(key) is None


async def test_store_cleanup_by_limit(tmp_path: Path):
    item = _new_store(tmp_path / 'data', botui_max_records=5)
    await item.start()
    try:
        key = _chat_key('group', '87654321')
        base = time.time() - 100
        for i in range(10):
            item.enqueue(_record(key, f'消息{i}', ts=base + i))
        await item.flush()
        assert await item.count() == 10

        removed = await item.cleanup()
        assert removed == 5
        assert await item.count() == 5
        # 保留的是最新的 5 条
        left = await item.messages(key, limit=10)
        assert [m.text for m in left] == [f'消息{i}' for i in range(5, 10)]
    finally:
        await item.stop()


async def test_store_cleanup_by_retention(tmp_path: Path):
    item = _new_store(tmp_path / 'data', botui_retention_days=1)
    await item.start()
    try:
        key = _chat_key('group', '87654321')
        now = time.time()
        item.enqueue(_record(key, '很久以前', ts=now - 3 * 86400))
        item.enqueue(_record(key, '刚刚', ts=now))
        await item.flush()

        removed = await item.cleanup()
        assert removed == 1
        left = await item.messages(key)
        assert [m.text for m in left] == ['刚刚']
    finally:
        await item.stop()


async def test_touch_chat_does_not_create_message(store):
    key = _chat_key('private', '10001')
    await store.touch_chat(key, name='小明', adapter='OneBot V11', self_id='12345678')

    assert await store.count() == 0
    chat = store.chat(key)
    assert chat is not None
    assert chat.kind == 'private'
    assert chat.name == '小明'


async def test_chat_alias_takes_precedence_and_persists(tmp_path: Path):
    """会话备注优先作为展示名，且要落库（重启后仍在）。"""
    path = tmp_path / 'data'
    key = _chat_key('group', '87654321')

    first = _new_store(path)
    await first.start()
    first.enqueue(_record(key, '你好'))
    await first.flush()
    # 适配器给的名称仍在 raw_name 里，展示名不受影响
    assert first.chat(key).to_dict()['name'] == '测试群'
    await first.set_chat_alias(key, '我的测试群')
    data = first.chat(key).to_dict()
    assert data['name'] == '我的测试群'
    assert data['alias'] == '我的测试群'
    assert data['raw_name'] == '测试群'
    await first.stop()

    second = _new_store(path)
    await second.start()
    try:
        chat = second.chat(key)
        assert chat is not None
        assert chat.alias == '我的测试群'
        # 备注参与会话搜索
        assert key in {c.key for c in second.chats('我的测试群')}
    finally:
        await second.stop()


async def test_chat_alias_can_be_cleared_and_unknown_is_none(store):
    key = _chat_key('group', '87654321')
    store.enqueue(_record(key, '你好'))
    await store.flush()

    await store.set_chat_alias(key, '备注')
    assert store.chat(key).alias == '备注'
    await store.set_chat_alias(key, '')
    assert store.chat(key).alias == ''
    # 会话不存在时返回 None（不抛异常）
    assert await store.set_chat_alias('12345678:group_404', 'x') is None


async def test_member_alias_does_not_override_nickname(store):
    """成员备注与适配器昵称互不覆盖，展示名以备注优先。"""
    key = _chat_key('group', '87654321')
    store.enqueue(_record(key, '你好'))
    await store.flush()

    await store.set_member_alias(key, '10001', '老板')
    member = store.member(key, '10001')
    assert member.alias == '老板'
    assert member.name == '小明'  # 原始昵称保留
    assert member.to_dict()['name'] == '老板'
    assert member.to_dict()['raw_name'] == '小明'

    # 备注也参与成员搜索
    assert '10001' in {m.user_id for m in store.members(key, '老板')}

    # 后续自动收集昵称不会把备注顶掉
    store.enqueue(_record(key, '又说一句'))
    await store.flush()
    assert store.member(key, '10001').alias == '老板'


async def test_bot_alias(store):
    record = await store.upsert_bot('12345678', adapter='OneBot V11', name='机器人')
    assert record.to_dict()['name'] == '机器人'
    updated = await store.set_bot_alias('12345678', '小助手')
    assert updated is not None
    data = updated.to_dict()
    assert data['name'] == '小助手'
    assert data['alias'] == '小助手'
    assert data['raw_name'] == '机器人'
    # 再次 upsert（模拟重连）不能把备注抹掉
    record = await store.upsert_bot('12345678', name='机器人')
    assert record.alias == '小助手'
    assert await store.set_bot_alias('99999999', 'x') is None


async def test_store_persists_across_restart(tmp_path: Path):
    key = _chat_key('group', '87654321')

    first = _new_store(tmp_path / 'data')
    await first.start()
    first.enqueue(_record(key, '重启前', ts=time.time()))
    await first.flush()
    await first.stop()

    second = _new_store(tmp_path / 'data')
    await second.start()
    try:
        # 会话列表来自冷启动时加载的缓存
        assert second.chat(key) is not None
        messages = await second.messages(key)
        assert [m.text for m in messages] == ['重启前']
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_search_finds_messages_by_content(store):
    """按内容搜索消息。"""
    key = _chat_key('group', '1')
    base = time.time()
    for i, text in enumerate(('今晚开会', '明天见', '开会改到周三')):
        store.enqueue(_record(key, text, ts=base + i))
    await store.flush()

    hits = await store.search('开会')
    assert {h.text for h in hits} == {'今晚开会', '开会改到周三'}


@pytest.mark.asyncio
async def test_search_escapes_like_wildcards(store):
    """``%`` 和 ``_`` 必须当字面量，否则搜一个 ``%`` 会把所有消息都捞出来。"""
    key = _chat_key('group', '1')
    base = time.time()
    for i, text in enumerate(
        ('进度 100% 了', '进度 100 了', 'under_score', 'underXscore')
    ):
        store.enqueue(_record(key, text, ts=base + i))
    await store.flush()

    pct = await store.search('%')
    assert [h.text for h in pct] == ['进度 100% 了'], '未转义的通配符会匹配到全部消息'
    und = await store.search('_')
    assert [h.text for h in und] == ['under_score']


@pytest.mark.asyncio
async def test_search_can_be_scoped_to_one_chat(store):
    """可以只在某个会话里搜。"""
    base = time.time()
    for i, key in enumerate((_chat_key('group', '1'), _chat_key('group', '2'))):
        store.enqueue(_record(key, '共同关键词', ts=base + i))
    await store.flush()

    assert len(await store.search('共同关键词')) == 2
    only = await store.search('共同关键词', chat_key=_chat_key('group', '1'))
    assert len(only) == 1
    assert only[0].chat_key == _chat_key('group', '1')


@pytest.mark.asyncio
async def test_search_ignores_empty_keyword(store):
    """空关键词直接返回空，避免退化成「列出全部」。"""
    key = _chat_key('group', '1')
    store.enqueue(_record(key, '随便一条', ts=time.time()))
    await store.flush()

    assert await store.search('') == []
    assert await store.search('   ') == []


@pytest.mark.asyncio
async def test_stream_messages_returns_ascending_batches(store):
    """导出用 stream_messages 从最早往后取，游标按 (ts, id) 严格向前。"""
    key = _chat_key('group', '1')
    base = time.time() - 100
    for i in range(5):
        store.enqueue(_record(key, f'第{i}条', ts=base + i))
    await store.flush()

    first = await store.stream_messages(key, limit=2)
    assert [m.text for m in first] == ['第0条', '第1条']

    second = await store.stream_messages(
        key, limit=2, after=first[-1].ts, after_id=first[-1].row_id
    )
    assert [m.text for m in second] == ['第2条', '第3条']

    third = await store.stream_messages(
        key, limit=10, after=second[-1].ts, after_id=second[-1].row_id
    )
    assert [m.text for m in third] == ['第4条']


@pytest.mark.asyncio
async def test_stream_messages_same_second_no_duplicates(store):
    """同一秒内的多条消息靠行号区分，翻页不会重复也不会漏。"""
    key = _chat_key('group', '1')
    base = time.time()
    for i in range(4):
        store.enqueue(_record(key, f'同秒{i}', ts=base))
    await store.flush()

    seen: list[str] = []
    after = None
    after_id = None
    while True:
        batch = await store.stream_messages(
            key, limit=2, after=after, after_id=after_id
        )
        if not batch:
            break
        seen.extend(m.text for m in batch)
        after = batch[-1].ts
        after_id = batch[-1].row_id
    assert seen == ['同秒0', '同秒1', '同秒2', '同秒3']


@pytest.mark.asyncio
async def test_forward_nodes_reads_inline_nodes(store):
    """合并转发若已内联节点，按 id 就能在记录里找到。"""
    from nonebot_plugin_botui.models import MessageRecord

    key = _chat_key('group', '1')
    nodes = [{'name': '小明', 'segments': [{'type': 'text', 'text': '内容'}]}]
    store.enqueue(
        MessageRecord(
            chat_key=key,
            chat_kind='group',
            chat_id='1',
            direction='in',
            ts=time.time(),
            segments=[{'type': 'forward', 'id': 'f1', 'nodes': nodes}],
        )
    )
    await store.flush()

    assert await store.forward_nodes(key, 'f1') == nodes
    assert await store.forward_nodes(key, '不存在') == []


@pytest.mark.asyncio
async def test_forward_nodes_without_chat_scans_recent(store):
    """不指定会话时只扫最近的一批消息，返回第一个命中的内联节点。"""
    from nonebot_plugin_botui.models import MessageRecord

    key = _chat_key('group', '1')
    nodes = [{'name': '小红', 'segments': [{'type': 'text', 'text': '跨会话'}]}]
    store.enqueue(
        MessageRecord(
            chat_key=key,
            chat_kind='group',
            chat_id='1',
            direction='in',
            ts=time.time(),
            segments=[{'type': 'forward', 'id': 'f2', 'nodes': nodes}],
        )
    )
    await store.flush()

    assert await store.forward_nodes(None, 'f2') == nodes
