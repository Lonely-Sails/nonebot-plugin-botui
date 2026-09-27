"""本地运行 nonebot-plugin-botui 的入口。

插件本身不含可执行入口（正式发布时由宿主项目加载），这个文件只用于在
本仓库里把 WebUI 跑起来：加载 ``pyproject.toml`` 里的 ``[tool.nonebot]``
配置（``plugin_dirs = ["src/"]``），注册 OneBot V11 适配器，然后启动驱动。

    uv run bot.py

WebUI 地址会打印到控制台日志（带访问令牌）。
"""

import nonebot
from nonebot.adapters.qq import Adapter as QQAdapter
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(QQAdapter)
driver.register_adapter(OneBotV11Adapter)

nonebot.load_from_toml('pyproject.toml')

if __name__ == '__main__':
    nonebot.run()
