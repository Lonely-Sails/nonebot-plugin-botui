"""纯逻辑单测：消息段转换、会话键、预览文本。"""

from __future__ import annotations

import pytest


def test_chat_key_roundtrip():
    from nonebot_plugin_botui.models import chat_key, split_chat_key

    key = chat_key('group', '87654321', '12345678')
    assert key == '12345678:group_87654321'
    assert split_chat_key(key) == ('group', '87654321')


def test_chat_key_with_self_id():
    """带机器人 ID 的会话键：同一群在不同机器人下是两条独立会话。"""
    from nonebot_plugin_botui.models import (
        chat_key,
        self_id_of,
        split_chat_key,
        split_chat_key_parts,
    )

    key = chat_key('group', '87654321', '12345678')
    assert key == '12345678:group_87654321'
    assert split_chat_key(key) == ('group', '87654321')
    assert split_chat_key_parts(key) == ('12345678', 'group', '87654321')
    assert self_id_of(key) == '12345678'


def test_split_chat_key_with_underscore_in_id():
    from nonebot_plugin_botui.models import split_chat_key

    # 群号/频道 id 里可能带下划线，只按第一个下划线切分
    assert split_chat_key('123:group_abc_def') == ('group', 'abc_def')


def test_describe_segments():
    from nonebot_plugin_botui.models import describe_segments

    segments = [
        {'type': 'at', 'target': '10001', 'name': '小明'},
        {'type': 'text', 'text': ' 你好'},
        {'type': 'image', 'url': 'http://x/y.jpg'},
    ]
    assert describe_segments(segments) == '@小明 你好[图片]'

    assert describe_segments([{'type': 'at', 'target': 'all'}]) == '@全体成员'
    assert describe_segments([{'type': 'unknown'}]) == '[消息]'
    assert describe_segments([]) == ''


def test_text_segments_roundtrip():
    """Message → 消息段：文本、@、图片、表情都要能正确识别。"""
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from nonebot_plugin_botui.segments import at_targets, to_segments, extract_text

    message = Message(
        [
            MessageSegment.at('10001'),
            MessageSegment.text(' 看这个'),
            MessageSegment.image('https://example.com/a.png'),
            MessageSegment.face(12),
        ]
    )
    segments = to_segments(message)
    kinds = [s['type'] for s in segments]
    assert kinds == ['at', 'text', 'image', 'face']

    at = segments[0]
    assert at['target'] == '10001'
    assert at_targets(segments) == ['10001']

    assert segments[1]['text'] == ' 看这个'
    assert segments[2]['url'] == 'https://example.com/a.png'
    assert segments[3]['id'] == '12'

    # 纯文本用于列表预览，会去掉首尾空白
    assert extract_text(message) == '看这个'


def test_at_all_segment():
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from nonebot_plugin_botui.segments import at_targets, to_segments

    message = Message([MessageSegment.at('all'), MessageSegment.text('集合')])
    segments = to_segments(message)
    assert segments[0] == {'type': 'at', 'target': 'all', 'name': '全体成员'}
    # 全体成员不算具体用户，不应出现在 at 列表里
    assert at_targets(segments) == []


def test_reply_segment():
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from nonebot_plugin_botui.segments import to_segments

    message = Message([MessageSegment.reply(555), MessageSegment.text('好的')])
    segments = to_segments(message)
    # 只给 id 时不应凭空造出用户/内容字段
    assert segments[0] == {'type': 'reply', 'id': '555'}


