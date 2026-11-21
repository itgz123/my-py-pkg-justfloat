"""
VOFA+ JustFloat 协议 TCP 服务端 — 固定频率发送模式
====================================================

架构
----
采用"主线程设值 → send() 预打包 → 发送线程固定 10Hz 拼接发送"的三段流水线:

  主线程 (仿真 @240Hz):
    set(0, ch0) → buffer[0] = ch0          ← 纳秒级，仅写内存
    set(1, ch1) → buffer[1] = ch1
    ...
    send() → pack(buffer) → 入队(bytes)    ← 微秒级，已预打包

  发送线程 (固定 10Hz):
    每 0.1s 唤醒:
      1. 清空队列, 收集所有预打包 bytes
      2. b"".join 拼接为一个大字节串
      3. 一次 sendall() 发出

  关键优化:
    - 预计算 pack 格式字符串 f"<{channels}f" (避免每帧重新创建)
    - struct.pack 在主线程完成 (发送线程不再打包)
    - 发送线程用 b"".join 替代 bytes += (避免 O(N²) 复制, ~7× 快)

缓冲区:
  collections.deque(maxlen=MAX_BUF)
  - 满时自动丢弃最旧帧 (保持实时性)
  - 空时发送线程跳过 (不发空帧)
  - 可通过 set_buffer_size() 动态调整

JustFloat 帧格式
----------------
  [float1] [float2] ... [floatN] [0x00 0x00 0x80 0x7f]
  ├── 小端 4 字节浮点数 ──┤ ├────── 固定帧尾 ──────┤
"""

import socket
import struct
import threading
import array
import collections
import math
import time
import warnings
from typing import Optional

FLOAT32_MAX: float = 3.4028234663852886e38
"""IEEE-754 单精度浮点数最大值（JustFloat 每通道 4 字节 float32）。"""


class ChannelOverflowWarning(UserWarning):
    """通道值超出 float32 范围或非有限，VOFA+ 端会显示 inf/nan。"""


