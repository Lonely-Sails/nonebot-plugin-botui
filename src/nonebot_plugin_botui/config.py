"""nonebot-plugin-botui 的配置与全局常量。"""

from __future__ import annotations

from nonebot import get_driver, get_plugin_config
from pydantic import BaseModel, field_validator

# 机器人发送消息时可能调用的 API 名称（用于区分“发出”的消息）
SEND_APIS: frozenset[str] = frozenset(
    {
        # OneBot V11 / V12
        'send_msg',
        'send_private_msg',
        'send_group_msg',
        'send_group_forward_msg',
        'send_private_forward_msg',
        # 通用 / 其他适配器
        'send_message',
        'send_group_message',
        'send_private_message',
        'send_channel_message',
        'post_message',
        'create_message',
        'send',
        'send_group_file',
        'upload_group_file',
        'upload_private_file',
        'send_group_notice',
    }
)


class Config(BaseModel):
    """插件配置项（环境变量前缀：BOTUI_）"""

    model_config = {'extra': 'ignore'}

    # ── 基础开关 ────────────────────────────────────────────────────────
    botui_enabled: bool = True
    """是否启用 WebUI"""
    botui_route: str = '/botui'
    """WebUI 挂载路径（需以 / 开头）"""
    botui_host: str = '127.0.0.1'
    """提示链接里使用的主机名（只影响链接显示，不影响服务实际监听地址）"""
    botui_allow_remote: bool = False
    """是否允许非本机来源访问。开启前请确保已启用令牌鉴权或放在反向代理之后"""

    # ── 鉴权 ────────────────────────────────────────────────────────────
    botui_auth: bool = True
    """是否启用 Token 鉴权（强烈建议保持开启）"""
    botui_token: str = ''
    """访问令牌；留空则自动生成并保存到数据目录的 token.txt"""

    # ── 记录 ────────────────────────────────────────────────────────────
    botui_capture_received: bool = True
    """是否记录机器人收到的消息"""
    botui_capture_sent: bool = True
    """是否记录机器人发出的消息"""
    botui_capture_self: bool = False
    """是否记录「自己发出的消息被适配器回传成事件」；关闭可避免同一条消息记两次"""
    botui_exclude_adapters: tuple[str, ...] = ()
    """不记录这些适配器的消息，例如 ('OneBot V12',)"""
    botui_max_records: int = 200_000
    """消息记录上限，超出后自动删除最旧的记录；0 表示不限制"""
    botui_retention_days: int = 0
    """消息保留天数，超期自动清理；0 表示不限制"""
    botui_resolve_at_name: bool = True
    """是否尝试把 @某人 解析成群名片（会调用适配器接口并缓存）"""
    botui_api_timeout: float = 10.0
    """插件内部调用适配器接口的超时时间（秒）"""

    # ── WebUI 行为 ──────────────────────────────────────────────────────
    botui_page_size: int = 50
    """聊天记录每页条数"""
    botui_write_enabled: bool = True
    """是否允许在 WebUI 中代替机器人发送消息"""
    botui_allow_recall: bool = True
    """是否允许在 WebUI 中撤回消息"""
    botui_send_interval: float = 1.0
    """同一会话两次发送之间的最小间隔（秒），用于限流"""
    botui_media_allow_private: bool = False
    """是否允许 /media 代理内网/环回地址。

    默认 false，只允许公网图片链接——否则 /media 会变成 SSRF 跳板，能被用来
    探测内网服务或读取云元数据接口（169.254.169.254）。仅当你确实要显示来自
    内网的图片时才打开。"""
    botui_file_preview: bool = True
    """是否允许在网页上在线预览收到的文件（文本 / 图片 / PDF / 音视频）。"""
    botui_file_max_bytes: int = 100 * 1024 * 1024
    """文件下载代理允许的最大体积（字节），101 MB。

    ``0`` 表示不限制。文本在线预览另有 512 KB 的读取上限，不受这里影响。"""

    # ── 本地缓存（媒体 / 文件）───────────────────────────────────────────
    botui_cache_enabled: bool = True
    """是否把机器人收到的图片 / 文件缓存在本地。

    机器人的媒体直链大多带防盗链、而且**会过期**：过一阵子再点开聊天记录，
    链接就已经打不开了。开启后，收到消息时就把可缓存的媒体下载到
    localstore 的缓存目录，之后 WebUI 一律读本地副本。缓存位置由
    ``LOCALSTORE_CACHE_DIR`` / ``LOCALSTORE_PLUGIN_CACHE_DIR`` 决定
    （与 ``LOCALSTORE_USE_CWD=true`` 一起用时就是项目下的 ``cache/``）。"""
    botui_cache_max_bytes: int = 512 * 1024 * 1024
    """本地缓存的总体积上限（字节），默认 512 MB。

    超出后先淘汰**没有被任何聊天记录引用**的资源（按最久未访问），仍超才动
    被引用的那批。``0`` 表示不限制 —— 与 ``BOTUI_CACHE_MAX_FILES`` 同时为
    ``0`` 时等于关闭缓存。"""
    botui_cache_max_files: int = 20000
    """本地缓存的资源条数上限，默认 2 万条；``0`` 表示不限制。"""
    botui_cache_file_max_bytes: int = 20 * 1024 * 1024
    """单个资源超过这个体积就不缓存（字节），默认 20 MB；``0`` 表示只受总量限制。

    大视频这类动辄几百 MB 的文件没必要为了「防链接失效」占满磁盘。"""
    botui_cache_ttl: float = 3 * 24 * 3600.0
    """没有任何聊天记录引用的缓存资源保留多久（秒），默认 3 天；``0`` 表示不回收。"""
    botui_cache_retention: float = 7 * 24 * 3600.0
    """被聊天记录引用的缓存资源最多保留多久（秒），默认 7 天。

    ``0`` 表示永不按时间回收（只受体积 / 条数上限约束）—— 想「一直留着」就
    设成 0，同时把两个上限留空或调大。"""
    botui_cache_fetch_timeout: float = 20.0
    """下载单个媒体/文件到缓存时的超时时间（秒）。"""
    botui_cache_max_bytes_per_message: int = 5 * 1024 * 1024 * 4
    """单条消息里所有可缓存内容加起来的上限（字节），默认 20 MB。

    防止「一条消息里塞几十个文件」把一次采集拖成几十秒。"""

    botui_upload_enabled: bool = True
    """是否允许在 WebUI 里上传附件（图片 / 文件）并以机器人身份发送。"""
    botui_upload_max_bytes: int = 20 * 1024 * 1024
    """单个上传附件的最大体积（字节），默认 20 MB；``0`` 表示不限制。"""
    botui_upload_ttl: float = 3600.0
    """上传附件在服务端的保留时长（秒），默认 1 小时；``0`` 表示不自动清理。

    附件先用不到时也是占着磁盘的：选完附件却一直不发送、或发了之后又不用了
    都不该让它永远留着，所以给个默认 1 小时的过期时间，由定时任务回收。"""

    # ── 其他 ────────────────────────────────────────────────────────────
    botui_debug: bool = False
    """打印更详细的调试日志"""

    @field_validator('botui_route')
    @classmethod
    def _check_route(cls, v: str) -> str:
        route = (v or '').strip()
        if not route.startswith('/'):
            route = '/' + route
        return '/' if route == '/' else route.rstrip('/')

    @field_validator('botui_page_size')
    @classmethod
    def _check_page_size(cls, v: int) -> int:
        return min(200, max(10, int(v)))


# 配置加载
plugin_config: Config = get_plugin_config(Config)
global_config = get_driver().config

# 机器人昵称（用于给自己发出的消息署名）
NICKNAME: str = next(iter(global_config.nickname), '') if global_config.nickname else ''