def test_reply_segment_keeps_quoted_summary():
    """reply 段自身带 sender / message 时要用它们。

    有些适配器会把被引用的人与正文直接塞进 reply 段数据里（OneBot V11 不是
    这样，它只放 id，真实结构见 ``test_reply_message_keeps_quoted_content``），
    这种情况也要能取出来。
    """
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from nonebot_plugin_botui.segments import to_segments

    raw = MessageSegment(
        'reply',
        {
            'id': '555',
            'sender': {'user_id': 10001, 'nickname': '小明', 'card': '小明'},
            'message': [{'type': 'text', 'data': {'text': '原来的话'}}],
        },
    )
    message = Message([raw, MessageSegment.text('好的')])
    segments = to_segments(message)
    reply = segments[0]
    assert reply['type'] == 'reply'
    assert reply['id'] == '555'
    assert reply['name'] == '小明'
    assert reply['text'] == '原来的话'
    assert reply['preview'] == '原来的话'


def test_reply_fallback_path_keeps_quoted_summary():
    """走原始段兜底路径（uniseg 不可用）时同样要带上引用摘要。"""
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from nonebot_plugin_botui.segments import _fallback_segments

    raw = MessageSegment(
        'reply',
        {
            'id': '777',
            'sender': {'user_id': 10002, 'nickname': '小红'},
            'message': [{'type': 'text', 'data': {'text': '被引用的内容'}}],
        },
    )
    segments = _fallback_segments(Message([raw]))
    assert segments[0]['id'] == '777'
    assert segments[0]['name'] == '小红'
    assert segments[0]['preview'] == '被引用的内容'


@pytest.mark.asyncio
async def test_reply_summary_filled_from_event():
    """reply 段只有 id 时，要从事件的 ``reply`` 字段把引用摘要补回来。

    这正是 OneBot V11 的真实结构：段里只有 id，被引用的人与正文在事件上。
    不补的话收进来的引用消息在界面上只剩「回复 555」。
    """
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from nonebot_plugin_botui.segments import to_segments_async

    class _FakeEvent:
        def __init__(self) -> None:
            self.reply = {
                'sender': {'user_id': 10002, 'nickname': '小红'},
                'message': [{'type': 'text', 'data': {'text': '中午一起吃饭吗'}}],
            }

    message = Message([MessageSegment.reply(555), MessageSegment.text('好啊')])
    segments = await to_segments_async(message, event=_FakeEvent())
    reply = next(s for s in segments if s['type'] == 'reply')
    assert reply['id'] == '555'
    assert reply['name'] == '小红'
    assert reply['preview'] == '中午一起吃饭吗'


def test_unknown_segment_is_kept():
    """适配器私有消息段要保留原始信息，供前端兜底展示。"""
    from nonebot.adapters.onebot.v11 import MessageSegment

    from nonebot_plugin_botui.segments import to_segments

    segments = to_segments(MessageSegment('music', {'type': 'qq', 'id': '1'}))
    assert segments
    assert segments[0]['type'] in {'music', 'unknown'}


def test_audio_segment_is_not_dropped():
    """uniseg 的 ``Audio`` 段不能被当成未知段丢掉（不能只认 Voice）。"""
    from nonebot.adapters.onebot.v11 import MessageSegment

    from nonebot_plugin_botui.segments import to_segments

    segments = to_segments(
        MessageSegment('record', {'file': 'x.amr', 'url': 'http://e/a.amr'})
    )
    assert segments
    assert segments[0]['type'] in {'voice', 'audio'}
    assert segments[0]['url'] == 'http://e/a.amr'


def test_emoji_keyboard_and_button_segments():
    """emoji / keyboard / button 段都要能转出来，且带上可读标签。"""
    from nonebot_plugin_alconna.uniseg import Button, Keyboard
    from nonebot_plugin_alconna.uniseg.segment import Emoji

    from nonebot_plugin_botui.segments import segment_to_dict

    face = segment_to_dict(Emoji(id='66', name='微笑'))
    assert face is not None
    assert face['type'] == 'face'
    assert face['id'] == '66'

    button = segment_to_dict(Button('action', '点我'))
    assert button is not None
    assert button['type'] == 'button'
    assert button['label'] == '点我'

    keyboard = segment_to_dict(Keyboard([Button('action', 'A'), Button('action', 'B')]))
    assert keyboard is not None
    assert keyboard['type'] == 'button'
    assert 'A' in (keyboard['label'] or '')
    assert 'B' in (keyboard['label'] or '')


