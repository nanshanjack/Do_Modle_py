"""``scripts/strategy_backtest.py`` 的信号规则测试。

**只测纯函数**（``rolling_targets`` / ``shifted_targets``）—— 它们是"因子 → 目标仓位"
这一步的全部逻辑，也是唯一可能引入**前视**或**结构被破坏**的地方。
回测引擎本身已有独立测试，不在此重复。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.strategy_backtest import rolling_targets, shifted_targets


def _series(values: list[float], start: date = date(2020, 1, 1)) -> list[tuple[date, float]]:
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


class TestRollingTargets:
    def test_cold_start_emits_nothing(self) -> None:
        """观测数不足 min_obs → 不产生任何目标仓位（冷启动不交易）。"""
        s = _series([1.0, 2.0, 3.0])
        assert rolling_targets(s, 1, window=2, min_obs=5) == {}

    def test_starts_exactly_at_min_obs(self) -> None:
        s = _series([1.0, 2.0, 3.0])
        out = rolling_targets(s, 1, window=2, min_obs=3)
        assert len(out) == 1
        assert list(out) == [s[2][0]]

    def test_sign_flips_target(self) -> None:
        """同样的因子序列，方向取反 → 目标仓位取反。"""
        s = _series([1.0, 2.0, 3.0, 10.0])
        up = rolling_targets(s, 1, window=3, min_obs=2)
        down = rolling_targets(s, -1, window=3, min_obs=2)
        assert set(up) == set(down)
        for d in up:
            assert up[d] != down[d]

    def test_uses_only_past_observations(self) -> None:
        """**PIT 关键**：改掉未来的因子值，历史目标仓位必须一字不变。"""
        base = _series([1.0, 2.0, 3.0, 4.0, 5.0])
        tampered = base[:3] + [(base[3][0], 999.0), (base[4][0], 999.0)]
        a = rolling_targets(base, 1, window=3, min_obs=2)
        b = rolling_targets(tampered, 1, window=3, min_obs=2)
        for d in list(a)[:2]:
            assert a[d] == b[d]

    def test_target_is_binary(self) -> None:
        s = _series([float(i % 7) for i in range(40)])
        out = rolling_targets(s, 1, window=10, min_obs=5)
        assert set(out.values()) <= {0, 1}


class TestShiftedTargets:
    def test_zero_shift_is_identity(self) -> None:
        s = _series([float(i) for i in range(30)])
        base = rolling_targets(s, 1, window=5, min_obs=3)
        assert shifted_targets(s, 1, 5, 3, 0) == base

    def test_shift_preserves_in_market_ratio(self) -> None:
        """循环移位是**保结构**的：在场天数、日期集合完全不变。"""
        s = _series([float((i * 7) % 11) for i in range(60)])
        base = rolling_targets(s, 1, window=8, min_obs=4)
        shifted = shifted_targets(s, 1, 8, 4, 13)
        assert set(base) == set(shifted)
        assert sum(base.values()) == sum(shifted.values())

    def test_shift_is_not_identity(self) -> None:
        """必须真的改变对齐 —— 否则安慰剂检验是空转。"""
        s = _series([float((i * 7) % 11) for i in range(60)])
        base = rolling_targets(s, 1, window=8, min_obs=4)
        shifted = shifted_targets(s, 1, 8, 4, 13)
        assert base != shifted

    def test_shift_larger_than_length_wraps(self) -> None:
        s = _series([float((i * 7) % 11) for i in range(60)])
        base = rolling_targets(s, 1, window=8, min_obs=4)
        n = len(base)
        assert shifted_targets(s, 1, 8, 4, n) == base
        assert shifted_targets(s, 1, 8, 4, n + 5) == shifted_targets(s, 1, 8, 4, 5)

    def test_empty_when_no_targets(self) -> None:
        s = _series([1.0, 2.0])
        assert shifted_targets(s, 1, 2, 10, 3) == {}
