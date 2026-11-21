"""
VOFA+ JustFloat 协议 TCP 服务端 — 固定频率发送 + 命令接收
========================================================

架构
----
发送方向（主线程 → VOFA+）采用"主线程设值 → send() 预打包 → 发送线程
固定频率拼接发送"的三段流水线:

  主线程 (仿真 @240Hz):
    set(0, ch0) → buffer[0] = ch0          ← 纳秒级，仅写内存
    set(1, ch1) → buffer[1] = ch1
    ...
    send() → pack(buffer) → 入队(bytes)    ← 微秒级，已预打包

  发送线程 (默认 10Hz):
    每个 TCP 周期唤醒:
      1. 清空队列, 收集所有预打包 bytes
      2. b"".join 拼接为一个大字节串
      3. 一次 sendall() 发出

接收方向（VOFA+ 控件 → 主线程）由独立接收线程完成:

  VOFA+ Slider 绑定命令 "cmd0:%.5f\\n"
    → 发出 "cmd0:1.23456\\n"
    → 接收线程按行解析 cmdN:float
    → 主线程 get(0) / get_data() 读取最新值

  关键优化:
    - 预计算 pack 格式字符串 f"<{channels}f" (避免每帧重新创建)
    - struct.pack 在主线程完成 (发送线程不再打包)
    - 发送线程用 b"".join 替代 bytes += (避免 O(N²) 复制, ~7× 快)

缓冲区:
  collections.deque(maxlen=自动估算的帧数)
  - 满时自动丢弃最旧帧 (保持实时性)
  - 空时发送线程跳过 (不发空帧)
  - 可根据 send_rate_hz / tcp_rate_hz / channels 自动估算
  - 可通过 set_buffer_size() 动态调整

JustFloat 帧格式
----------------
  [float1] [float2] ... [floatN] [0x00 0x00 0x80 0x7f]
  ├── 小端 4 字节浮点数 ──┤ ├────── 固定帧尾 ──────┤

命令格式 (VOFA+ → 本服务端)
---------------------------
  cmd0:1.23456\\n
  cmd1:-2.50000\\n
  ...

  每个 cmdN 只保留最新值；非法行会被静默忽略。
"""

import array
import collections
import math
import re
import select
import socket
import struct
import threading
import time
import warnings
from typing import Dict, Optional, Union, overload

FLOAT32_MAX: float = 3.4028234663852886e38
"""IEEE-754 单精度浮点数最大值（JustFloat 每通道 4 字节 float32）。"""


class ChannelOverflowWarning(UserWarning):
    """通道值超出 float32 范围或非有限，VOFA+ 端会显示 inf/nan。"""


