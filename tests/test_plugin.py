"""nonebug 集成测试：消息采集与消息发送记录。

插件**不注册任何聊天指令**，所以这些用例不再通过 ``app.test_matcher(matcher)``
触发，而是直接 ``app.test_matcher()`` 把事件喂进 NoneBot 的事件管道 —— 采集靠的是
``event_preprocessor`` 钩子，它不依赖任何 matcher 存在。

注意：插件在测试里是以 ``src.nonebot_plugin_botui`` 这个模块路径加载的
（``[tool.nonebot] plugin_dirs = ["src/"]``），所以这里统一通过 ``botui`` 夹具
拿到模块对象，避免导入出第二个互不相干的模块。
"""

from __future__ import annotations

import pytest
from fake import fake_group_message_event_v11, fake_private_message_event_v11
from nonebug import App

BOT_ID = '12345678'
GROUP_ID = 87654321
USER_ID = 10001

GROUP_INFO = {'group_id': GROUP_ID, 'group_name': '测试群', 'member_count': 3}
MEMBER_INFO = {
    'group_id': GROUP_ID,
    'user_id': USER_ID,
    'nickname': '小明',
    'card': '小明',
    'role': 'member',
    'join_time': 0,
    'sex': 'unknown',
}


def _chat_key(kind: str, chat_id: str) -> str:
    from src.nonebot_plugin_botui.models import chat_key

    # 会话 key 带上机器人 ID，与采集时写入的格式保持一致
    return chat_key(kind, chat_id, BOT_ID)


def _mock_uninfo(ctx, user_id: int = USER_ID):
    """uninfo 识别群聊会话时会拉取群信息与群成员信息。"""
    ctx.should_call_api('get_group_info', {'group_id': GROUP_ID}, result=GROUP_INFO)
    ctx.should_call_api(
        'get_group_member_info',
        {'group_id': GROUP_ID, 'user_id': user_id, 'no_cache': True},
        result={**MEMBER_INFO, 'user_id': user_id},
    )


def _make_bot(ctx):
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    adapter = nonebot.get_adapter(OnebotV11Adapter)
    return ctx.create_bot(base=Bot, adapter=adapter, self_id=BOT_ID)


@pytest.mark.asyncio
async def test_message_is_captured(app: App, botui):
    """机器人收到的群消息会被写入存储。"""
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    store = botui._get_store()
    key = _chat_key('group', str(GROUP_ID))

    message = Message([MessageSegment.at(BOT_ID), MessageSegment.text(' 你好呀')])
    event = fake_group_message_event_v11(message=message, to_me=True)

    before = await store.count()
    async with app.test_matcher() as ctx:
        bot = _make_bot(ctx)
        _mock_uninfo(ctx)
        ctx.receive_event(bot, event)

    await store.flush()
    assert await store.count() == before + 1

    messages = await store.messages(key, limit=5)
    last = messages[-1]
    assert last.direction == 'in'
    assert last.text == '你好呀'
    assert last.user_id == str(USER_ID)
    assert last.user_name == '小明'
    assert last.chat_name == '测试群'
    # @ 机器人的消息段也要保留下来
    assert any(s['type'] == 'at' and s['target'] == BOT_ID for s in last.segments)

    chat = store.chat(key)
    assert chat is not None
    assert chat.last_direction == 'in'


@pytest.mark.asyncio
async def test_own_message_is_not_recorded_as_incoming(app: App, botui):
    """机器人自己发的消息被回传成事件时，不应该再记一条「收到」。

    BOTUI_CAPTURE_SELF 默认关闭就是为了这个：同一条消息已经由发送钩子
    记成 out 了，再记一条 in 界面上就会看到两份。
    """
    store = botui._get_store()
    key = _chat_key('group', str(GROUP_ID))

    # user_id 就是机器人自己 —— 模拟适配器把机器人发出的消息回传
    event = fake_group_message_event_v11(message='我自己发的', user_id=int(BOT_ID))

    before = await store.count()
    async with app.test_matcher() as ctx:
        bot = _make_bot(ctx)
        _mock_uninfo(ctx, user_id=int(BOT_ID))
        ctx.receive_event(bot, event)

    await store.flush()
    assert await store.count() == before
    assert all(m.text != '我自己发的' for m in await store.messages(key, limit=20))


