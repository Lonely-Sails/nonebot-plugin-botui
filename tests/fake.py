from typing import TYPE_CHECKING, Literal
from itertools import count

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent as GroupMessageEventV11
    from nonebot.adapters.onebot.v11 import (
        PrivateMessageEvent as PrivateMessageEventV11,
    )

# alconna 会用 message_id 作为键缓存解析后的消息（UniMessage），
# 所以每个假事件都必须有唯一的 message_id，否则用例之间会串味。
_SEQ = count(1000)


def fake_group_message_event_v11(**field) -> 'GroupMessageEventV11':
    from pydantic import create_model
    from nonebot.adapters.onebot.v11 import Message, GroupMessageEvent
    from nonebot.adapters.onebot.v11.event import Reply, Sender

    _Fake = create_model('_Fake', __base__=GroupMessageEvent)

    class FakeEvent(_Fake):
        time: int = 1000000
        self_id: int = 12345678
        post_type: Literal['message'] = 'message'
        sub_type: str = 'normal'
        user_id: int = 10001
        message_type: Literal['group'] = 'group'
        group_id: int = 87654321
        message_id: int = next(_SEQ)
        message: Message = Message('test')
        raw_message: str = 'test'
        font: int = 0
        sender: Sender = Sender(card='', nickname='小明', role='member')
        to_me: bool = False
        reply: Reply | None = None

    return FakeEvent(**field)


def fake_private_message_event_v11(**field) -> 'PrivateMessageEventV11':
    from pydantic import create_model
    from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
    from nonebot.adapters.onebot.v11.event import Sender

    _Fake = create_model('_Fake', __base__=PrivateMessageEvent)

    class FakeEvent(_Fake):
        time: int = 1000000
        self_id: int = 12345678
        post_type: Literal['message'] = 'message'
        sub_type: str = 'friend'
        user_id: int = 10001
        message_type: Literal['private'] = 'private'
        message_id: int = next(_SEQ)
        message: Message = Message('test')
        raw_message: str = 'test'
        font: int = 0
        sender: Sender = Sender(nickname='小明')
        to_me: bool = False

    return FakeEvent(**field)