class JustFloatServer:
    """默认 10Hz 发送的 JustFloat TCP 服务端，同时接收 cmdN 命令。

    主线程通过 ``set()`` 写入各通道值，通过 ``send()`` 将当前
    快照预打包 (struct.pack) 推入 FIFO 缓冲区。发送线程每个 TCP 周期
    清空缓冲区，b"".join 拼接成一个大包，一次 sendall() 发出。

    VOFA+ 控件（如 Slider）发来的 ``cmdN:value`` 命令由接收线程解析，
    主线程用 :meth:`get` / :meth:`get_data` 读取最新值。

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

            # 读取 VOFA+ 滑块 cmd0 的值（未收到时用 5.5）
            kp = server.get(0, default=5.5)
            p.stepSimulation()
    """

    TAIL = b"\x00\x00\x80\x7f"
    """JustFloat 协议帧尾: 0x00 0x00 0x80 0x7f (小端 +Inf)"""

    SEND_INTERVAL = 0.1
    """默认 TCP 发送周期 (秒)。10Hz，可被构造参数 ``tcp_rate_hz`` 覆盖。"""

    MAX_BUF = 256
    """发送缓冲区的历史最小容量 (帧数)，现在作为自动计算的兜底下限。"""

    DEFAULT_SEND_RATE_HZ = 1000.0
    """默认假设 ``send()`` 调用频率 (Hz)。"""

    DEFAULT_TCP_RATE_HZ = 10.0
    """默认 TCP 发送频率 (Hz)。"""

    DEFAULT_BUFFER_SECONDS = 1.0
    """默认希望缓冲多少秒的发送数据。"""

    DEFAULT_MAX_BUFFER_BYTES = 1 << 20
    """默认缓冲内存上限 (1 MiB)，通道很多时用于限制帧数。"""

    MAX_LINE_BYTES = 4096
    """单条命令的最大字节数，防止异常客户端让接收缓冲区无限增长。"""

    _CMD_RE = re.compile(
        r"^cmd(\d+)\s*:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$",
        re.IGNORECASE,
    )
    """匹配 ``cmd0:1.23456`` 这类命令，允许数字前后的空白。"""

    # ================================================================
    # 构造
    # ================================================================

    def __init__(
        self,
        channels: int = 4,
        port: int = 12345,
        *,
        send_rate_hz: float = DEFAULT_SEND_RATE_HZ,
        tcp_rate_hz: float = DEFAULT_TCP_RATE_HZ,
        buffer_seconds: float = DEFAULT_BUFFER_SECONDS,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    ):
        """创建服务端并启动发送线程。

        构造即启动: 绑定端口、开始监听、发送线程运行。

        缓冲区按"1kHz 调用 send() + 10Hz TCP"自动估算：

        - 每个 TCP 周期需要 ``ceil(send_rate_hz / tcp_rate_hz)`` 帧；
        - 希望缓存 ``buffer_seconds`` 秒，但受 ``max_buffer_bytes`` 限制；
        - 通道数越多，每帧字节数越大，同样内存下能缓存的帧数越少；
        - 至少保留 ``MAX_BUF`` 帧和 2 个 TCP 周期，避免极端参数下立刻丢帧。

        Args:
            channels: 通道数量 (每帧浮点数个数), 默认 4
            port: TCP 监听端口, 默认 12345；传 0 由系统分配（可用
                :attr:`port` 读取实际端口）
            send_rate_hz: 假设主循环调用 ``send()`` 的频率，默认 1000Hz
            tcp_rate_hz: TCP 发送线程频率，默认 10Hz
            buffer_seconds: 期望缓冲的秒数，默认 1.0
            max_buffer_bytes: 缓冲内存软上限，默认 1 MiB；通道数很大时
                为保证至少 2 个 TCP 周期，实际内存可能略超

        Raises:
            OSError: 端口被占用或地址不可用
            ValueError: channels < 1 或频率/缓冲参数非正
        """
        if channels < 1:
            raise ValueError("channels 必须 >= 1")

        send_rate_hz = float(send_rate_hz)
        tcp_rate_hz = float(tcp_rate_hz)
        buffer_seconds = float(buffer_seconds)
        if send_rate_hz <= 0.0:
            raise ValueError("send_rate_hz 必须为正数")
        if tcp_rate_hz <= 0.0:
            raise ValueError("tcp_rate_hz 必须为正数")
        if buffer_seconds <= 0.0:
            raise ValueError("buffer_seconds 必须为正数")
        if max_buffer_bytes < 1:
            raise ValueError("max_buffer_bytes 必须 >= 1")

        self._channels = channels
        self._running = True
        self._send_rate_hz = send_rate_hz
        self._tcp_rate_hz = tcp_rate_hz
        self._send_interval = 1.0 / tcp_rate_hz

        # ---- 主线程写缓冲区 ----
        # array.array('f') = C 层面连续 float 数组, 紧凑且快速
        self._buffer = array.array("f", [0.0]) * channels

        # 已警告过溢出的通道（每通道只提醒一次，避免主循环刷屏）
        self._overflowed_channels: set = set()

        # 预计算 struct.pack 格式字符串 (恒定: <{channels}f)
        self._pack_fmt = f"<{channels}f"

        # ---- 自动估算发送缓冲区 ----
        # 每帧 = channels 个 float32 + JustFloat 帧尾
        self._frame_bytes = 4 * channels + len(self.TAIL)
        self._frames_per_cycle = max(
            1, math.ceil(send_rate_hz / tcp_rate_hz)
        )
        desired_frames = max(1, math.ceil(send_rate_hz * buffer_seconds))
        memory_cap_frames = max(1, max_buffer_bytes // self._frame_bytes)
        self._buffer_frames = max(
            self.MAX_BUF,
            2 * self._frames_per_cycle,
            min(desired_frames, memory_cap_frames),
        )
        self._buffer_bytes = self._buffer_frames * self._frame_bytes

        # 存储已打包的 bytes (struct.pack + TAIL), 发线程只做拼接
        self._send_queue: collections.deque = collections.deque(
            maxlen=self._buffer_frames
        )

        # ---- 客户端连接 ----
        self._client: Optional[socket.socket] = None
        self._client_lock = threading.Lock()

        # ---- 接收到的 cmdN 最新值 ----
        # 键为 "cmd0"、"cmd1" ...，值为 float
        self._cmd_values: Dict[str, float] = {}
        self._cmd_lock = threading.Lock()
        self._recv_buffer = bytearray()
        self._recv_lock = threading.Lock()
        self._recv_thread: Optional[threading.Thread] = None

        # ---- TCP 服务端套接字 ----
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("0.0.0.0", port))
        self._socket.listen(1)
        # accept 超时 50ms: 足够让线程响应退出信号, 又不至于阻塞太久
        self._socket.settimeout(0.05)
        self._port = self._socket.getsockname()[1]

        # ---- 启动发送线程 ----
        self._thread = threading.Thread(
            target=self._sender_loop, name="VofaPlusSender", daemon=True
        )
        self._thread.start()

        print(
            f"JustFloatServer started: {channels} channels, "
            f"buffer={self._buffer_frames} frames/"
            f"{self._buffer_bytes / 1024:.1f} KiB, "
            f"port={self._port}, send={send_rate_hz:g}Hz, "
            f"tcp={tcp_rate_hz:g}Hz ({self._frames_per_cycle} frames/cycle)"
        )

    # ================================================================
    # 发送 API
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

    # ================================================================
    # 接收 API (VOFA+ 命令 → Python)
    # ================================================================

    @overload
    def get(self, cmd: Union[int, str], default: float) -> float: ...

    @overload
    def get(self, cmd: Union[int, str], default: Optional[float] = None) -> Optional[float]: ...

    def get(self, cmd: Union[int, str], default: Optional[float] = None) -> Optional[float]:
        """获取 ``cmdN`` 的最新值。

        参数既可以是整数 ``0``，也可以是字符串 ``"cmd0"``。

        传入非 ``None`` 的 ``default`` 时，类型检查器会把返回值推断为
        ``float``；不传或传 ``None`` 时返回 ``Optional[float]``。

        Args:
            cmd: 命令编号或名称，如 ``0`` / ``"cmd0"``
            default: 尚未收到该命令时返回的值，默认 ``None``

        Returns:
            最新浮点值；未收到过则返回 ``default``

        Raises:
            ValueError: cmd 为负数或字符串不符合 ``cmdN`` 格式
        """
        key = self._cmd_key(cmd)
        with self._cmd_lock:
            return self._cmd_values.get(key, default)

    def get_data(self) -> Dict[str, float]:
        """返回所有已收到命令的最新值快照。

        Returns:
            ``{"cmd0": 1.23, "cmd1": 4.56}`` 形式的字典；未收到任何命令
            时返回空字典。返回的是副本，可在主线程安全遍历。
        """
        with self._cmd_lock:
            return dict(self._cmd_values)

    @property
    def has_data(self) -> bool:
        """是否已收到过至少一条 ``cmdN`` 命令。"""
        with self._cmd_lock:
            return bool(self._cmd_values)

    def clear_data(self) -> None:
        """清空所有已接收的命令值。幂等，线程安全。"""
        with self._cmd_lock:
            self._cmd_values.clear()

    @property
    def port(self) -> int:
        """实际监听的 TCP 端口（构造时传 0 时尤其有用）。"""
        return self._port

    @property
    def send_interval(self) -> float:
        """TCP 发送周期 (秒)，即 ``1 / tcp_rate_hz``。"""
        return self._send_interval

    @property
    def frame_bytes(self) -> int:
        """单个 JustFloat 帧的字节数：``4 * channels + 4``。"""
        return self._frame_bytes

    @property
    def frames_per_cycle(self) -> int:
        """每个 TCP 周期预计收到的帧数：``ceil(send_rate_hz / tcp_rate_hz)``。"""
        return self._frames_per_cycle

    @property
    def buffer_frames(self) -> int:
        """当前发送队列容量 (帧数)。"""
        return self._buffer_frames

    @property
    def buffer_bytes(self) -> int:
        """当前发送队列容量对应的字节数（按满队列估算）。"""
        return self._buffer_bytes

    # ================================================================
    # 状态 / 生命周期
    # ================================================================

    @property
    def is_connected(self) -> bool:
        """当前是否有 VOFA+ 客户端连接。

        发送线程在 accept 成功后置为 True；接收线程在 recv 返回 EOF/出错
        时置为 False（因此客户端主动断开能被较快感知），sendall 失败或
        close() 也会置为 False。
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
        self._buffer_frames = size
        self._buffer_bytes = size * self._frame_bytes
        print(
            f"Buffer size changed to {size} frames "
            f"({self._buffer_bytes / 1024:.1f} KiB, "
            f"{len(self._send_queue)} pending)"
        )

    def close(self) -> None:
        """停止服务端并释放资源。幂等。"""
        if self._socket is None and not self._running:
            return

        self._running = False
        self._close_client()

        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

        # 等待线程退出。接收线程最多在 select 上等 0.1s，发送线程最多
        # 睡一个 TCP 周期；留 0.3s 余量，避免 close() 长时间阻塞。
        current = threading.current_thread()
        join_timeout = max(0.3, 2.0 * self._send_interval)
        for thread in (self._thread, self._recv_thread):
            if thread is not None and thread is not current and thread.is_alive():
                thread.join(timeout=join_timeout)

        print("JustFloatServer closed")

    def __enter__(self) -> "JustFloatServer":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ================================================================
    # 发送线程
    # ================================================================

    def _sender_loop(self) -> None:
        """发送线程主循环: 接受连接 + 每个 TCP 周期清空队列合并发送。

        每周期将所有待发帧拼接成一个大字节串，一次 sendall() 发出。
        相比逐帧发送: ① syscall 从 N 次降为 1 次 ② TCP 段利用率更高
        ③ VOFA+ 收到完整批数据，解析效率更高。

        循环逻辑::

            while _running:
                sleep(interval)           ← 默认 10Hz 周期
                if 无客户端 → accept()
                while 队列有数据:
                    popleft → pack → 拼入 packet
                if packet 非空:
                    sendall(packet)       ← 一次发出所有帧
        """
        while self._running:
            time.sleep(self._send_interval)

            # ---- 阶段 1: 接受客户端 ----
            if self._client is None:
                sock = self._socket
                if sock is None:
                    continue
                try:
                    client, addr = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._running:
                        time.sleep(0.1)
                    continue

                with self._client_lock:
                    self._client = client
                self.clear_data()
                with self._recv_lock:
                    self._recv_buffer.clear()
                self._start_receiver(client)
                print(f"Client connected: {addr}")
                # 新连接: 跳过本周期, 等下个周期再发
                # (让 TCP 连接稳定 + 缓冲区积累数据)
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

    # ================================================================
    # 接收线程
    # ================================================================

    def _start_receiver(self, client: socket.socket) -> None:
        """为新连接启动接收线程。"""
        thread = threading.Thread(
            target=self._receiver_loop,
            args=(client,),
            name="VofaPlusReceiver",
            daemon=True,
        )
        self._recv_thread = thread
        thread.start()

    def _receiver_loop(self, client: socket.socket) -> None:
        """接收线程: 持续读取客户端数据，按行解析 ``cmdN:value``。

        用 select 轮询而不是给 socket 设超时：同一个 socket 还要给发送
        线程 sendall 使用，设 timeout 会让 sendall 在慢客户端上抛异常。
        """
        while self._running and self._client is client:
            try:
                readable, _, _ = select.select([client], [], [], 0.1)
            except (OSError, ValueError):
                break

            if not readable:
                continue

            try:
                data = client.recv(4096)
            except OSError:
                break

            if not data:
                break  # 对端关闭

            if self._client is not client:
                break  # 已被发送线程换掉/关闭

            self._feed(client, data)

        self._close_client(client)

    def _feed(self, client: socket.socket, data: bytes) -> None:
        """把收到的字节追加进缓冲区，并按换行拆出完整命令行。

        持锁并再次确认 client 仍是当前连接，避免旧连接的收尾数据在
        新连接清空缓冲区之后又写进来。
        """
        with self._recv_lock:
            if self._client is not client:
                return

            self._recv_buffer.extend(data)

            while True:
                idx = self._recv_buffer.find(b"\n")
                if idx < 0:
                    # 没有换行：半包，等下一次 recv；异常超长行直接丢弃
                    if len(self._recv_buffer) > self.MAX_LINE_BYTES:
                        self._recv_buffer.clear()
                    return

                line = bytes(self._recv_buffer[:idx])
                del self._recv_buffer[: idx + 1]

                if len(line) > self.MAX_LINE_BYTES:
                    continue
                self._parse_line(line)

    def _parse_line(self, line: bytes) -> None:
        """解析单行命令，非法内容静默忽略。"""
        try:
            text = line.strip().decode("ascii")
        except UnicodeDecodeError:
            return

        match = self._CMD_RE.match(text)
        if match is None:
            return

        try:
            value = float(match.group(2))
        except ValueError:
            return

        if not math.isfinite(value):
            return

        key = "cmd" + match.group(1)
        with self._cmd_lock:
            self._cmd_values[key] = value

    @staticmethod
    def _cmd_key(cmd: Union[int, str]) -> str:
        """把 0 / 'cmd0' 统一成 'cmd0'。"""
        if isinstance(cmd, str):
            key = cmd.strip().lower()
            if not re.fullmatch(r"cmd\d+", key):
                raise ValueError(f"cmd 名称必须是 cmdN 格式，收到 {cmd!r}")
            return key
        if not isinstance(cmd, int):
            raise TypeError(f"cmd 必须是 int 或 str，收到 {type(cmd).__name__}")
        if cmd < 0:
            raise ValueError("cmd 编号必须 >= 0")
        return f"cmd{cmd}"

    # ================================================================
    # 客户端连接管理
    # ================================================================

    def _close_client(self, client: Optional[socket.socket] = None) -> None:
        """关闭客户端连接。幂等。

        Args:
            client: 只关闭这个客户端；如果它已经不是当前客户端则忽略。
                接收线程用它在退出时关闭自己对应的连接，避免误关新连接。
        """
        with self._client_lock:
            if client is not None and self._client is not client:
                return
            current = self._client
            self._client = None

        if current is not None:
            try:
                current.close()
            except OSError:
                pass
            print("Client disconnected")


# ================================================================
# 使用示例
# ================================================================
if __name__ == "__main__":
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

                # 打印 VOFA+ 滑块发来的 cmd0（未收到时为 None）
                # print("cmd0 =", server.get(0))

                t += 0.001
                time.sleep(0.001)
        except KeyboardInterrupt:
            pass
