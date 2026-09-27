<div align="center">
    <a href="https://github.com/Lonely-Sails/nonebot-plugin-botui">
    <img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-template/refs/heads/resource/.docs/NoneBotPlugin.svg" width="310" alt="logo"></a>

# NoneBot-Plugin-BotUI

<a href="https://github.com/Lonely-Sails/nonebot-plugin-botui"><img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="python"></a>
<a href="https://github.com/Lonely-Sails/nonebot-plugin-botui"><img src="https://img.shields.io/badge/NoneBot2-2.3.0%2B-ff69b4" alt="nonebot2"></a>
<a href="https://github.com/Lonely-Sails/nonebot-plugin-botui/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="license"></a>
</div>

给机器人配一个 **QQ 风格的 WebUI 控制台**：把机器人收发的全部消息记到本地 SQLite，在网页上像翻聊天记录一样查看，并以机器人的身份发言、回复、@ 和撤回。

## ✨ 功能特性

- **收发都记**：收到的消息由 `event_preprocessor` 钩子采集，发出的消息由 `Bot.on_calling_api` 钩子拦截 `send_msg` 一类的接口记录；图文混合消息按消息段完整保留
- **引用可读**：回复/引用消息会连同被引用者与原文一起保留，气泡上方显示「谁：引用内容」的引用块，点击可跳转到被引用的那条消息
- **文件接收与在线预览**：收到的文件显示为文件卡片（按扩展名着色图标 + 文件名 + 大小），点击即可在弹窗中在线预览 —— 文本类（代码、配置、日志、`txt`/`md`/`json` 等）直接渲染正文，图片、PDF 内联显示，音视频内联播放，其它类型可一键下载；文本预览默认只读取前 512 KB 并提示已截断，下载走**真流式代理**（支持 HTTP Range 断点续传），默认最大 100 MB（`BOTUI_FILE_MAX_BYTES`）。**文件名会自动还原**：像 QQ 官方机器人适配器只给一个下载直链、文件名恒为 `file.bin` 的情况，插件会从链接的 `fname`/`filename` 等参数或路径末段、以及上游响应头里取回真实文件名（含中文），并按真实后缀判断预览方式与下载名
- **合并转发展开**：合并转发消息可点开查看每个节点的发送者、时间与内容，优先使用记录里已有的内联节点，缺失时再调用适配器接口拉取
- **聊天记录导出**：右键菜单可把当前会话（含时间、发送者与全部消息段）导出为 JSON 文件下载
- **跨平台**：不绑定具体适配器 —— 会话与用户信息来自 `nonebot-plugin-uninfo` 的 `Session`，消息解析与发送走 `nonebot-plugin-alconna` 的 `UniMessage` / `Target`
- **本地存储**：消息与会话落盘到插件数据目录的 `botui.sqlite3`（WAL 模式），支持记录条数上限与保留天数，插件每 6 小时自动清理一次
- **QQ 风格 WebUI**：左侧会话列表（搜索、头像、未读小红点、最后一条消息摘要）+ 右侧聊天记录（气泡、日期分隔、图片点击放大）
- **实时推送**：WebUI 通过 `/api/ws` 建立 WebSocket 长连接（`ws://` / `wss://`），服务端主动推送新消息、撤回与新会话，页面无需轮询；断线按 1s ~ 30s 指数退避自动重连，重连时带上事件序号补发漏掉的那段
- **以机器人身份发言**：网页上直接发送文本、`@` 成员、回复某条消息，并可按配置撤回自己发出的消息；`@` 成员从「聊天里出现过的成员名册」中挑选，无需手动输入 QQ 号
- **发送图片与文件**：点输入框旁的 `＋` 选图 / 选文件，也可以直接把文件**拖进页面**或从剪贴板**粘贴**（截图直接发）。图片按 `Image` 段发送、聊天里直接显示，其余按 `File` 段发送；选中的附件先以缩略图 / 文件名标签排在输入框上方，可逐个移除后再发送。上传的附件先落到插件数据目录（`uploads/`），默认单个上限 20 MB（`BOTUI_UPLOAD_MAX_BYTES`）、选完不用 1 小时后自动回收（`BOTUI_UPLOAD_TTL`），已发出的附件保留 7 天以便聊天记录里的图片仍可显示
- **多机器人切换**：右上角的下拉框会记录**所有连接过**的机器人，在线的标绿点、离线的置灰；每个机器人的会话、消息与搜索结果彼此隔离，切换后互不串台。给某个会话发消息时只会用**它自己的机器人**发送，该机器人不在线时宁可失败也不会冒用别的机器人
- **令牌鉴权 + 仅本机访问**：默认开启，访问令牌自动生成并保存在数据目录，默认只允许本机来源的请求
- **零额外端口**：路由直接挂到宿主项目已有的 FastAPI 应用上（`nonebot2[fastapi]`），不新起服务
- **不注册任何指令**：不占用命令名、不拦截聊天消息，访问地址只在启动时打印到控制台日志

