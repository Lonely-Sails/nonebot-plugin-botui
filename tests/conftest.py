import os
import sys
import json
import shutil
import importlib
import importlib.abc
import importlib.util
from typing import cast
from pathlib import Path

import pytest
import nonebot
import pytest_asyncio
from pytest_asyncio import is_async_test
from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

if Path('.env.dev').exists():
    os.environ['ENVIRONMENT'] = 'dev'
else:
    os.environ['ENVIRONMENT'] = 'test'

DATA_DIR = '/tmp/nonebot_plugin_botui_test'

# pytest-xdist 会同时跑多个进程（pyproject 里是 -n auto）。这些进程共享同一个
# 测试数据目录，于是 A 进程的 store.reset() 会把 B 进程刚写的数据删掉，出现
# 随机失败。带上 worker 编号，让每个进程用各自的数据库。
#
# 注意这里必须直接赋值、不能用 setdefault：xdist 的 worker 是控制器 fork 出来
# 的子进程，会继承控制器里已经设好的环境变量，用 setdefault 的话 worker 拿到
# 的还是控制器那份不带编号的路径。
_WORKER = os.environ.get('PYTEST_XDIST_WORKER', '')
if _WORKER:
    DATA_DIR = f'{DATA_DIR}_{_WORKER}'

# 路径交给 nonebot-plugin-localstore 决定（插件本身不再自己拼目录），所以测试
# 也用 localstore 的插件级配置来重定向，这样跑测试时走的就是线上那条路径解析。
os.environ['LOCALSTORE_PLUGIN_DATA_DIR'] = json.dumps(
    {'nonebot_plugin_botui': DATA_DIR}
)
os.environ.setdefault('BOTUI_AUTH', 'true')
os.environ.setdefault('BOTUI_TOKEN', 'test-token')
os.environ.setdefault('BOTUI_SEND_INTERVAL', '0')
os.environ.setdefault('BOTUI_RESOLVE_AT_NAME', 'false')
# 媒体库：用一组「方便测试」的参数 —— ttl/retention 缩小到秒级（用例把时间戳
# 拨回过去即可触发回收，不必真的等），配额收紧到 1MB（够用又不至于写得很多）。
os.environ.setdefault('BOTUI_MEDIA_TTL', '100')
os.environ.setdefault('BOTUI_MEDIA_RETENTION', '1000')
os.environ.setdefault('BOTUI_MEDIA_MAX_BYTES', str(1024 * 1024))
os.environ.setdefault('BOTUI_MEDIA_MAX_FILES', '1000')
os.environ.setdefault('BOTUI_MEDIA_FILE_MAX_BYTES', str(1024 * 1024))
# alconna 默认按 message_id 缓存解析结果（线上能省一次消息序列化），但测试里
# 每个用例都是独立的世界，缓存会让上一个用例的消息串到下一个用例里。
os.environ.setdefault('ALCONNA_CACHE_MESSAGE', 'false')


def pytest_collection_modifyitems(items: list[pytest.Item]):
    pytest_asyncio_tests = (item for item in items if is_async_test(item))
    session_scope_marker = pytest.mark.asyncio(loop_scope='session')
    for async_test in pytest_asyncio_tests:
        async_test.add_marker(session_scope_marker, append=False)


@pytest.fixture(scope='session', autouse=True)
def after_nonebot_init(after_nonebot_init: None):
    # 每次测试都从干净的数据库开始，避免上一次运行的记录影响断言
    shutil.rmtree(DATA_DIR, ignore_errors=True)

    # 加载适配器
    driver = nonebot.get_driver()
    driver.register_adapter(OnebotV11Adapter)

    # 加载插件（plugin_dirs = ["src/"]，所以模块名是 src.nonebot_plugin_botui）
    nonebot.load_from_toml('pyproject.toml')
    _alias_plugin_modules()


def _alias_plugin_modules() -> None:
    """让 ``nonebot_plugin_botui`` 指向 ``src.nonebot_plugin_botui``。

    ``[tool.nonebot] plugin_dirs = ["src/"]`` 会让插件以 ``src.nonebot_plugin_botui``
    这个名字被导入，而测试代码里写的是 ``from nonebot_plugin_botui.models import ...``。
    如果不做处理，后者会把包 ``__init__.py`` 再执行一遍：启动钩子、采集钩子都会
    重复注册，同一份状态在两个模块对象里各有一份，测试行为就变得难以预测。

    子模块是惰性导入的，所以这里装一个 meta path finder 按需转发。
    """
    package = 'src.nonebot_plugin_botui'
    alias = 'nonebot_plugin_botui'
    module = sys.modules.get(package)
    if module is None:  # pragma: no cover - 正常加载时不会发生
        return
    sys.modules[alias] = module

    class _AliasFinder:
        """把 ``nonebot_plugin_botui.x`` 的导入转发到 ``src.nonebot_plugin_botui.x``"""

        def find_spec(self, fullname: str, path=None, target=None):
            if not fullname.startswith(f'{alias}.'):
                return None
            real = f'{package}{fullname[len(alias) :]}'
            try:
                real_module = importlib.import_module(real)
            except ImportError:
                return None
            sys.modules[fullname] = real_module
            # _AliasLoader 是鸭子类型的加载器（只需要 create_module /
            # exec_module），并不继承 importlib 的 Loader，所以这里要显式转换
            loader = cast('importlib.abc.Loader', _AliasLoader(real_module))
            return importlib.util.spec_from_loader(fullname, loader)

    class _AliasLoader:
        def __init__(self, real_module) -> None:
            self._real = real_module

        def create_module(self, spec):
            return self._real

        def exec_module(self, module) -> None:
            pass

    if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _AliasFinder())


