"""vofa_plus —— VOFA+ JustFloat 协议 TCP 服务端（发送 + 命令接收）。

固定 10Hz 批量发送，供仿真主循环（240Hz+）把数据推给 VOFA+ 波形软件；
同时接收 VOFA+ 控件（Slider/Button 等）发来的 ``cmdN:value`` 命令，
供主线程通过 :meth:`get` / :meth:`get_data` 读取。纯标准库实现，无第三方
运行时依赖。

用法::

    from vofa_plus import JustFloatServer

    with JustFloatServer(channels=6, port=12345) as server:
        while True:
            server.set(0, joint[0])   # 写通道
            server.set(1, joint[1])
            server.send()             # 快照入队（微秒级）

            kp = server.get(0, default=5.5)   # 读 VOFA+ 滑块 cmd0
            p.stepSimulation()

兼容两种导入方式::

    import vofa_plus                   # vofa_plus.JustFloatServer
    import vofa_plus.server as server  # server.JustFloatServer
"""

from .server import (
    ChannelOverflowWarning,
    FLOAT32_MAX,
    JustFloatServer,
)

__version__ = "0.3.0"

__all__ = [
    "JustFloatServer",
    "ChannelOverflowWarning",
    "FLOAT32_MAX",
    "__version__",
]