class JustFloatServer:
    """固定 10Hz 发送的 JustFloat TCP 服务端。

    主线程通过 ``set()`` 写入各通道值，通过 ``send()`` 将当前
    快照预打包 (struct.pack) 推入 FIFO 缓冲区。发送线程每 0.1s
    清空缓冲区，b"".join 拼接成一个大包，一次 sendall() 发出。

    未连接客户端时 ``send()`` 仍会入队（满时丢最旧帧），发送线程
    只等待连接；连上后积压帧会一次性发出。需要避免积压时，先判断
    :attr:`is_connected` 再调用 ``send()``。

    用法::

        server = JustFloatServer(channels=6)   # 6 通道

        while True:
            server.set(0, joint[0])            # 设关节角
            server.set(1, joint[1])
            server.set(2, joint[2])
            server.send()                      # 预打包入队
            p.stepSimulation()
    """

    TAIL = b"\x00\x00\x80\x7f"
    """JustFloat 协议帧尾: 0x00 0x00 0x80 0x7f (小端 +Inf)"""

    SEND_INTERVAL = 0.1
    """发送周期 (秒)。固定 10Hz，不可变。"""

    MAX_BUF = 256
    """发送缓冲区容量 (帧数)。满时自动丢弃最旧帧。可通过 set_buffer_size() 修改。"""

    # ================================================================
    # 构造
    # ================================================================

    def __init__(self, channels: int = 4, port: int = 12345):
        """创建服务端并启动发送线程。

        构造即启动: 绑定端口、开始监听、发送线程运行。

        Args:
            channels: 通道数量 (每帧浮点数个数), 默认 4
            port: TCP 监听端口, 默认 12345

        Raises:
            OSError: 端口被占用或地址不可用
            ValueError: channels < 1
        """
        if channels < 1:
            raise ValueError("channels 必须 >= 1")

        self._channels = channels
        self._running = True

        # ---- 主线程写缓冲区 ----
        # array.array('f') = C 层面连续 float 数组, 紧凑且快速
        self._buffer = array.array("f", [0.0]) * channels

        # 已警告过溢出的通道（每通道只提醒一次，避免主循环刷屏）
        self._overflowed_channels: set = set()

        # 预计算 struct.pack 格式字符串 (恒定: <{channels}f)
        self._pack_fmt = f"<{channels}f"

        # ---- 发送线程 FIFO 缓冲区 ----
        # 存储已打包的 bytes (struct.pack + TAIL), 发线程只做拼接
        self._send_queue: collections.deque = collections.deque(maxlen=self.MAX_BUF)

        # ---- 客户端连接 ----
        self._client: Optional[socket.socket] = None

        # ---- TCP 服务端套接字 ----
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("0.0.0.0", port))
        self._socket.listen(1)
        # accept 超时 50ms: 足够让线程响应退出信号, 又不至于阻塞太久
        self._socket.settimeout(0.05)

        # ---- 启动发送线程 ----
        self._thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._thread.start()

        print(
            f"JustFloatServer started: {channels} channels, "
            f"buffer={self.MAX_BUF} frames, port={port}, "
            f"send rate={1/self.SEND_INTERVAL:.0f}Hz"
        )

    # ================================================================
    # 公开 API
    # ================================================================

    def set(self, channel: int, value: float) -> None:
        """设置指定通道的值。仅写内存，纳秒级，永不阻塞。

        若值超出 float32 范围或为 inf/nan，会发一次
        :class:`ChannelOverflowWarning`（每通道只提醒一次）：缓冲区是
        ``array('f')``，超范围的值会被静默存成 ``inf``，VOFA+ 端只看到
        曲线骤停或断线。

        Args:
            channel: 通道索引 (0 ~ channels-1)
            value: 浮点值

        Raises:
            IndexError: channel 超出范围
        """
        is_finite = math.isfinite(value)
        overflow = not is_finite or abs(value) > FLOAT32_MAX
        if overflow and channel not in self._overflowed_channels:
            self._overflowed_channels.add(channel)
            warnings.warn(
                f"通道 {channel} 的值 {value:.6g} "
                f"{'非有限' if not is_finite else '超出 float32 范围'}，"
                f"写入缓冲区后会变成 inf/nan，VOFA+ 端无法显示。"
                f"检查上游计算（常见原因：仿真步长过大导致数值发散）。"
                f"（该通道只提醒一次）",
                ChannelOverflowWarning,
                stacklevel=2,
            )
        self._buffer[channel] = value

    def send(self) -> None:
        """将当前缓冲区快照打包后推入发送队列。微秒级，永不阻塞。

        快照在入队前已完成 struct.pack (预计算格式字符串)。
        发送线程只做字节拼接，不再重复打包。

        队列满时自动丢弃最旧帧 (保持实时性)。
        """
        if not self._running:
            return

        # 直接打包入队: 跳过中间 list 分配, 发线程不再 pack
        self._send_queue.append(struct.pack(self._pack_fmt, *self._buffer) + self.TAIL)

    @property
    def is_connected(self) -> bool:
        """当前是否有 VOFA+ 客户端连接。

        发送线程在 accept 成功后置为 True，在 sendall 失败或 close()
        后置为 False。客户端主动断开时，通常要等到下一次发送失败才会
        反映为 False（本服务端不读取客户端数据，无法立即感知 FIN）。
        """
        return self._client is not None

    def set_buffer_size(self, size: int) -> None:
        """设置发送缓冲区容量 (帧数)。

        改变容量后, 保留现有帧 (超出新容量的最旧帧被丢弃)。
        可在运行中随时调用。

        Args:
            size: 新容量 (必须 >= 1)

        Raises:
            ValueError: size < 1
        """
        if size < 1:
            raise ValueError("buffer size must be >= 1")
        # 用现有内容重建 deque, 按新 maxlen 截断
        self._send_queue = collections.deque(self._send_queue, maxlen=size)
        print(f"Buffer size changed to {size} frames ({len(self._send_queue)} pending)")

    def close(self) -> None:
        """停止服务端并释放资源。幂等。"""
        self._running = False
        self._close_client()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        print("JustFloatServer closed")

    def __enter__(self) -> "JustFloatServer":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ================================================================
    # 发送线程
    # ================================================================

    def _sender_loop(self) -> None:
        """发送线程主循环: 接受连接 + 每 0.1s 清空队列合并发送。

        每周期将所有待发帧拼接成一个大字节串，一次 sendall() 发出。
        相比逐帧发送: ① syscall 从 N 次降为 1 次 ② TCP 段利用率更高
        ③ VOFA+ 收到完整批数据，解析效率更高。

        循环逻辑::

            while _running:
                sleep(0.1)                ← 固定 10Hz 周期
                if 无客户端 → accept()
                while 队列有数据:
                    popleft → pack → 拼入 packet
                if packet 非空:
                    sendall(packet)       ← 一次发出所有帧
        """
        while self._running:
            time.sleep(self.SEND_INTERVAL)

            # ---- 阶段 1: 接受客户端 ----
            if self._client is None:
                sock = self._socket
                if sock is None:
                    continue
                try:
                    client, addr = sock.accept()
                    self._client = client
                    print(f"Client connected: {addr}")
                    # 新连接: 跳过本周期, 等下个周期再发
                    # (让 TCP 连接稳定 + 缓冲区积累数据)
                    continue
                except socket.timeout:
                    continue
                except OSError:
                    if self._running:
                        time.sleep(0.1)
                    continue

            # ---- 阶段 2: 清空队列, 拼接所有帧 ----
            # b"".join 比 bytes += 快约 7× (避免 O(N²) 复制)
            chunks = []
            while True:
                try:
                    packet = self._send_queue.popleft()
                except IndexError:
                    break  # 队列清空
                chunks.append(packet)

            if not chunks:
                continue  # 队列为空, 跳过此周期

            # 所有帧已预打包 (send() 时已完成 struct.pack)
            # 此处仅做一次拼接 + 一次 sendall
            try:
                self._client.sendall(b"".join(chunks))
            except OSError:
                self._close_client()

    def _close_client(self) -> None:
        """关闭客户端连接。幂等。"""
        if self._client is not None:
            try:
                self._client.close()
            except OSError:
                pass
            self._client = None
            print("Client disconnected")


# ================================================================
# 使用示例
# ================================================================
if __name__ == "__main__":
    import math

    CHANNELS = 4

    with JustFloatServer(channels=CHANNELS, port=12345) as server:
        t = 0.0
        try:
            while True:
                # 写入各通道
                server.set(0, math.sin(t))
                server.set(1, math.sin(2 * t))
                server.set(2, math.sin(3 * t))
                server.set(3, math.sin(4 * t))
                # 推入发送队列
                server.send()

                t += 0.001
                time.sleep(0.001)
        except KeyboardInterrupt:
            pass
