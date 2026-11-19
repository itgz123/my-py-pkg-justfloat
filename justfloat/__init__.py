"""justfloat —— VOFA+ JustFloat 协议 TCP 服务端。

固定 10Hz 批量发送，供仿真主循环（240Hz+）把数据推给 VOFA+ 波形软件。
纯标准库实现，无第三方运行时依赖。

用法::

    from justfloat import JustFloatServer

    with JustFloatServer(channels=6, port=12345) as server:
        while True:
            server.set(0, joint[0])   # 写通道
            server.set(1, joint[1])
            server.send()             # 快照入队（微秒级）
            p.stepSimulation()

兼容两种导入方式::

    import justfloat                   # justfloat.JustFloatServer
    import justfloat.justfloat as jf   # jf.JustFloatServer
"""

from .justfloat import JustFloatServer

__version__ = "0.1.0"

__all__ = ["JustFloatServer", "__version__"]