## 📦 安装

```bash
nb plugin install nonebot-plugin-botui
```

或用 pip 安装：

```bash
pip install nonebot-plugin-botui
# 尚未发布到 PyPI 时，也可以直接从仓库安装：
pip install git+https://github.com/Lonely-Sails/nonebot-plugin-botui.git
```

安装后在 `pyproject.toml` 的 `[tool.nonebot]` 中加入：

```toml
plugins = ["nonebot_plugin_botui"]
```

> WebUI 依赖宿主项目的 **FastAPI 驱动**：请安装 `nonebot2[fastapi]`（并在 `.env` 里配置 `DRIVER=~fastapi`），否则插件启动时会提示「未能拿到 FastAPI 应用」。
>
> 依赖：`nonebot2 >= 2.3.0`、`nonebot-plugin-alconna`、`nonebot-plugin-uninfo`、`nonebot-plugin-localstore`、`nonebot-plugin-apscheduler`、`fastapi`、`aiosqlite`、`httpx`。

## 🚀 快速开始

插件**不注册任何聊天指令**，不会占用命令名，也不会拦截用户消息。启动机器人后直接看控制台日志：

1. 启动日志里会给出带令牌的访问地址，以及数据存放位置：

   ```text
   [INFO] nonebot_plugin_botui | http://127.0.0.1:8080/botui/?token=xxxxxxxx
   [INFO] nonebot_plugin_botui | 仅本机可访问；需要远程访问请设置 BOTUI_ALLOW_REMOTE=true
   [INFO] nonebot_plugin_botui | 数据：/path/to/data/botui.sqlite3
   ```

   每行都是一条独立日志（前缀由 NoneBot 的日志格式补上）。只有**偏离默认值**的开关才会额外列出（例如关掉了采集、关掉了鉴权）；保持默认时不会啰嗦。

2. 打开那个链接即可。前端会自己把地址栏里的 `?token=` 抹掉，并保存到浏览器 `localStorage`，下次直接访问 `/botui/` 就行；换浏览器时在令牌输入框里粘贴令牌即可。

几个说明：

- 日志若显示**未能挂载 WebUI**，通常是驱动不对：WebUI 依赖宿主项目的 **FastAPI 驱动**，请安装 `nonebot2[fastapi]` 并在 `.env` 里配置 `DRIVER=~fastapi`。
- 令牌默认自动生成并写入插件数据目录下的 **`token.txt`**（目录由 `nonebot-plugin-localstore` 提供；文件权限为 `0600`）。想固定令牌就设置 `BOTUI_TOKEN`。
- 关闭令牌后地址里不会带 `?token=`，此时任何能访问该地址的人都能查看记录并冒充机器人发言，启动日志会明确警告。

## ⚙️ 配置