@pytest.fixture(autouse=True)
def clean_uninfo_cache():
    """uninfo 会缓存会话信息（默认 300 秒）。

    同一个事件里事件预处理器和匹配器都会调用 ``get_session``，缓存能让它们共用
    一次查询（这也是线上行为）；但用例之间必须清空，否则后面的用例拿不到
    ``get_group_info`` 之类的调用，预期的 API 调用序列就对不上了。
    """
    from nonebot_plugin_uninfo.adapters import INFO_FETCHER_MAPPING

    for fetcher in INFO_FETCHER_MAPPING.values():
        fetcher.clean()
    yield
    for fetcher in INFO_FETCHER_MAPPING.values():
        fetcher.clean()


@pytest.fixture(autouse=True)
async def flush_store(clean_uninfo_cache: None):
    """等待写入队列清空，避免异步落库影响 ``store.count()`` 之类的断言。

    进入和退出都要排空：只在退出时排空的话，前面用例留下的记录仍然可能在
    后面用例的 ``count()`` 与 ``flush()`` 之间被后台写循环提交，于是
    「count() 应该 +1」这类断言会随机失败（``-n auto` 下尤其明显）。
    """
    module = sys.modules.get('src.nonebot_plugin_botui')
    store = None
    if module is not None:
        from src.nonebot_plugin_botui.capture import store_ref

        store = store_ref()
    if store is not None:
        await store.flush()
    yield
    if store is not None:
        await store.flush()


@pytest.fixture(scope='session')
def botui(after_nonebot_init: None):
    """插件模块。

    插件在 import 阶段就会 ``require`` 其他插件并读取配置，
    所以只能在 NoneBot 初始化之后导入。
    """
    module = sys.modules.get('src.nonebot_plugin_botui')
    if module is None:  # pragma: no cover - 正常加载时不会发生
        raise RuntimeError('BotUI 插件没有被加载，请检查 pyproject.toml 的 plugin_dirs')
    return module


@pytest_asyncio.fixture(loop_scope='session', scope='session')
async def media_store(after_nonebot_init, botui):
    """把媒体库挂到测试用的消息库连接上（与线上启动钩子做的事一致）。

    媒体库是异步的、且必须 ``attach`` 到一条 aiosqlite 连接后才可用，所以这里
    必须真的启动 store。会话级只挂一次；每个用例的清理由下面 function 级的
    ``media`` 夹具负责（它同样被 ``client`` 间接依赖，保证接口测试也拿到已挂载
    的媒体库）。
    """
    store = botui._get_store()
    await store.start()
    m = botui._get_media()
    if m.enabled and not m.ready and store.db is not None:
        await m.attach(store.db, store.lock)
    return m


@pytest_asyncio.fixture(loop_scope='session')
async def media(media_store):
    """媒体库（每个用例前后各清一次）。

    媒体元数据都在同一张 ``blobs`` 表里，不同用例（包括自建的实例）共享它，
    不清就会互相串味。
    """
    m = media_store
    await m.clear('all')
    m.reset_stats()
    yield m
    await m.clear('all')


@pytest_asyncio.fixture(loop_scope='session', scope='session')
async def client(media_store, after_nonebot_init):
    """直接打挂载后的 ASGI 应用，不需要真的监听端口。

    用 ``httpx.AsyncClient`` 而不是同步的 ``TestClient``：后者会另开一个事件循环，
    和 aiosqlite 的连接对不上。依赖 ``media_store`` 是为了确保上传 / 取回接口
    用到的媒体库已经挂到同一条连接上。
    """
    from httpx import AsyncClient, ASGITransport
    from nonebot.drivers import ASGIMixin

    driver = nonebot.get_driver()
    if not isinstance(driver, ASGIMixin):  # pragma: no cover - 测试固定用 fastapi 驱动
        pytest.skip('测试需要 FastAPI 驱动')
    transport = ASGITransport(app=driver.asgi)
    async with AsyncClient(transport=transport, base_url='http://botui.test') as c:
        yield c
