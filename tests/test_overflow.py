from __future__ import annotations

import warnings

import pytest

from vofa_plus import ChannelOverflowWarning, FLOAT32_MAX, JustFloatServer


def test_overflow_warns_once_per_channel() -> None:
    with JustFloatServer(channels=2, port=0) as server:
        with pytest.warns(ChannelOverflowWarning, match="float32"):
            server.set(0, FLOAT32_MAX * 10.0)

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            server.set(0, FLOAT32_MAX * 10.0)  # 同一通道不再提醒
            server.set(1, 1.0)                # 正常值不提醒


def test_nonfinite_value_warns() -> None:
    with JustFloatServer(channels=1, port=0) as server:
        with pytest.warns(ChannelOverflowWarning, match="非有限"):
            server.set(0, float("nan"))


def test_normal_values_do_not_warn() -> None:
    with JustFloatServer(channels=2, port=0) as server:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for value in (0.0, -1.0, 1e-30, FLOAT32_MAX):
                server.set(0, value)
                server.send()