在 `.env` / `.env.prod` 中配置（环境变量前缀 `BOTUI_`，完整示例见 `.env.example`）：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | :-: | --- | --- |
| `BOTUI_ENABLED` | `bool` | `true` | 是否启用 WebUI；关闭后插件不采集也不挂载 |
| `BOTUI_ROUTE` | `str` | `/botui` | WebUI 挂载路径，不以 `/` 开头会自动补上 |
| `BOTUI_HOST` | `str` | `127.0.0.1` | 提示链接里使用的主机名（**只影响链接显示**，不改变安全策略） |
| `BOTUI_ALLOW_REMOTE` | `bool` | `false` | 是否允许非本机来源访问；开启前请确认令牌鉴权已启用（见「安全提示」） |
| `BOTUI_AUTH` | `bool` | `true` | 是否启用令牌鉴权（强烈建议保持开启） |
| `BOTUI_TOKEN` | `str` | 空 | 访问令牌；留空则自动生成并保存到数据目录的 `token.txt` |
| `BOTUI_CAPTURE_RECEIVED` | `bool` | `true` | 是否记录机器人收到的消息 |
| `BOTUI_CAPTURE_SENT` | `bool` | `true` | 是否记录机器人发出的消息 |
| `BOTUI_CAPTURE_SELF` | `bool` | `false` | 是否记录「自己发出的消息被适配器回传成事件」的情况；关闭可避免同一条消息被记两次 |
| `BOTUI_EXCLUDE_ADAPTERS` | `tuple[str, ...]` | `()` | 不记录这些适配器的消息，例如 `["OneBot V12"]` |
| `BOTUI_MAX_RECORDS` | `int` | `200000` | 消息记录上限，超出后自动删除最旧的记录；`0` 表示不限制 |
| `BOTUI_RETENTION_DAYS` | `int` | `0` | 消息保留天数，超期自动清理；`0` 表示不限制 |
| `BOTUI_RESOLVE_AT_NAME` | `bool` | `true` | 是否把 `@某人` 解析成群名片（会调用适配器接口并缓存） |
| `BOTUI_API_TIMEOUT` | `float` | `10.0` | 插件内部调用适配器接口（如获取群成员信息）的超时时间（秒） |
| `BOTUI_PAGE_SIZE` | `int` | `50` | 聊天记录每页条数，取值范围 `10 ~ 200` |
| `BOTUI_WRITE_ENABLED` | `bool` | `true` | 是否允许在 WebUI 中代替机器人发送消息；`false` 时网页进入只读模式 |
| `BOTUI_ALLOW_RECALL` | `bool` | `true` | 是否允许在 WebUI 中撤回消息 |
| `BOTUI_MEDIA_ALLOW_PRIVATE` | `bool` | `false` | 图片代理是否允许访问内网/环回地址；除非确实要用它代理内网图床，否则保持关闭（见「安全提示」） |
| `BOTUI_FILE_PREVIEW` | `bool` | `true` | 是否允许在网页上在线预览收到的文件（文本 / 图片 / PDF / 音视频） |
| `BOTUI_FILE_MAX_BYTES` | `int` | `104857600` | 文件下载代理允许的最大体积（字节，默认 100 MB）；`0` 表示不限制 |
| `BOTUI_UPLOAD_ENABLED` | `bool` | `true` | 是否允许在 WebUI 里上传附件（图片 / 文件）并以机器人身份发送 |
| `BOTUI_UPLOAD_MAX_BYTES` | `int` | `20971520` | 单个上传附件的最大体积（字节，默认 20 MB）；`0` 表示不限制 |
| `BOTUI_UPLOAD_TTL` | `float` | `3600.0` | 未发送的上传附件在服务端的保留时长（秒），默认 1 小时；`0` 表示不自动清理（已发出的附件固定保留 7 天） |
| `BOTUI_SEND_INTERVAL` | `float` | `1.0` | 同一会话两次发送之间的最小间隔（秒），用于限流 |
| `BOTUI_DEBUG` | `bool` | `false` | 打印更详细的调试日志 |

### 数据存放位置

数据库与令牌的路径**完全交给 `nonebot-plugin-localstore`**，插件只读取它给出的目录，不再提供自己的目录配置项：

```dotenv
# 全局改（所有插件生效）
LOCALSTORE_DATA_DIR=/srv/nonebot/data

# 只改本插件（推荐，用插件 ID 作 key）
LOCALSTORE_PLUGIN_DATA_DIR={"nonebot_plugin_botui": "/srv/botui/data"}
```

默认位置参考 localstore 的规则：`LOCALSTORE_USE_CWD=true` 时为 `<运行目录>/data/nonebot_plugin_botui/`，否则为系统数据目录下的 `nonebot2/nonebot_plugin_botui/`。可以用 `nb localstore` 查看实际路径。目录下会生成：

