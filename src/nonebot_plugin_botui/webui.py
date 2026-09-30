"""WebUI 服务：事件总线 + 路由挂载。

WebUI 复用 NoneBot 驱动（FastAPI）自带的 ASGI 应用，
因此**不需要**额外启动端口；插件只往上面挂一个路由。

由于是往同一个事件循环里的应用中挂路由，机器人当前连着的适配器
（包括反向 WebSocket 这类只有机器人主动外连的情况）都能直接被复用。

实时刷新走 WebSocket（``/api/ws``）：每个连接对应一个订阅队列，新事件由
``publish_*`` 扇出过去；断线重连时客户端带上 ``?since=N``，服务端再从环形
缓冲里补发漏掉的那一段，不用做任何轮询。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any
from collections import deque

import nonebot
from nonebot import logger

from .config import plugin_config as cfg
from .models import BotRecord, ChatRecord, MessageRecord

BUS_LIMIT = 2000  # 环形缓冲条数（断线重连时补发用）
QUEUE_LIMIT = 500  # 单个连接的待发队列上限


class EventBus:
    """事件总线：环形缓冲（补发）+ 订阅队列（实时推送）。

    ``publish_*`` 只做两件事：把事件塞进环形缓冲，再扇出给所有订阅队列。
    队列满了就丢掉这条事件 —— 服务端不替慢客户端无限积压；前端会通过事件
    序号出现空洞发现漏事件，自行重新同步（见 ``app.js`` 的重连逻辑）。
    """

    def __init__(self, limit: int = BUS_LIMIT, queue_limit: int = QUEUE_LIMIT):
        self._events: deque[dict[str, Any]] = deque(maxlen=limit)
        self._queue_limit = queue_limit
        self._seq = 0
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def latest(self) -> int:
        return self._seq

    # ── 订阅 ────────────────────────────────────────────────────────────
    def subscribe(self) -> tuple[asyncio.Queue[dict[str, Any]], int]:
        """订阅事件，返回 ``(队列, 订阅时刻的事件序号)``。

        返回的序号是「补发 / 实时」的分界点：调用方先用 :meth:`backlog` 补发
        ``since < seq <= 分界点`` 的事件，再从队列里取之后的事件，两边不会
        重叠也不会漏。这段逻辑没有 ``await``，所以分界点是精确的。
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_limit)
        boundary = self._seq
        self._subs.add(queue)
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 只在非事件循环里订阅时发生
            pass
        return queue, boundary

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(queue)

    # ── 发布 ────────────────────────────────────────────────────────────
    def _publish(self, event: dict[str, Any]) -> None:
        self._seq += 1
        event['seq'] = self._seq
        self._events.append(event)
        if not self._subs:
            return
        # 写入钩子跑在事件循环里，但测试/外部代码可能从别的线程入队；
        # 队列不是线程安全的，跨线程时要绕回事件循环线程再扇出。
        loop = self._loop
        if loop is None or loop.is_closed():  # pragma: no cover - 正常总有循环
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fanout(event)
        else:  # pragma: no cover - 只有跨线程发布才会走到
            loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict[str, Any]) -> None:
        for queue in list(self._subs):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - 需要极慢的客户端
                logger.debug('BotUI 事件队列已满，丢弃一条事件（前端会自行重同步）')

    def publish_message(self, record: MessageRecord, chat: ChatRecord | None) -> None:
        self._publish(
            {
                'type': 'message',
                'id': record.row_id,
                'message': record.to_dict(),
                'chat': chat.to_dict() if chat is not None else None,
            }
        )

    def publish_recall(self, row_id: int) -> None:
        self._publish(
            {
                'type': 'recall',
                'id': int(row_id),
                'message': None,
                'chat': None,
            }
        )

    def publish_chat(self, chat: ChatRecord) -> None:
        self._publish(
            {
                'type': 'chat',
                'id': 0,
                'message': None,
                'chat': chat.to_dict(),
            }
        )

    def publish_bot(self, bot: BotRecord) -> None:
        """机器人上线/离线事件（WebUI 的切换器靠它刷新在线标记）"""
        self._publish(
            {
                'type': 'bot',
                'id': 0,
                'message': None,
                'chat': None,
                'bot': bot.to_dict(),
            }
        )

    def backlog(self, since: int, until: int, limit: int = 500) -> list[dict[str, Any]]:
        """取 ``since < seq <= until`` 的事件（最多 ``limit`` 条）。

        ``since <= 0`` 视为「客户端刚打开页面」：这类客户端不存在漏事件的概念
        （历史消息它自己会去拉），补发只会把记录重复推一遍。
        """
        if since <= 0 or until <= since:
            return []
        return [e for e in self._events if since < e['seq'] <= until][:limit]


