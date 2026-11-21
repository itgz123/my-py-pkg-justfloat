"""命令接收功能集成测试：用真实 TCP 连接模拟 VOFA+ 控件发来的 cmdN。"""

from __future__ import annotations

import socket
import struct
import time
from typing import Callable

import pytest

from vofa_plus import JustFloatServer


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    """轮询等待条件成立，避免测试依赖固定 sleep。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_receives_cmd_values() -> None:
    with JustFloatServer(channels=1, port=0) as server:
        assert server.port > 0
        assert server.get(0) is None
        assert server.get(0, 0.0) == 0.0
        assert not server.has_data

        with socket.create_connection(("127.0.0.1", server.port), timeout=3) as client:
            assert wait_until(lambda: server.is_connected)

            client.sendall(b"cmd0:1.23456\ncmd1:-2.5\ncmd2:1e-3\n")
            assert wait_until(lambda: server.get("cmd2") == 1e-3)

            assert server.get(0) == pytest.approx(1.23456)
            assert server.get("cmd1") == -2.5
            data = server.get_data()
            assert set(data) == {"cmd0", "cmd1", "cmd2"}
            assert data["cmd0"] == pytest.approx(1.23456)
            assert data["cmd1"] == -2.5
            assert data["cmd2"] == 1e-3
            assert server.has_data


def test_partial_line_is_buffered() -> None:
    with JustFloatServer(channels=1, port=0) as server:
        with socket.create_connection(("127.0.0.1", server.port), timeout=3) as client:
            assert wait_until(lambda: server.is_connected)

            client.sendall(b"cmd0:1.2")
            time.sleep(0.05)
            assert server.get(0) is None  # 没有换行，不能当成完整命令

            client.sendall(b"345\n")
            assert wait_until(lambda: server.get(0) is not None)
            assert server.get(0) == pytest.approx(1.2345)


def test_invalid_lines_are_ignored() -> None:
    with JustFloatServer(channels=1, port=0) as server:
        with socket.create_connection(("127.0.0.1", server.port), timeout=3) as client:
            assert wait_until(lambda: server.is_connected)

            client.sendall(
                b"garbage\n"
                b"cmd0:abc\n"
                b"cmd1:3.5\r\n"
                b"\n"
                b"cmd2:1e-3\n"
                b"cmd3:nan\n"
                b"cmd4:inf\n"
            )
            assert wait_until(lambda: server.get(1) == 3.5 and server.get(2) == 1e-3)
            time.sleep(0.05)
            assert set(server.get_data()) == {"cmd1", "cmd2"}


def test_data_cleared_on_new_connection() -> None:
    with JustFloatServer(channels=1, port=0) as server:
        first = socket.create_connection(("127.0.0.1", server.port), timeout=3)
        try:
            assert wait_until(lambda: server.is_connected)
            first.sendall(b"cmd0:9.0\n")
            assert wait_until(lambda: server.get(0) == 9.0)
        finally:
            first.close()

        assert wait_until(lambda: not server.is_connected)
        # 断开本身不清数据；新连接 accept 时才清空，避免重连读到旧滑块值
        assert server.get(0) == 9.0

        with socket.create_connection(("127.0.0.1", server.port), timeout=3) as second:
            assert wait_until(lambda: server.is_connected)
            assert not server.has_data
            assert second is not None


def test_get_rejects_invalid_cmd_names() -> None:
    with JustFloatServer(channels=1, port=0) as server:
        with pytest.raises(ValueError):
            server.get("ch0")
        with pytest.raises(ValueError):
            server.get(-1)


def test_justfloat_sending_still_works() -> None:
    with JustFloatServer(channels=1, port=0) as server:
        with socket.create_connection(("127.0.0.1", server.port), timeout=3) as client:
            assert wait_until(lambda: server.is_connected)

            # 服务端跳过 accept 的那个周期，之后每 0.1s 发送一次
            server.set(0, 1.0)
            server.send()

            expected = struct.pack("<1f", 1.0) + JustFloatServer.TAIL
            received = b""
            client.settimeout(3)
            while len(received) < len(expected):
                received += client.recv(len(expected) - len(received))

            assert received == expected