- `botui.sqlite3`：消息与会话数据库（WAL 模式）
- `token.txt`：自动生成的访问令牌（权限 `0600`）
- `uploads/`：WebUI 里上传、等待或已经发出的附件（每个附件一个子目录，过期由定时任务回收）

> 路径在插件导入时解析一次并存为常量。localstore 是靠**调用栈**反查「哪个插件在要目录」的，只有栈上存在插件自己的帧时才能查到（从 `__main__`、别名模块名或外部库回调里现取会报 `Cannot detect caller plugin`）。导入时解析最稳妥，之后全项目只读常量。

## 🎉 命令

**没有命令。** 插件不注册任何 matcher，不 `require` 任何前缀，也不拦截聊天消息 ——
访问地址只在启动时打印到控制台日志（见「快速开始」）。

这样设计是为了避免和你的其他插件抢命令名：早期的版本注册过 `帮助` / `状态` /
`链接` / `统计` / `清理` / `重置` 这些又短又通用的别名，很容易和别人撞车。

清理旧记录不需要手动触发：插件每 6 小时会按 `BOTUI_MAX_RECORDS` 与
`BOTUI_RETENTION_DAYS` 自动执行一次。

## 🖥️ 界面说明

- **主题**：默认深色，右上角按钮可切换浅色，选择保存在 `localStorage`（`botui_theme`）。
- **机器人切换**：右上角下拉框列出**所有连接过**的机器人（在线的标绿点、离线的置灰，显示头像、名称、`self_id` 与适配器）。始终恰好选中一个机器人：进来时优先选中上次选过的（记录在 `localStorage` 的 `botui_bot`），否则选中第一个在线的。切换机器人会清空当前会话与消息列表，只显示该机器人的会话与记录；连接状态由服务端推送实时更新，选择的机器人离线时会自动挑选下一个在线的。
- **会话记忆**：每个机器人最近打开的会话会记在 `localStorage`（`botui_chat`，形如 `{self_id: chat_key}`），下次打开页面或切回该机器人时自动打开那个会话；若已不存在（被清理或换了机器人）则照常停留在会话列表。
- **令牌页**：开启鉴权时，若浏览器里没有令牌（或令牌失效返回 401），会先弹出输入框；令牌只存在当前浏览器的 `localStorage` 中，不会回传服务器保存。
- **会话列表**：按最后消息时间倒序，显示头像（拉不到图就用名称首字生成彩色圆）、名称、最后一条消息摘要（机器人发出的以「我: 」开头）、时间（今天为 `HH:MM`，昨天为「昨天」，更早为 `MM-DD` / `YYYY-MM-DD`）与未读数（超过 99 显示 `99+`，未打开的会话显示「新」+ 摘要）。
- **搜索**：顶部搜索框输入后出现「会话 / 消息」两个标签。**会话**标签即时过滤会话名、会话 ID（群号/QQ 号）与最后一条消息；**消息**标签会请求 `/api/search` 做正文全文检索，结果按时间倒序展示并高亮关键词，点一条即可跳到对应会话并定位到该消息（不在已加载的一页时会提示）。
- **聊天记录**：左右分栏气泡，连续消息紧凑排版，按天插入「今天 / 昨天 / 5月1日 / 2024年5月1日」分隔；切换会话时先显示骨架占位；图片消息点击放大（灯箱，点击任意处或按 `Esc` 关闭），`@`、回复、表情、文件、语音、视频、卡片、合并转发等消息段有对应样式；向上滚动自动加载更早的消息，单次会话最多在前端保留 1200 条。
- **文件与预览**：文件消息显示为文件卡片，点击在弹窗内预览 —— 文本类（代码/日志/配置等）直接显示正文并在超过 512 KB 时提示截断，图片与 PDF 内联显示，音视频提供内联播放器，其它类型给出下载按钮（弹窗右上角也可下载/另存）。语音、音频、视频消息也都有内联播放器；图片与媒体统一走 `/api/media` 代理，文件走 `/api/file` 流式代理（带令牌与 SSRF 校验）。预览开关为 `BOTUI_FILE_PREVIEW`。
- **合并转发**：合并转发消息显示为「聊天记录」卡片，点击展开弹窗，逐个列出每个节点的发送者昵称、时间与消息内容。节点优先取自记录里的内联数据，没有时才向适配器请求。
- **实时刷新**：页面通过 WebSocket（`/api/ws`）接收服务端推送的事件，新消息实时置顶并提示「N 条新消息」，新会话自动进入列表，撤回事件会把气泡移除。连接失败/断开时按 1s ~ 30s 指数退避重连，并在左下角显示连接状态（令牌无效用 `4401`、来源被拒用 `4403`，分别对应弹令牌框 / 提示只允许本机）；重连会带上最后收到的事件序号，服务端从环形缓冲里补发漏掉的那段；序号出现较大空洞或长时间没收到任何推送时会自动重新同步一次。不在底部时新消息不打扰阅读，只提示「N 条新消息」。
- **发送 / 回复 / @**：底部输入框 `Enter` 发送、`Shift+Enter` 换行；`@` 按钮打开**成员选择菜单**（关键词过滤、显示头像与群主/管理员标签），只能从「这个群里记录到的成员」里选，不再要求手输 QQ 号，选中的成员以「@昵称」标签形式展示、可逐个移除；右键消息选「回复」会在输入框上方显示引用条（含被引用者与原文摘要），发送时带上回复段。
- **发送图片 / 文件**：输入框左侧的 `＋` 按钮打开文件选择框（可多选），也可以把文件**拖进页面任意位置**（会显示拖拽提示层），或在输入框里直接**粘贴**剪贴板里的图片（截图 → `Ctrl/Cmd+V` → 发送）。选中的附件会先上传到服务端并以缩略图（图片）或「类别 + 文件名 + 大小」标签排在输入框上方，可逐个 `✕` 移除。发送时可以只发附件不发文字。上传进度反映在发送按钮上（显示「上传中」），上传中的附件不能发送。
- **右键菜单**：`复制文本`、`回复`、`撤回`（仅机器人自己发出且拿到消息 ID 的消息可用，且需要 `BOTUI_ALLOW_RECALL=true`；不支持撤回的适配器会在调用时返回失败原因）、`预览文件`（文件消息可用）、`导出聊天记录`（导出当前会话为 JSON）、`复制消息ID`。
- **只读模式**：`BOTUI_WRITE_ENABLED=false` 时输入框、发送按钮、`@` 按钮与 `＋` 附件按钮均被禁用，底部提示「只读模式」。
- **窄屏**：宽度小于 768px 时变成单栏，点消息头部返回按钮回到会话列表；输入框字号会自动提到 16px，避免 iOS 聚焦时把整页放大；同时按 `viewport-fit=cover` 处理了刘海屏安全区。