def test_describe_segments_labels_new_types():
    """新增段类型在聊天列表预览里要有可读标签，而不是退回「[消息]」。"""
    from nonebot_plugin_botui.models import describe_segments

    assert describe_segments([{'type': 'audio'}]) == '[音频]'
    assert describe_segments([{'type': 'button'}]) == '[按钮]'


def test_forward_segment_fallback_keeps_id():
    """兜底路径遇到 forward 段要保留 id，而不是当成未知段丢掉。"""
    from nonebot_plugin_botui.segments import _fallback_segments

    segments = _fallback_segments([{'type': 'forward', 'data': {'id': 'fwd-9'}}])
    assert segments[0]['type'] == 'forward'
    assert segments[0]['id'] == 'fwd-9'


def test_parse_forward_nodes_from_get_forward_msg():
    """``get_forward_msg`` 的返回值要能展开成统一的节点列表。"""
    from nonebot_plugin_botui.segments import parse_forward_nodes

    payload = {
        'messages': [
            {
                'type': 'node',
                'data': {
                    'user_id': '10001',
                    'nickname': '小明',
                    'time': 1700000000,
                    'content': [{'type': 'text', 'data': {'text': '第一句'}}],
                },
            },
            {
                'type': 'node',
                'data': {
                    'user_id': '10002',
                    'nickname': '小红',
                    'time': 1700000001,
                    'content': [
                        {'type': 'image', 'data': {'url': 'http://e/a.png'}},
                        {'type': 'text', 'data': {'text': '看图'}},
                    ],
                },
            },
        ]
    }
    nodes = parse_forward_nodes(payload)
    assert len(nodes) == 2
    assert nodes[0]['name'] == '小明'
    assert nodes[0]['user_id'] == '10001'
    assert nodes[0]['time'] == 1700000000.0
    assert nodes[0]['segments'] == [{'type': 'text', 'text': '第一句'}]
    # 第二条里的图片段也要能转出来（展开后仍可预览）
    assert [s['type'] for s in nodes[1]['segments']] == ['image', 'text']


def test_parse_forward_nodes_accepts_plain_dict_and_list():
    """直接是单个节点 dict，或节点列表，都要能展开（不同适配器形状不一）。"""
    from nonebot_plugin_botui.segments import parse_forward_nodes

    single = parse_forward_nodes(
        {'nickname': '小明', 'content': [{'type': 'text', 'data': {'text': 'hi'}}]}
    )
    assert len(single) == 1
    assert single[0]['segments'] == [{'type': 'text', 'text': 'hi'}]

    listed = parse_forward_nodes(
        [
            {'nickname': 'A', 'message': [{'type': 'text', 'data': {'text': 'a'}}]},
            {'nickname': 'B', 'message': [{'type': 'text', 'data': {'text': 'b'}}]},
        ]
    )
    assert [n['name'] for n in listed] == ['A', 'B']


def test_parse_forward_nodes_handles_garbage():
    """认不出的输入返回空列表，不能抛异常。"""
    from nonebot_plugin_botui.segments import parse_forward_nodes

    assert parse_forward_nodes(None) == []
    assert parse_forward_nodes(12345) == []
    assert parse_forward_nodes({'unexpected': 'shape'}) == []


def test_fallback_segments_accepts_dict_segments():
    """兜底路径要同时认适配器段对象与纯 dict（call_api 结果常是 dict）。"""
    from nonebot_plugin_botui.segments import _fallback_segments

    single = _fallback_segments({'type': 'text', 'data': {'text': '你好'}})
    assert single == [{'type': 'text', 'text': '你好'}]

    mixed = _fallback_segments(
        [
            {'type': 'text', 'data': {'text': 'a'}},
            {
                'type': 'image',
                'data': {'url': 'http://e/b.png', 'file': 'b.png'},
            },
        ]
    )
    assert [s['type'] for s in mixed] == ['text', 'image']
    assert mixed[1]['url'] == 'http://e/b.png'


