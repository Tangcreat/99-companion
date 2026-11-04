"""Android 启动入口（Chaquopy 调用）。

Kotlin 侧把数据目录（filesDir）和**设备时区偏移**一起传进来。时区必须由 Kotlin 给：
Chaquopy 里的 Python 不保证拿得到手机时区，猜错就会把中午当凌晨，
角色会答「现在是凌晨一点」——这是实测踩过的坑。
"""
import os
import sys


def start(base_dir, port=8787, tz_offset_min=None, app_version=""):
    base = str(base_dir)
    os.environ["STARLIT_DATA_ROOT"] = base
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    # 先定好时钟再 import server —— App 在模块导入时就初始化了
    from core import clock
    if tz_offset_min is not None:
        clock.set_offset(tz_offset_min)
    if app_version:
        os.environ["STARLIT_APP_VERSION"] = str(app_version)

    import server
    server.start_server(int(port), "127.0.0.1")
    return "ok"


def set_tz(tz_offset_min):
    """运行期设备改了时区，可再调一次。"""
    from core import clock
    clock.set_offset(tz_offset_min)
    return clock.tz_name()