## 🔐 安全提示

- **默认开启令牌鉴权**（`BOTUI_AUTH=true`）。令牌要么由 `BOTUI_TOKEN` 指定，要么自动生成 24 字节随机串写入数据目录的 `token.txt`（权限 `0600`）。请勿把令牌或 `token.txt` 泄露出去。
- **默认只允许本机访问**。服务端会拒绝非本机来源的请求（返回 403）。判断来源用的是 `ipaddress` 的 loopback 判定，所以 `127.0.0.1`、`127.0.0.2`、`::1`、`::ffff:127.0.0.1`、`localhost` 都算本机。要开放给外部，需要显式设置 **`BOTUI_ALLOW_REMOTE=true`**。
- **`BOTUI_HOST` 只管链接显示**。它决定启动日志里给出的主机名（比如你走反向代理，就填域名），**不会**改变来源限制。这样把链接改成局域网地址或域名都不会意外放开访问；反过来也不会因为填了域名就把自己锁在外面。
- 若同时开启 `BOTUI_ALLOW_REMOTE=true` 与 `BOTUI_AUTH=false`，启动时会打印一条醒目警告 —— 这是「任何人只要能访问该地址就能查看全部记录并冒充机器人」的状态。
- **链接里带的是单一密钥**。`?token=xxx` 就是全部凭据：谁能拿到链接，谁就能查看全部聊天记录并以机器人的身份发消息、撤回消息。地址只出现在**服务端控制台日志**里（不会发到任何聊天窗口），请自行保管好日志输出。
- **`BOTUI_AUTH=false` 意味着完全开放**（在来源限制之内）：任何能访问该地址的人都能查看记录、以机器人身份发言和撤回；启动日志里也会明确提示「令牌鉴权已关闭」。
- **图片代理**：`/api/media` 需要令牌，且**只代理公网地址**。它会把目标域名解析一遍，只要解析结果里有环回、内网、链路本地（含 `169.254.169.254` 这类云元数据地址）等非公网地址就拒绝，重定向每一跳都会重新校验，因此不能拿它当跳板去读内网服务。如果你的图床确实在内网，才需要打开 `BOTUI_MEDIA_ALLOW_PRIVATE=true`。
- **文件代理**：`/api/file`（流式下载/预览）与 `/api/preview`（文本预览）同样需要令牌，并复用与图片代理一致的 **SSRF 校验**与重定向逐跳校验。下载默认限制 100 MB（`BOTUI_FILE_MAX_BYTES`，`0` 表示不限制），文本预览最多读取前 512 KB。若你的文件确实在内网，同样需要 `BOTUI_MEDIA_ALLOW_PRIVATE=true`。
- **附件上传**：`/api/upload`（写入 `uploads/`）需要令牌且仅在**可写模式**下可用（`BOTUI_WRITE_ENABLED=false` 时直接 403），取回附件时 `id` 经严格校验，只允许本插件生成的随机串，`../` 之类的路径穿越会被拒绝；文件名会剥离路径与控制字符后才落盘。单个附件默认上限 20 MB（`BOTUI_UPLOAD_MAX_BYTES`）。上传的附件会先以文件字节驻留在磁盘上，因此**请确保磁盘空间与访问权限可控**；`BOTUI_UPLOAD_ENABLED=false` 可以整体关闭该功能。
- **需要公网访问时**：建议放在反向代理之后，加 HTTPS 与额外的访问控制，并保持令牌鉴权开启；连同 `BOTUI_WRITE_ENABLED=false` 一起使用还可以只读地公开记录。
  - 注意反代与机器人在**同一台机器**上时，请求来源就是 `127.0.0.1`，来源限制会自然放行 —— 此时**鉴权完全依赖令牌**，请务必保持 `BOTUI_AUTH=true` 并确认反代不会把 `?token=` 记进日志。
  - 若反代在另一台机器上，才需要 `BOTUI_ALLOW_REMOTE=true`。

