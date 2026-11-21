# vofa_plus

VOFA+ **JustFloat** 协议的 Python TCP 服务端：把仿真/控制程序里的数据推到
VOFA+ 上位机看波形，同时接收 VOFA+ 控件（Slider、Button 等）发来的
`cmdN:value` 命令，实现上位机调参/闭环控制。纯标准库实现，无第三方运行时依赖。

> 包名已从 `justfloat` 重命名为 `vofa_plus`；旧的 `import justfloat` 不再可用。

## 设计要点

| 特性 | 说明 |
| --- | --- |
| 非阻塞 `set()` / `send()` | 主线程只写内存 + `struct.pack`，微秒级返回 |
| 默认 10Hz 发送 | 发送线程默认每 0.1s 把队列里所有帧 `b"".join` 后一次 `sendall()`，周期可配 |
| 自动 FIFO 缓冲 | 按 `send_rate_hz`、`tcp_rate_hz`、`channels` 估算帧数/内存，满了丢最旧帧 |
| 命令接收 | 独立接收线程解析 `cmd0:1.23456\n`，主线程用 `get()` / `get_data()` 取最新值 |
| 单客户端 | 接受一个客户端，断开后自动等待重连 |
| 连接查询 | `is_connected` 属性，可在发送前判断 VOFA+ 是否已连接 |

## 安装

本仓库设计为以 **git submodule** 形式放进各个项目，然后可编辑安装到该项目的虚拟环境：

```bash
git submodule add git@github.com:itgz123/my-py-pkg-justfloat.git my-py-pkg-justfloat
pip install -e ./my-py-pkg-justfloat
```

## 用法

### 发送数据到 VOFA+

```python
from vofa_plus import JustFloatServer

with JustFloatServer(channels=6, port=12345) as server:
    while True:
        server.set(0, joint[0])
        server.set(1, joint[1])
        if server.is_connected:     # 未连接时可直接跳过，避免积压
            server.send()           # 快照入队，发送线程默认每 0.1s 批量发出
        p.stepSimulation()
```

### 缓冲区自动估算

`JustFloatServer` 现在按“主循环 1kHz 调 `send()`、TCP 10Hz 发送”估算队列容量：

```python
server = JustFloatServer(
    channels=8,          # 8 通道 -> 每帧 8*4+4 = 36 B
    send_rate_hz=1000,   # 假设每秒 send() 1000 次
    tcp_rate_hz=10,      # TCP 线程每秒发 10 次
    buffer_seconds=1.0,  # 希望缓存 1 秒数据
    max_buffer_bytes=1 << 20,  # 内存软上限 1 MiB
)
```

计算方式：

- `frames_per_cycle = ceil(send_rate_hz / tcp_rate_hz)`，默认 100 帧/周期；
- `frame_bytes = 4 * channels + 4`；
- 目标帧数 = `send_rate_hz * buffer_seconds`；
- 再用 `max_buffer_bytes // frame_bytes` 限制内存；
- 至少保留 `MAX_BUF=256` 帧和 2 个 TCP 周期，避免极端参数下立刻丢帧。

8 通道默认得到 `1000` 帧（约 35 KiB）；1000 通道时受 1 MiB 限制得到 `261` 帧。
实际值可通过 `server.buffer_frames` / `server.buffer_bytes` / `server.frames_per_cycle`
读取，也可用 `server.set_buffer_size()` 手动覆盖。

### 获取 VOFA+ 发来的数据

VOFA+ 的 Slider 可以绑定一条 Str 模式命令，命令里用 `%f` 作参数占位符，
例如 `cmd0:%.5f\n`。拖动滑块时 VOFA+ 会发出：

```
cmd0:1.23456
cmd1:-2.50000
...
```

Python 侧无需自己解析，直接用 `get()` 取最新值：

```python
from vofa_plus import JustFloatServer

with JustFloatServer(channels=4, port=12345) as server:
    while True:
        kp = server.get(0, default=5.5)   # cmd0，未收到时用 5.5
        ki = server.get(1, default=1.5)   # cmd1
        kd = server.get("cmd2", default=0.0)

        # get_data() 返回全部已收到命令的副本：
        # {"cmd0": 5.5, "cmd1": 1.5, "cmd2": 0.0}
        print(server.get_data())

        server.set(0, t)
        server.send()
        p.stepSimulation()
```

VOFA+ 侧配置（与 `vofa+_config/vofa+.cmds.json` 对应）：