@pytest.mark.asyncio
async def test_multi_segment_message_is_captured(app: App, botui):
    """图文混合的消息要按段完整记录。"""
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    store = botui._get_store()
    key = _chat_key('group', str(GROUP_ID))

    message = Message(
        [
            MessageSegment.text('看这张图'),
            MessageSegment.image('https://example.com/cat.png'),
            MessageSegment.face(66),
        ]
    )
    event = fake_group_message_event_v11(message=message)

    async with app.test_matcher() as ctx:
        bot = _make_bot(ctx)
        _mock_uninfo(ctx)
        ctx.receive_event(bot, event)

    await store.flush()
    messages = await store.messages(key, limit=3)
    last = messages[-1]
    assert [s['type'] for s in last.segments] == ['text', 'image', 'face']
    assert last.text == '看这张图'


@pytest.mark.asyncio
async def test_reply_message_keeps_quoted_content(app: App, botui):
    """带引用的消息要连同「回复了谁、回复了什么」一起记录。

    只存一个 reply id 的话，WebUI 上只能显示「回复 555」，看不出上下文。

    这里刻意还原 OneBot V11 的真实结构：reply 段里**只有 id**，
    被引用的发送者与正文挂在事件的 ``reply`` 字段上 —— 也正是这一点让
    收进来的引用消息在界面上退化成了「回复 <id>」。
    """
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    from nonebot.adapters.onebot.v11.event import Reply, Sender

    store = botui._get_store()
    key = _chat_key('group', str(GROUP_ID))

    event = fake_group_message_event_v11(
        message=Message(
            [MessageSegment('reply', {'id': '555'}), MessageSegment.text('好啊')]
        ),
        reply=Reply(
            time=1000000,
            message_type='group',
            message_id=555,
            real_id=555,
            sender=Sender(user_id=10002, nickname='小红', card='小红'),
            message=Message('中午一起吃饭吗'),
        ),
    )

    async with app.test_matcher() as ctx:
        bot = _make_bot(ctx)
        _mock_uninfo(ctx)
        ctx.receive_event(bot, event)

    await store.flush()
    last = (await store.messages(key, limit=3))[-1]
    assert last.text == '好啊'
    reply = next(s for s in last.segments if s['type'] == 'reply')
    assert reply['id'] == '555'
    assert reply['name'] == '小红'
    assert reply['preview'] == '中午一起吃饭吗'


@pytest.mark.asyncio
async def test_member_name_is_backfilled_from_at_resolution(
    app: App, botui, monkeypatch
):
    """@ 某人时解析到的群名片要回填进成员名册，WebUI 的 @ 菜单才有名字可选。

    测试环境默认关掉了 ``BOTUI_RESOLVE_AT_NAME``（免得每个用例都要 mock 接口），
    这里显式打开。注意要改 ``botui`` 这个插件模块上的 cfg —— 也就是采集钩子
    实际持有的那个配置对象；直接动 ``capture`` 模块的全局变量不可靠（见下面
    关于模块别名的说明）。
    """
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    monkeypatch.setattr(botui.cfg, 'botui_resolve_at_name', True, raising=False)

    store = botui._get_store()
    key = _chat_key('group', str(GROUP_ID))

    event = fake_group_message_event_v11(
        message=Message([MessageSegment.at('10002'), MessageSegment.text(' 你好')])
    )

    async with app.test_matcher() as ctx:
        bot = _make_bot(ctx)
        _mock_uninfo(ctx)
        # uninfo 拉完发消息者的成员信息后，解析 @ 时还会再拉一次被 @ 的人
        ctx.should_call_api(
            'get_group_member_info',
            {'group_id': GROUP_ID, 'user_id': 10002, 'no_cache': True},
            result={
                **MEMBER_INFO,
                'user_id': 10002,
                'nickname': '小红',
                'card': '小红',
            },
        )
        ctx.receive_event(bot, event)

    await store.flush()
    members = {m.user_id: m for m in store.members(key)}
    assert members['10002'].name == '小红'
    last = (await store.messages(key, limit=3))[-1]
    at = next(s for s in last.segments if s['type'] == 'at')
    assert at['name'] == '小红'


@pytest.mark.asyncio
async def test_private_message_is_captured(app: App, botui):
    store = botui._get_store()
    key = _chat_key('private', str(USER_ID))

    event = fake_private_message_event_v11(message='私聊消息')

    async with app.test_matcher() as ctx:
        bot = _make_bot(ctx)
        ctx.receive_event(bot, event)

    await store.flush()
    messages = await store.messages(key, limit=5)
    assert messages
    assert messages[-1].text == '私聊消息'
    assert messages[-1].chat_kind == 'private'

    chat = store.chat(key)
    assert chat is not None
    assert chat.kind == 'private'