def test_media_helpers_classify_and_decode():
    """文本判定与解码要覆盖常见类型 / 编码。"""
    from nonebot_plugin_botui.media import is_textual, decode_text

    assert is_textual('text/plain')
    assert is_textual('application/json')
    assert is_textual('application/octet-stream', 'notes.md')
    assert is_textual('application/octet-stream', 'http://e/a/b.py')
    assert not is_textual('image/png', 'a.png')
    assert not is_textual('application/zip', 'a.zip')

    assert decode_text('中文'.encode())[0] == '中文'
    assert decode_text('中文'.encode('gb18030'))[0] == '中文'
    # 带 BOM 的 UTF-16 走 BOM 分支
    assert decode_text('中文'.encode('utf-16'))[0] == '中文'
    # 二进制乱码也不能抛异常
    assert isinstance(decode_text(bytes([0xFF, 0xFE, 0x00, 0x01]))[0], str)


def test_media_total_bytes_parsing():
    """总字节数优先看 Content-Range，其次 Content-Length。"""
    from nonebot_plugin_botui.media import _total_bytes

    assert _total_bytes({'content-range': 'bytes 0-511/1048576'}) == 1048576
    assert _total_bytes({'content-length': '2048'}) == 2048
    assert _total_bytes({}) == 0


def test_resolve_file_name_from_url():
    """QQ 适配器的 File 段只给 url、name 恒为 file.bin，要从链接里还原真名。"""
    from nonebot_plugin_botui.segments import resolve_file_name

    # 查询串优先：fname 里的 + 是编码后的空格
    qq = (
        'https://njc-download.ftn.qq.com/ftn_handler/524d7bd623'
        '?fname=404+-+%E9%A1%B5%E9%9D%A2%E6%9C%AA%E6%89%BE%E5%88%B0.html'
    )
    assert resolve_file_name('file.bin', qq) == '404 - 页面未找到.html'
    assert resolve_file_name(None, qq) == '404 - 页面未找到.html'
    # 段里给了真实名字时优先用段里的
    assert resolve_file_name('报表.xlsx', qq) == '报表.xlsx'

    # 没有查询串时退回路径末段
    assert (
        resolve_file_name('file.bin', 'https://e.com/a/b/%E6%8A%A5%E5%91%8A.pdf')
        == '报告.pdf'
    )
    assert resolve_file_name('file.bin', 'https://e.com/dl/photo.jpg') == 'photo.jpg'

    # 占位名与拿不到名字时给兜底
    assert resolve_file_name('file.bin', 'https://x.io/download') == '文件'
    assert resolve_file_name(None, '') == '文件'
    assert resolve_file_name('file.bin', '') == '文件'
    # 非法字符被清掉，路径分隔符不会留下来
    assert resolve_file_name('a/b\\c.txt', '') == 'abc.txt'


def test_guess_mime_from_url():
    """按链接里的文件名猜 MIME，供界面挑预览方式。"""
    from nonebot_plugin_botui.segments import guess_mime

    assert guess_mime('https://e.com/x/报告.pdf') == 'application/pdf'
    assert guess_mime('https://e.com/x/a.png') == 'image/png'
    assert guess_mime('https://e.com/x/download') is None
    assert guess_mime('') is None


def test_file_segment_uses_url_name():
    """整段转换时 File 段应输出还原后的文件名与 MIME。"""
    from nonebot_plugin_botui.segments import segment_to_dict

    class _File:
        type = 'file'
        name = 'file.bin'
        url = 'https://e.com/a/%E6%8A%A5%E5%91%8A.pdf'
        file = ''
        mimetype = 'application/octet-stream'

    from nonebot_plugin_alconna.uniseg.segment import File

    seg = File(
        url='https://e.com/a/%E6%8A%A5%E5%91%8A.pdf',
        name='file.bin',
    )
    out = segment_to_dict(seg)
    assert out is not None
    assert out['type'] == 'file'
    assert out['name'] == '报告.pdf'
    assert out['mime'] == 'application/pdf'


