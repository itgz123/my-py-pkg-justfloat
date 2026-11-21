# justfloat

VOFA+ **JustFloat** 协议的 Python TCP 服务端，用于把仿真/控制程序里的数据推到
VOFA+ 上位机看波形。纯标准库实现，无第三方运行时依赖。

## 设计要点

| 特性 | 说明 |
| --- | --- |
| 非阻塞 `set()` / `send()` | 主线程只写内存 + `struct.pack`，微秒级返回 |
| 固定 10Hz 发送 | 发送线程每 0.1s 把队列里所有帧 `b"".join` 后一次 `sendall()` |
| 有界 FIFO 缓冲 | `deque(maxlen=256)`，满了丢最旧帧保实时性，可 `set_buffer_size()` 调整 |
| 单客户端 | 接受一个客户端，断开后自动等待重连 |
| 连接查询 | `is_connected` 属性，可在发送前判断 VOFA+ 是否已连接 |

## 安装

本仓库设计为以 **git submodule** 形式放进各个项目，然后可编辑安装到该项目的虚拟环境：

```bash
git submodule add git@github.com:itgz123/my-py-pkg-justfloat.git my-py-pkg-justfloat
pip install -e ./my-py-pkg-justfloat
```

## 用法

```python
from justfloat import JustFloatServer

with JustFloatServer(channels=6, port=12345) as server:
    while True:
        server.set(0, joint[0])
        server.set(1, joint[1])
        if server.is_connected:     # 未连接时可直接跳过，避免积压
            server.send()           # 快照入队，发送线程每 0.1s 批量发出
        p.stepSimulation()
```

### TCP 未连接时

- `send()` **不会阻塞、也不会报错**，仍会把帧打包压入有界队列；队列满（默认
  256 帧）后丢最旧帧。发送线程在无客户端时只做 `accept()` 等待。
- 客户端连上后，队列中积压的帧会在下一个 10Hz 发送周期一次性发出。
  如果不希望重连瞬间收到旧数据，发送前先判断 `server.is_connected`。
- 客户端断开后，服务端通常要等到下一次 `sendall()` 失败才会把
  `is_connected` 置回 `False`（服务端不读取客户端数据，无法立即感知 FIN）。

## API

| 方法 | 说明 |
| --- | --- |
| `JustFloatServer(channels=4, port=12345)` | 创建并启动（构造即监听 + 启动发送线程） |
| `set(channel, value)` | 写通道值，纳秒级，不阻塞 |
| `send()` | 当前缓冲区打包入队，微秒级，不阻塞 |
| `is_connected` | 属性：当前是否有 VOFA+ 客户端连接 |
| `set_buffer_size(size)` | 运行时调整缓冲帧数（>=1） |
| `close()` / `with` | 停止并释放资源（幂等） |

## 溢出保护

缓冲区是 `array('f')`（float32），超出 `3.4e38` 或 `inf/nan` 的值会被静默存成
`inf`，VOFA+ 端只看到曲线骤停，很难定位。`set()` 会在写缓冲区前检查，每个
通道**只提醒一次**：

```python
import warnings
from justfloat import ChannelOverflowWarning, JustFloatServer

with JustFloatServer(channels=2) as server:
    with warnings.catch_warnings(record=True) as caught:
        server.set(0, 1e39)      # ChannelOverflowWarning: 超出 float32 范围
        server.set(1, float("nan"))  # ChannelOverflowWarning: 非有限
```

常见触发原因是上游仿真数值发散（例如步长超出积分器稳定域），警告里会提示
先检查上游计算。`FLOAT32_MAX` 也一并导出，便于调用方自己预判。

## 帧格式

```
[float1][float2]...[floatN][0x00 0x00 0x80 0x7f]
 └── 小端 4 字节浮点 ──┘ └──── 帧尾 (+Inf) ────┘
```

## 目录结构

```
my-py-pkg-justfloat/
├── pyproject.toml        # 打包配置（setuptools，版本取自 __version__）
├── README.md
└── justfloat/
    ├── __init__.py       # 对外导出 JustFloatServer
    └── justfloat.py      # 实现
```