@pytest.mark.asyncio
async def test_sent_message_is_captured(app: App, botui):
    """机器人通过 send_msg 发出的消息同样会被记录，并且可撤回。"""
    from nonebot.adapters.onebot.v11 import Message

    store = botui._get_store()
    key = _chat_key('group', str(GROUP_ID))

    async with app.test_api() as ctx:
        bot = _make_bot(ctx)
        ctx.should_call_api(
            'send_msg',
            {
                'message_type': 'group',
                'group_id': GROUP_ID,
                'message': Message('机器人说话'),
            },
            result={'message_id': 4242, 'status': 'ok'},
        )
        await bot.send_msg(
            message_type='group',
            group_id=GROUP_ID,
            message=Message('机器人说话'),
        )

    await store.flush()
    messages = await store.messages(key, limit=10)
    sent = [m for m in messages if m.direction == 'out']
    assert sent, '发出的消息没有被记录'
    last = sent[-1]
    assert last.text == '机器人说话'
    assert last.is_self is True
    assert last.message_id == '4242'
    assert last.recallable is True


@pytest.mark.asyncio
async def test_mount_rejects_non_asgi_driver(app: App, botui, monkeypatch):
    """非服务端型驱动器下 mount() 应返回 False，而不是把断言错误抛出去。

    ``nonebot.get_app()`` 内部会 assert 驱动是 ASGIMixin，所以插件必须先自己
    判断，才能给出一条可读的日志。
    """
    import nonebot
    from nonebot.drivers import ASGIMixin

    driver = nonebot.get_driver()
    assert isinstance(driver, ASGIMixin), '测试固定跑在 FastAPI 驱动上'

    class _NotASGI:
        config = driver.config

    monkeypatch.setattr(nonebot, 'get_driver', lambda: _NotASGI())

    server = botui._get_server()
    mounted = server.mounted
    server.mounted = False
    try:
        assert server.mount() is False
    finally:
        server.mounted = mounted


@pytest.mark.asyncio
async def test_disabled_plugin_does_not_mount(app: App, botui, monkeypatch):
    """BOTUI_ENABLED=false 时不能把 WebUI 挂起来。"""
    server = botui._get_server()
    cfg = botui.cfg
    was_mounted = server.mounted
    monkeypatch.setattr(cfg, 'botui_enabled', False, raising=False)
    server.mounted = False
    try:
        assert botui._try_mount() is False, '关闭状态下不应挂载'
        assert server.mounted is False
    finally:
        server.mounted = was_mounted


@pytest.mark.asyncio
async def test_enabled_plugin_mounts(app: App, botui, monkeypatch):
    """开关打开时挂载照常工作（确认上面那条不是因为别的原因失败）。"""
    server = botui._get_server()
    cfg = botui.cfg
    was_mounted = server.mounted
    monkeypatch.setattr(cfg, 'botui_enabled', True, raising=False)
    server.mounted = False
    try:
        assert botui._try_mount() is True
        assert server.mounted is True
    finally:
        server.mounted = was_mounted


@pytest.mark.asyncio
async def test_host_exception_handler_is_preserved(app: App, botui, monkeypatch):
    """挂载时不能把宿主已注册的异常处理器「顶掉」。

    Starlette 的 add_exception_handler 只是往 dict 里赋值、没有链式调用，
    直接注册会把宿主的定制静默替换掉；插件应当把原处理器接过去，在非 BotUI
    路径上转交给它。
    """
    import nonebot
    from starlette.exceptions import HTTPException as StarletteHTTPException

    server = botui._get_server()
    was_mounted = server.mounted
    monkeypatch.setattr(botui.cfg, 'botui_enabled', True, raising=False)

    async def host_handler(request, exc):  # pragma: no cover - 只用于比对
        raise AssertionError('不该真的被调用')

    starlette_app = nonebot.get_app()
    # 记录宿主当前的处理器，然后模拟「宿主先注册过自己的」
    original = starlette_app.exception_handlers.get(StarletteHTTPException)
    starlette_app.exception_handlers[StarletteHTTPException] = host_handler
    server.mounted = False
    try:
        assert server.mount() is True
        current = starlette_app.exception_handlers[StarletteHTTPException]
        # 现在注册的应该是 BotUI 的包装器（它会转交给 host_handler）
        assert current is not host_handler, 'BotUI 应当注册自己的处理器'
        closure = [c.cell_contents for c in (current.__closure__ or ())]
        assert host_handler in closure, 'BotUI 必须把宿主的处理器保存下来转交'
    finally:
        server.mounted = was_mounted
        if original is not None:
            starlette_app.exception_handlers[StarletteHTTPException] = original