def test_fallback_file_segment_uses_url_name():
    """兜底路径同样要还原文件名。"""
    from nonebot_plugin_botui.segments import _fallback_segments

    out = _fallback_segments(
        {
            'type': 'file',
            'data': {
                'url': 'https://e.com/dl?filename=%E5%B9%B4%E6%8A%A5.xlsx',
                'file': 'file.bin',
            },
        }
    )
    assert out[0]['type'] == 'file'
    assert out[0]['name'] == '年报.xlsx'
    assert out[0]['mime'] == (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


def test_config_route_normalization():
    from nonebot_plugin_botui.config import Config

    assert Config(botui_route='botui').botui_route == '/botui'
    assert Config(botui_route='/botui/').botui_route == '/botui'
    assert Config(botui_route='/').botui_route == '/'
    # 分页大小会被收敛到合理范围
    assert Config(botui_page_size=1).botui_page_size == 10
    assert Config(botui_page_size=9999).botui_page_size == 200


@pytest.mark.parametrize(
    ('api', 'data', 'expected'),
    [
        # OneBot V11
        (
            'send_group_msg',
            {'group_id': 111, 'message': 'x'},
            ('12345678:group_111', '111'),
        ),
        (
            'send_private_msg',
            {'user_id': 222, 'message': 'x'},
            ('12345678:private_222', '222'),
        ),
        # OneBot V12：接口统一叫 send_message，靠 detail_type 区分
        (
            'send_message',
            {'detail_type': 'group', 'group_id': '333', 'message': 'x'},
            ('12345678:group_333', '333'),
        ),
        (
            'send_message',
            {'detail_type': 'private', 'user_id': '444', 'message': 'x'},
            ('12345678:private_444', '444'),
        ),
        # 频道（guild/channel）也算群聊场景
        (
            'send_message',
            {'channel_id': '555', 'message': 'x'},
            ('12345678:group_555', '555'),
        ),
        # 认不出目标时返回 None，采集层会跳过而不是记到错误的会话里
        ('send_message', {'message': 'x'}, None),
        # 群文件/群公告这类接口**没有**会话参数，参数里的数字是文件名、
        # 文件夹序号或公告标题。以前会按「接口名含 group 就挑第一个数字字段」
        # 去猜，于是聊天列表里冒出一堆幽灵会话（例如公告标题叫 2024 就多一个
        # group_2024）。
        ('upload_group_file', {'file': '/tmp/a.txt', 'name': '123'}, None),
        ('send_group_notice', {'content': 'hi', 'name': '2024'}, None),
        ('upload_group_file', {'file': 'x', 'folder': '999'}, None),
    ],
)
def test_extract_target_across_adapters(api, data, expected):
    """跨平台的关键：不同适配器的发送参数形状差别很大，都要能认出会话。"""
    from nonebot_plugin_botui.capture import _extract_target

    assert _extract_target(api, data, '12345678') == expected


def test_version_is_consistent_across_files():
    """版本号出现在多处，必须一致。

    ``pyproject.toml`` 的 ``[project] version``、``[tool.bumpversion]`` 的
    ``current_version`` 和 ``api.VERSION`` 一旦不同步，``/api/meta`` 报给
    前端的版本就会长期停在旧值（界面右下角显示的就是它）。
    ``[tool.bumpversion.files]`` 负责在 bump 时一起改，这个测试负责兜底。
    """
    import re
    from pathlib import Path

    import tomllib

    root = Path(__file__).parent.parent
    data = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))

    project_version = data['project']['version']
    bump_version = data['tool']['bumpversion']['current_version']
    assert project_version == bump_version, (
        f'[project] version={project_version!r} 与 '
        f'[tool.bumpversion] current_version={bump_version!r} 不一致'
    )

    # PEP 440 不允许 v 前缀，带了会导致构建/比较出问题
    assert not project_version.startswith('v'), '版本号不要带 v 前缀'

    api_src = (root / 'src' / 'nonebot_plugin_botui' / 'api.py').read_text(
        encoding='utf-8'
    )
    match = re.search(r"^VERSION = '([^']+)'", api_src, re.M)
    assert match, 'api.py 里没找到 VERSION 定义'
    assert match.group(1) == project_version, (
        f'api.py 的 VERSION={match.group(1)!r} 与 pyproject.toml 的 '
        f'{project_version!r} 不一致（/api/meta 会报错版本）'
    )

    # bumpversion 必须覆盖这两个文件，否则 bump 之后又会漂移
    filenames = {f['filename'] for f in data['tool']['bumpversion']['files']}
    assert 'pyproject.toml' in filenames
    assert 'src/nonebot_plugin_botui/api.py' in filenames


