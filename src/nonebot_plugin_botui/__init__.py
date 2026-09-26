"""nonebot-plugin-botui：给机器人配一个 QQ 风格的 WebUI 控制台。

核心能力：
- 记录机器人收到的所有消息（私聊/群聊），以及机器人自己发出的消息；
- 提供一个类似 QQ 的网页界面：左侧会话列表、右侧聊天记录，支持搜索、翻页；
- 可以直接在网页上以机器人的身份发消息（支持 @ 和回复），也可以撤回；
- 全部基于 nonebot-plugin-alconna 的跨平台消息发送能力与
  nonebot-plugin-uninfo 的跨平台用户/场景信息，不绑定具体适配器；
- WebUI 复用 NoneBot 自带的 FastAPI 应用，不额外占用端口。

使用方式：启动时会把带令牌的访问地址打印到控制台日志，直接点开即可。
插件**不注册任何聊天指令**，不会占用任何命令名，也不会拦截用户消息。
"""

from __future__ import annotations

import secrets

from nonebot import logger, require, get_driver
from nonebot.plugin import PluginMetadata, inherit_supported_adapters

require('nonebot_plugin_uninfo')
require('nonebot_plugin_alconna')
require('nonebot_plugin_localstore')
require('nonebot_plugin_apscheduler')

from nonebot_plugin_apscheduler import scheduler

from .paths import DB_FILE, TOKEN_FILE
from .store import MessageStore
from .webui import WebUIServer
from .config import Config
from .config import plugin_config as cfg
from .models import MessageRecord
from .capture import setup as setup_capture
from .capture import store_ref, register_hooks

__plugin_meta__ = PluginMetadata(
    name='BotUI 控制台',
    description=(
        '一个 QQ 风格的 WebUI：查看机器人收发的全部消息，'
        '并以机器人的身份发消息、撤回消息'
    ),
    usage=(
        '插件不注册聊天指令：启动时会把 WebUI 地址和访问令牌打印到控制台日志，'
        '浏览器打开该地址即可使用。'
    ),
    type='application',
    homepage='https://github.com/Lonely-Sails/nonebot-plugin-botui',
    config=Config,
    supported_adapters=inherit_supported_adapters(
        'nonebot_plugin_alconna', 'nonebot_plugin_uninfo'
    ),
)

driver = get_driver()

# ── 运行时状态（首次用到时创建） ────────────────────────────────────────
_store: MessageStore | None = None
_server: WebUIServer | None = None
_token: str = ''
_hooks_ready = False


def _load_token() -> str:
    """读取或生成访问令牌。

    路径由 nonebot-plugin-localstore 给出（见 ``.paths``），这里只负责读写。
    """
    if cfg.botui_token:
        return cfg.botui_token
    path = TOKEN_FILE
    try:
        if path.is_file():
            token = path.read_text(encoding='utf-8').strip()
            if token:
                return token
    except Exception as e:  # pragma: no cover - 文件系统异常
        logger.warning(f'BotUI 读取令牌失败，将重新生成：{e}')
    token = secrets.token_urlsafe(24)
    try:
        path.write_text(token, encoding='utf-8')
        path.chmod(0o600)
    except Exception as e:  # pragma: no cover - 只读文件系统
        logger.warning(f'BotUI 保存令牌失败（重启后会变化）：{e}')
    return token


def _get_store() -> MessageStore:
    global _store, _hooks_ready
    if _store is None:
        _store = MessageStore(cfg, DB_FILE)
        _store.on_insert = _on_record_inserted
        if not _hooks_ready:
            # 采集钩子只注册一次（Bot.on_calling_api 会累加回调，重复注册会记多条）
            setup_capture(_store, cfg)
            register_hooks()
            _hooks_ready = True
    return _store


def _get_server() -> WebUIServer:
    global _server
    if _server is None:
        _server = WebUIServer()
    return _server


def _on_record_inserted(record: MessageRecord) -> None:
    """记录落库后推送增量事件（WebUI 靠它实时刷新）"""
    if _store is not None and _server is not None:
        _server.publish_message(record, _store.chat(record.chat_key))


def _try_mount() -> bool:
    """挂载 WebUI；``BOTUI_ENABLED=false`` 时永远不挂。"""
    if not cfg.botui_enabled:
        logger.info('BotUI 已关闭（BOTUI_ENABLED=false），不会挂载 WebUI')
        return False
    if _get_server().mounted:
        return True
    _get_store()  # 确保采集已就绪
    return _get_server().mount()


# ── 生命周期 ────────────────────────────────────────────────────────────
async def _cleanup_job() -> None:
    store = store_ref()
    if store is None:
        return
    removed = await store.cleanup()
    if removed:
        logger.info(f'BotUI 已清理 {removed} 条超出上限的历史记录')


def _log_banner(mounted: bool, store: MessageStore) -> None:
    """把访问方式打到控制台。

    插件不注册任何聊天指令，控制台日志就是唯一的「入口」，所以这里必须给出
    带令牌的完整地址。

    一条信息一行、各自调用一次 logger：loguru 会给每条记录都加上
    ``时间 [级别] 插件名 |`` 前缀，多行挤在一条记录里的话，第二行开始就没有
    前缀了，看上去像悬在空中。

    除此之外只打两类信息：数据存到哪了，以及**偏离默认值**的开关。采集开关、
    记录上限之类保持默认时不打 —— 这些在配置文件里本来就能看到，每次都念一遍
    只会把真正要看的那行地址淹掉。
    """
    if mounted:
        logger.info(_get_server().help_url(_token))
        if not cfg.botui_allow_remote:
            logger.info('仅本机可访问；需要远程访问请设置 BOTUI_ALLOW_REMOTE=true')
    else:
        logger.warning('未能挂载 WebUI，请使用 FastAPI 驱动（nonebot2[fastapi]）')
    logger.info(f'数据：{store.path}')

    if not cfg.botui_auth:
        logger.warning('令牌鉴权已关闭：能访问该地址的人都能冒充机器人发言')
    if not (cfg.botui_capture_received and cfg.botui_capture_sent):
        received = '开' if cfg.botui_capture_received else '关'
        sent = '开' if cfg.botui_capture_sent else '关'
        logger.info(f'采集已按配置调整（收到={received}，发出={sent}）')


@driver.on_startup
async def _on_startup() -> None:
    global _token
    if not cfg.botui_enabled:
        logger.info('BotUI 已在配置中关闭（BOTUI_ENABLED=false）')
        return
    store = _get_store()
    await store.start()
    _token = _load_token()
    from . import api

    server = _get_server()
    api.setup(store, server, _token)
    mounted = _try_mount()
    if cfg.botui_allow_remote and not cfg.botui_auth:
        logger.warning(
            'BotUI 已允许非本机访问（BOTUI_ALLOW_REMOTE=true）且未开启令牌鉴权'
            '（BOTUI_AUTH=false）：任何能访问该地址的人都能查看全部聊天记录并'
            '冒充机器人发言，请务必确认！'
        )
    _log_banner(mounted, store)
    scheduler.add_job(
        _cleanup_job,
        'interval',
        hours=6,
        id='botui_cleanup',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


@driver.on_shutdown
async def _on_shutdown() -> None:
    store = store_ref()
    if store is None:
        return
    try:
        await store.flush()
        await store.stop()
        logger.debug('BotUI 存储已关闭')
    except Exception as e:  # pragma: no cover - 关闭阶段尽力而为
        logger.warning(f'BotUI 关闭存储时出错：{e}')