class WebUIServer:
    """WebUI 的门面：对外提供事件流与路由挂载。

    事件总线（:class:`EventBus`）刻意不直接暴露出去：外部只通过这里的方法
    读写事件，序号游标与缓冲上限就始终由一个地方说了算。
    """

    def __init__(self) -> None:
        self._bus = EventBus()
        self.route = cfg.botui_route
        self.mounted = False
        self.url = ''
        self._lock = threading.Lock()

    # ── 事件 ────────────────────────────────────────────────────────────
    @property
    def latest(self) -> int:
        """当前事件序号"""
        return self._bus.latest

    def subscribe(self) -> tuple[asyncio.Queue[dict[str, Any]], int]:
        return self._bus.subscribe()

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._bus.unsubscribe(queue)

    def backlog(self, since: int, until: int, limit: int = 500) -> list[dict[str, Any]]:
        return self._bus.backlog(since, until, limit)

    def publish_message(self, record: MessageRecord, chat: ChatRecord | None) -> None:
        self._bus.publish_message(record, chat)

    def publish_recall(self, row_id: int) -> None:
        self._bus.publish_recall(row_id)

    def publish_bot(self, bot: BotRecord) -> None:
        self._bus.publish_bot(bot)

    def publish_chat(self, chat: ChatRecord) -> None:
        """会话资料变化（如用户设置了备注）：推送更新给已打开的页面"""
        self._bus.publish_chat(chat)

    # ── 挂载 ────────────────────────────────────────────────────────────
    def mount(self) -> bool:
        with self._lock:
            if self.mounted:
                return True
            app = _fastapi_app()
            if app is None:
                return False

            from . import api

            app.include_router(api.router, prefix=self.route)

            _install_error_handlers(app, self.route)

            # Starlette 会把中间件栈（异常处理器就包在里面）缓存到
            # ``middleware_stack``，一旦有请求先到达就会固化。挂载通常发生在启动
            # 阶段、尚无请求，但为了稳妥，这里重建一次栈让新处理器生效。
            #
            # 不直接把 middleware_stack 置成 None：那样会连带让 Starlette 的
            # 「启动后不许再加中间件」检查失效（它正是判断 middleware_stack
            # 是否为 None），等于替其他插件把这个保护关掉了。
            if app.middleware_stack is not None:
                app.middleware_stack = app.build_middleware_stack()

            self.mounted = True
            port = nonebot.get_driver().config.port
            self.url = f'http://{cfg.botui_host}:{port}{self.route}/'
            logger.debug(f'BotUI 路由已挂载到 {self.url}')
            return True

    def help_url(self, token: str = '') -> str:
        """带令牌的访问链接（仅在需要时附带）"""
        if not self.url:
            return ''
        if token and cfg.botui_auth:
            return f'{self.url}?token={token}'
        return self.url


def _fastapi_app() -> Any:
    """取宿主的 FastAPI 应用；拿不到就记一条日志并返回 None。

    必须先自己判断驱动类型：``nonebot.get_app()`` 内部直接 ``assert
    isinstance(driver, ASGIMixin)``，非服务端型驱动下抛的是断言错误，
    对用户来说完全看不出该怎么办。
    """
    from nonebot.drivers import ASGIMixin

    try:
        from fastapi import FastAPI
    except ImportError:
        logger.error(
            'BotUI 的 WebUI 需要 FastAPI，'
            '请安装 nonebot2[fastapi]（或 fastapi）后重试。'
        )
        return None

    if not isinstance(nonebot.get_driver(), ASGIMixin):
        logger.error(
            'BotUI 的 WebUI 需要服务端型驱动器（如 FastAPI），'
            '当前驱动器不支持，WebUI 不会启动。'
        )
        return None

    app = nonebot.get_app()
    if not isinstance(app, FastAPI):
        logger.error('BotUI 未能拿到 FastAPI 应用，WebUI 不会启动。')
        return None
    return app


def _json_error(status_code: int, error: str, detail: Any = None, headers: Any = None):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code,
        content={'ok': False, 'error': error, 'detail': detail},
        headers=headers,
    )


def _install_error_handlers(app: Any, route_prefix: str) -> None:
    """让 BotUI 路由下的错误统一成 ``{"ok": false, "error": ...}``。

    两点必须注意：

    1. 要注册 starlette 的 ``HTTPException``：``fastapi.HTTPException`` 是它的
       子类，而异常处理器按 MRO 查找，Starlette 自带的那份会先命中，注册在
       FastAPI 的类上永远不会生效。
    2. Starlette 的 ``add_exception_handler`` 只是往 dict 里赋值、**没有链式
       调用**，直接注册会把宿主（或其他插件）的 404/422 定制静默顶掉。所以先把
       原有的处理器接过来，非 BotUI 路径原样转交给它。
    """
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from fastapi.exception_handlers import (
        http_exception_handler,
        request_validation_exception_handler,
    )

    previous_http = app.exception_handlers.get(StarletteHTTPException)
    previous_validation = app.exception_handlers.get(RequestValidationError)

    @app.exception_handler(StarletteHTTPException)
    async def _botui_http_error(request: Any, exc: StarletteHTTPException):
        if request.url.path.startswith(route_prefix):
            return _json_error(
                exc.status_code,
                str(exc.detail),
                exc.detail,
                getattr(exc, 'headers', None),
            )
        return await _delegate(previous_http or http_exception_handler, request, exc)

    @app.exception_handler(RequestValidationError)
    async def _botui_validation_error(request: Any, exc: RequestValidationError):
        if request.url.path.startswith(route_prefix):
            return _json_error(422, '请求参数不合法', exc.errors())
        return await _delegate(
            previous_validation or request_validation_exception_handler, request, exc
        )


async def _delegate(handler: Any, request: Any, exc: Exception) -> Any:
    """调用宿主原来的异常处理器。

    Starlette 自己的 ``ExceptionHandler`` 允许返回 ``Response`` **或**
    ``Awaitable[Response]``（同步处理器会被它丢进线程池），而我们这里是 ``async``
    包装函数，必须自己兼容这两种返回。
    """
    result = handler(request, exc)
    if inspect.isawaitable(result):
        return await result
    return result
