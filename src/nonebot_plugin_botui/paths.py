"""插件用到的全部文件路径，统一由 nonebot-plugin-localstore 提供。

为什么单独放一个模块：localstore 的 ``get_plugin_data_dir()`` 是靠**调用栈**
反查「哪个插件在要目录」的（见它的 ``_get_caller_plugin``：逐层向上找帧，
按模块名查到已注册的插件为止）。这带来两点麻烦：

1. 只有栈上**存在**插件自己的帧时才查得到；从 ``__main__``、别名模块名或
   完全外部的回调里调用，会抛 ``RuntimeError: Cannot detect caller plugin``；
2. 每次调用都要走一遍栈，结果取决于「谁在调」，属于隐式上下文依赖。

把路径在**插件模块导入时**解析一次并存成常量，就绕开了这两点：插件加载时
栈上必然是插件自己的帧，之后全项目只读这几个常量，行为稳定。

存储位置完全交给 localstore 决定，可用它的配置项覆盖，例如::

    LOCALSTORE_DATA_DIR=/srv/nonebot/data
    LOCALSTORE_PLUGIN_DATA_DIR={"nonebot_plugin_botui": "/srv/botui/data"}
"""

from __future__ import annotations

from pathlib import Path

from nonebot_plugin_localstore import (
    get_plugin_data_dir,
    get_plugin_cache_dir,
    get_plugin_data_file,
)

#: 插件数据目录（localstore 的目录函数自带 mkdir，这里不必再补）
DATA_DIR: Path = get_plugin_data_dir()
#: 插件缓存目录（可被删的派生数据都放这里，见 filecache.py）
CACHE_DIR: Path = get_plugin_cache_dir()
#: SQLite 数据库文件
DB_FILE: Path = get_plugin_data_file('botui.sqlite3')
#: 自动生成的访问令牌文件
TOKEN_FILE: Path = get_plugin_data_file('token.txt')
#: WebUI 里上传、等待发送的附件目录（每个附件一个子目录，见 uploads.py）
UPLOAD_DIR: Path = DATA_DIR / 'uploads'
#: 收到的图片/文件缓存目录（每个资源一个子目录，见 filecache.py）
FILE_CACHE_DIR: Path = CACHE_DIR / 'files'