## 🌐 跨平台说明

- 用户与场景信息统一由 `nonebot-plugin-uninfo` 的 `Session` 提供，发送目标由 `nonebot-plugin-alconna` 的 `Target` 构造，因此插件本身不绑定任何适配器；`supported_adapters` 直接继承自这两个插件。
- **多机器人**：每个机器人（`self_id`）的会话彼此独立 —— 会话 key 采用 `self_id:kind_id` 的形式内嵌机器人 ID，同名群在 A、B 两个机器人下就是两条不同的会话；每条消息都带上产生它的适配器名与 `self_id`。WebUI 的发送只会用**会话自己的机器人**，它不在线时返回 503 而不会改用别的机器人。
- 只要适配器支持对应的能力就能用：私聊/群聊识别、消息段解析、以及通过 alconna 的 exporter 发送消息。召回（撤回）需要该适配器在 alconna 里实现了对应的 exporter，不支持时网页会提示「适配器 xxx 不支持撤回」。
- `BOTUI_EXCLUDE_ADAPTERS` 可以按适配器名（如 `["OneBot V12"]`）排除某些平台的记录。

## 🧪 开发 / 测试

```bash
uv sync                  # 安装依赖
uv run poe test          # 跑测试（pytest + 覆盖率，产出 coverage.xml / junit.xml）
uv run ruff check .      # lint
uv run ruff format .     # 格式化
uv run poe bump patch    # 版本号变更（bump-my-version）
```

## 📄 协议

MIT © 2026 Lonely-Sails
