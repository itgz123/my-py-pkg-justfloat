"""发送缓冲区自动估算测试。"""

from __future__ import annotations

import pytest

from vofa_plus import JustFloatServer


def test_auto_buffer_for_1khz_send_and_10hz_tcp() -> None:
    with JustFloatServer(channels=8, port=0) as server:
        assert server.frame_bytes == 4 * 8 + 4
        assert server.frames_per_cycle == 100       # 1000 / 10
        assert server.buffer_frames == 1000         # 1 秒数据
        assert server.buffer_bytes == 1000 * 36
        assert server.send_interval == pytest.approx(0.1)


def test_channel_count_limits_buffer_by_memory_cap() -> None:
    with JustFloatServer(channels=1000, port=0) as server:
        # 4004 B/帧，1 MiB 软上限 -> 261 帧；仍大于 2 个 TCP 周期
        assert server.frame_bytes == 4004
        assert server.buffer_frames == 261
        assert server.buffer_bytes <= 1 << 20


def test_custom_rates_and_buffer_seconds() -> None:
    with JustFloatServer(
        channels=8,
        port=0,
        send_rate_hz=500.0,
        tcp_rate_hz=20.0,
        buffer_seconds=0.5,
    ) as server:
        assert server.frames_per_cycle == 25        # 500 / 20
        assert server.send_interval == pytest.approx(0.05)
        # 0.5 秒 * 500Hz = 250 帧，但 MAX_BUF=256 是兜底下限
        assert server.buffer_frames == 256


def test_set_buffer_size_updates_properties() -> None:
    with JustFloatServer(channels=4, port=0) as server:
        server.set_buffer_size(42)
        assert server.buffer_frames == 42
        assert server.buffer_bytes == 42 * server.frame_bytes


def test_invalid_buffer_parameters_rejected() -> None:
    with pytest.raises(ValueError):
        JustFloatServer(channels=0, port=0)
    with pytest.raises(ValueError):
        JustFloatServer(channels=4, port=0, send_rate_hz=0.0)
    with pytest.raises(ValueError):
        JustFloatServer(channels=4, port=0, tcp_rate_hz=0.0)
    with pytest.raises(ValueError):
        JustFloatServer(channels=4, port=0, buffer_seconds=0.0)
    with pytest.raises(ValueError):
        JustFloatServer(channels=4, port=0, max_buffer_bytes=0)