def test_segments_of_payload_branches(app, botui):
    """待发送内容的各种形状都要能转成消息段。

    注意 ``Message`` 本身是 ``list`` 的子类，分支顺序写错会把适配器消息
    当成普通列表处理；这里把每种形状都试一遍。
    """
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    from src.nonebot_plugin_botui.capture import _segments_of_payload

    adapter = nonebot.get_adapter(OnebotV11Adapter)
    bot = Bot(adapter, self_id='12345678')

    # 纯文本
    assert _segments_of_payload('你好', bot) == [{'type': 'text', 'text': '你好'}]

    # 单个消息段（不是 list，需要显式包一层）
    seg = _segments_of_payload(MessageSegment.text('hi'), bot)
    assert seg
    assert seg[0]['type'] == 'text'

    # 适配器 Message（list 的子类，必须先于 list 判断）
    msg = _segments_of_payload(Message('你好'), bot)
    assert msg
    assert msg[0]['type'] == 'text'

    # 普通列表（元素是消息段）：交给适配器的 Message 类还原
    plain = _segments_of_payload([MessageSegment.text('ok')], bot)
    assert plain
    assert plain[0]['type'] == 'text'

    # 元素是 dict 的普通列表：OneBot 的 Message 不接受 dict，这里会走
    # 兜底分支——不能抛异常，退回 unknown
    mixed = _segments_of_payload([{'type': 'text', 'data': {'text': 'x'}}], bot)
    assert mixed
    assert mixed[0]['type'] == 'unknown'

    # 认不出的东西同理
    assert _segments_of_payload(12345, bot)[0]['type'] == 'unknown'


def test_every_config_field_is_documented():
    """新增配置项时必须同步更新 README 表格和 .env.example。

    这个项目已经出现过「加了 ``BOTUI_MEDIA_ALLOW_PRIVATE`` 但文档表格没跟上」
    的情况，用户按文档配不出这个开关，所以这里把它固化下来。
    """
    import re as _re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    source = (root / 'src/nonebot_plugin_botui/config.py').read_text(encoding='utf-8')
    body = source.split('class Config(BaseModel):', 1)[1]
    fields = {f.upper() for f in _re.findall(r'^\s{4}(botui_\w+)\s*:', body, _re.M)}

    readme = (root / 'README.md').read_text(encoding='utf-8')
    documented = set(_re.findall(r'^\| `(BOTUI_[A-Z_]+)`', readme, _re.M))

    example = (root / '.env.example').read_text(encoding='utf-8')
    in_example = set(_re.findall(r'^#?\s*(BOTUI_[A-Z_]+)=', example, _re.M))

    assert fields, '没能从 config.py 里解析出配置项，检查一下解析逻辑'
    assert fields == documented, (
        f'README 配置表与模型不一致：缺 {fields - documented}，多 {documented - fields}'
    )
    assert fields == in_example, f'.env.example 与模型不一致：缺 {fields - in_example}'