1. 在左侧「命令」中新增命令 0–9，模式选 **Str**，内容分别填
   `cmd0:%.5f\n`、`cmd1:%.5f\n` … `cmd9:%.5f\n`。
2. 拖入 10 个 Slider，右键「绑定命令」，分别绑定命令 0–9。
3. Slider 的当前值会被替换进 `%f`，例如 `cmd0:5.50000\n`。

命令解析规则：

- 每行一条，格式 `cmdN:<float>`，支持 `\r\n`、负号、小数、科学计数法。
- 每个 `cmdN` 只保留**最新值**；同一命令连续发送时后到的覆盖先到的。
- 非法行、未知格式会被静默忽略，不会影响主循环。
- 新客户端连接时会清空旧命令值，避免重连后读到上一次的滑块值；也可手动
  `server.clear_data()`。
- 未收到的命令，`get(cmd, default)` 返回 `default`（默认 `None`）。

## API

| 方法 / 属性 | 说明 |
| --- | --- |
| `JustFloatServer(channels=4, port=12345, *, send_rate_hz=1000, tcp_rate_hz=10, buffer_seconds=1.0, max_buffer_bytes=1<<20)` | 创建并启动；`port=0` 时由系统分配 |
| `set(channel, value)` | 写 JustFloat 通道值，纳秒级，不阻塞 |
| `send()` | 当前缓冲区打包入队，微秒级，不阻塞 |
| `get(cmd, default=None)` | 取 `cmdN` 最新值；`cmd` 可为 `0` 或 `"cmd0"` |
| `get_data()` | 返回 `dict[str, float]` 快照，如 `{"cmd0": 1.23}` |
| `has_data` | 属性：是否已收到过任意 `cmdN` |
| `clear_data()` | 清空已接收的命令值 |
| `is_connected` | 属性：当前是否有 VOFA+ 客户端连接 |
| `port` / `send_interval` | 属性：实际监听端口 / TCP 发送周期 |
| `frame_bytes` / `frames_per_cycle` | 属性：单帧字节数 / 每个 TCP 周期的预计帧数 |
| `buffer_frames` / `buffer_bytes` | 属性：当前队列容量（帧 / 字节） |
| `set_buffer_size(size)` | 运行时手动调整发送缓冲帧数（>=1） |
| `close()` / `with` | 停止并释放资源（幂等） |

### TCP 未连接时

- `send()` **不会阻塞、也不会报错**，仍会把帧打包压入有界队列；队列满（默认按
  1kHz/10Hz/1s 自动估算，至少 256 帧）后丢最旧帧。发送线程在无客户端时只做
  `accept()` 等待。
- 客户端连上后，队列中积压的帧会在下一个 TCP 发送周期一次性发出。
  如果不希望重连瞬间收到旧数据，发送前先判断 `server.is_connected`。
- 客户端断开后，接收线程会较快感知 EOF 并把 `is_connected` 置回 `False`。

## 溢出保护

缓冲区是 `array('f')`（float32），超出 `3.4e38` 或 `inf/nan` 的值会被静默存成
`inf`，VOFA+ 端只看到曲线骤停，很难定位。`set()` 会在写缓冲区前检查，每个
通道**只提醒一次**：

```python
import warnings
from vofa_plus import ChannelOverflowWarning, JustFloatServer

with JustFloatServer(channels=2) as server:
    with warnings.catch_warnings(record=True) as caught:
        server.set(0, 1e39)      # ChannelOverflowWarning: 超出 float32 范围
        server.set(1, float("nan"))  # ChannelOverflowWarning: 非有限
```

常见触发原因是上游仿真数值发散（例如步长超出积分器稳定域），警告里会提示
先检查上游计算。`FLOAT32_MAX` 也一并导出，便于调用方自己预判。

## 帧格式

### JustFloat（Python → VOFA+）

```
[float1][float2]...[floatN][0x00 0x00 0x80 0x7f]
 └── 小端 4 字节浮点 ──┘ └──── 帧尾 (+Inf) ────┘
```

### 命令（VOFA+ → Python）

```
cmd0:1.23456\n
cmd1:-2.50000\n
```

## 目录结构

```
my-py-pkg-justfloat/
├── pyproject.toml        # 打包配置（setuptools，版本取自 __version__）
├── README.md
└── vofa_plus/
    ├── __init__.py       # 对外导出 JustFloatServer
    └── server.py         # 实现：JustFloat 发送 + cmdN 接收
```