@pytest.mark.asyncio
async def test_plugin_registers_no_matchers(app: App, botui):
    """插件不能注册任何 matcher —— 不占命令名，也不拦截用户消息。

    这是刻意的设计：早期版本注册过 ``帮助`` / ``状态`` / ``链接`` 这类又短又
    通用的别名，很容易和别人的插件撞车。入口改为启动时打印到控制台日志。
    """
    from nonebot.matcher import matchers

    assert dict(matchers) == {}, f'插件不应该注册任何 matcher，实际有 {dict(matchers)}'


@pytest.mark.asyncio
async def test_plain_text_does_not_trigger_anything(app: App, botui):
    """以前能被口令唤醒的文本，现在什么都不做 —— 不会有人回复。

    采集本身照常进行（那是 ``event_preprocessor`` 干的），所以这里只断言
    「没有新增的 out 消息」，也就是插件没有拿这些话当指令去回消息。
    """
    store = botui._get_store()
    key = _chat_key('group', str(GROUP_ID))

    # 用最大行号作基线：共享存储里可能已经有别的用例留下的消息，
    # 按「行号更大」筛选就只看得见本次新增的。
    await store.flush()
    baseline = max((m.row_id for m in await store.messages(key, limit=500)), default=0)

    # uninfo 会缓存会话信息，每次都要清一遍，否则后几次不会真的去调适配器接口，
    # 而 nonebug 要求声明的 API 调用必须全部发生。
    from nonebot_plugin_uninfo.adapters import INFO_FETCHER_MAPPING

    texts = ('界面', 'botui', '控制台', 'botui 链接', '界面帮助')
    for text in texts:
        for fetcher in INFO_FETCHER_MAPPING.values():
            fetcher.clean()
        event = fake_group_message_event_v11(message=text)
        async with app.test_matcher() as ctx:
            bot = _make_bot(ctx)
            _mock_uninfo(ctx)
            ctx.receive_event(bot, event)

    await store.flush()
    added = [m for m in await store.messages(key, limit=500) if m.row_id > baseline]
    assert [m.text for m in added] == list(texts), '消息本身仍然应该被采集'
    assert all(m.direction == 'in' for m in added), '这些话不该再触发任何回复'


# ── 机器人展示信息 ──────────────────────────────────────────────────────
def test_bot_info_never_returns_partial(botui):
    """机器人名字绝不能是 ``functools.partial(...)`` 那串。

    NoneBot 的基类 ``Bot`` 重写了 ``__getattr__``：任何**不存在**的属性都会被
    当成 API 调用返回 ``partial(bot.call_api, name)``。早期 ``_bot_info`` 用
    ``getattr(bot, 'nickname', '')`` 兜底取昵称，默认值形同虚设，界面上就出现
    了 ``functools.partial(<bound method Bot.call_api of Bot(...)>, 'nickname')``。
    """
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    adapter = nonebot.get_adapter(OnebotV11Adapter)
    bot = Bot(adapter, self_id=BOT_ID)

    info = botui._bot_info(bot)
    assert info['self_id'] == BOT_ID
    assert 'functools.partial' not in str(info.get('name', ''))
    assert 'call_api' not in str(info.get('name', ''))
    # 取不到真实昵称时宁可为空，交给前端用 self_id 顶替
    assert info.get('name', '') == ''


def test_bot_info_reads_self_info_dict(botui):
    """适配器把昵称挂在 ``bot.self_info`` 上时要能读到。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    adapter = nonebot.get_adapter(OnebotV11Adapter)

    class SelfInfoBot(Bot):
        def __init__(self, adapter, self_id):
            super().__init__(adapter, self_id)
            self.self_info = {'nickname': '小可爱', 'avatar': 'http://x/y.png'}

    info = botui._bot_info(SelfInfoBot(adapter, BOT_ID))
    assert info['name'] == '小可爱'
    assert info['avatar'] == 'http://x/y.png'


def test_bot_info_never_calls_api(botui):
    """``_bot_info`` 是纯属性读取，不该主动调适配器接口。

    连接钩子里多发一次 API 调用会打乱调用方的调用序列（例如 nonebug 测试里
    ``should_call_api`` 的期望顺序），所以这里用「一调就炸」的 bot 钉死行为。
    """
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    adapter = nonebot.get_adapter(OnebotV11Adapter)

    class BoomBot(Bot):
        async def call_api(self, api, **kwargs):
            raise AssertionError('不该调用适配器接口')

    info = botui._bot_info(BoomBot(adapter, BOT_ID))
    assert info['self_id'] == BOT_ID
    assert info.get('name', '') == ''
