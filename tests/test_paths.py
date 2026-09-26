"""路径解析：确保所有文件都由 nonebot-plugin-localstore 提供。

这些用例守住三件事：

1. 插件不再自己拼数据目录，路径常量必须落在 localstore 给出的目录里；
2. 目录重定向走的是 localstore 的配置（``LOCALSTORE_PLUGIN_DATA_DIR``），
   而不是插件自己的配置项；
3. 路径在**导入时**就解析成普通常量，运行期不需要再问 localstore ——
   localstore 靠调用栈反查插件，栈上必须有插件自己的帧才查得到。
"""

from __future__ import annotations


def test_paths_come_from_localstore():
    """数据库与令牌都应在 localstore 给出的插件数据目录下。"""
    from nonebot_plugin_botui import paths

    assert paths.DB_FILE.parent == paths.DATA_DIR
    assert paths.TOKEN_FILE.parent == paths.DATA_DIR
    assert paths.DB_FILE.name == 'botui.sqlite3'
    assert paths.TOKEN_FILE.name == 'token.txt'


def test_plugin_is_detectable_from_its_submodules():
    """localstore 靠「调用栈里的模块属于哪个插件」来定位目录。

    它用 ``get_plugin_by_module_name`` 反查，而插件加载后模块名是
    ``src.nonebot_plugin_botui``。这里确认包名与子模块名都能反查到插件 ——
    这正是 ``paths.py`` 在导入时能拿到正确目录的前提。
    """
    import nonebot
    from nonebot.plugin import get_plugin_by_module_name

    for module_name in (
        'src.nonebot_plugin_botui',
        'src.nonebot_plugin_botui.paths',
    ):
        plugin = get_plugin_by_module_name(module_name)
        assert plugin is not None, module_name
        assert plugin.id_ == 'nonebot_plugin_botui'

    assert nonebot.get_plugin('nonebot_plugin_botui') is not None


def test_data_dir_is_created_by_localstore():
    """localstore 的目录函数自带 mkdir，插件不该再依赖额外的建目录逻辑。"""
    from nonebot_plugin_botui import paths

    assert paths.DATA_DIR.is_dir()


def test_paths_match_configured_override():
    """conftest 用 LOCALSTORE_PLUGIN_DATA_DIR 把目录指到了临时路径，
    说明插件确实走的是 localstore 的配置解析，而不是写死的路径。"""
    import os
    import json

    from nonebot_plugin_botui import paths

    configured = json.loads(os.environ['LOCALSTORE_PLUGIN_DATA_DIR'])
    assert str(paths.DATA_DIR) == configured['nonebot_plugin_botui']


def test_paths_are_plain_constants_not_recomputed():
    """路径必须是普通常量，不能是「用的时候现问 localstore」。

    localstore 按调用栈反查插件，栈上没有插件帧时会抛
    ``RuntimeError: Cannot detect caller plugin``。这里用一个全新线程模拟
    这种环境（线程栈里只有标准库的引导帧），确认现取会失败、而插件常量照常可用。
    """
    import threading

    from nonebot_plugin_botui import paths

    box: dict[str, object] = {}

    def worker() -> None:
        from nonebot_plugin_localstore import get_plugin_data_dir

        try:
            box['fresh'] = get_plugin_data_dir()
        except RuntimeError as e:
            box['fresh'] = e
        box['constant'] = paths.DATA_DIR

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert isinstance(box['fresh'], RuntimeError), (
        '若 localstore 在无插件帧的线程里也能解析目录，这里的说明就该更新'
    )
    assert box['constant'] == paths.DATA_DIR


def test_module_does_not_expose_own_dir_config():
    """数据目录交给 localstore 管，插件不再提供自己的目录配置项。"""
    from nonebot_plugin_botui.config import Config

    assert 'botui_data_dir' not in Config.model_fields
