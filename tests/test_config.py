"""``do_modle/config.py`` 单测 —— 含用户费率前提的回归守卫。"""

from __future__ import annotations

from pathlib import Path

import pytest

from do_modle.config import (
    AppConfig,
    CostSettings,
    deep_merge,
    load_config,
    load_yaml,
)
from do_modle.rules.cost import AShareCostModel, CostConfig


def test_load_config_returns_app_config() -> None:
    cfg = load_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.stock.symbol == "sh.601872"
    assert cfg.stock.name == "招商轮船"
    assert cfg.stock.board == "MAIN"


def test_cost_settings_match_user_actual_rates() -> None:
    """回归守卫：配置文件里的费率必须等于用户真实费率（万分之一 + 免五）。"""
    cfg = load_config()
    assert cfg.cost.commission_rate == pytest.approx(0.0001)
    assert cfg.cost.min_commission == pytest.approx(0.0)


def test_config_cost_can_build_rules_cost_config() -> None:
    """配置 → 规则层的 CostConfig → 成本模型，必须能直接串起来。"""
    cfg = load_config()
    cost = CostConfig.from_mapping(
        {
            "commission_rate": cfg.cost.commission_rate,
            "min_commission": cfg.cost.min_commission,
            "stamp_tax_rate": cfg.cost.stamp_tax_rate,
            "transfer_rate": cfg.cost.transfer_rate,
            "slippage": cfg.cost.slippage,
        }
    )
    assert AShareCostModel(cost).round_trip_rate(200_000) == pytest.approx(0.00072)


def test_matching_execution_price_is_next_open() -> None:
    """成交价基准必须是次日开盘——改成 same_close 会引入未来函数。"""
    assert load_config().matching.execution_price == "next_open"


def test_backtest_defaults() -> None:
    bt = load_config().backtest
    assert bt.initial_cash == pytest.approx(200_000.0)
    assert bt.in_sample_end == "2021-12-31"
    assert len(bt.out_of_sample_segments) == 2


def test_out_of_sample_segments_are_tuples() -> None:
    segs = load_config().backtest.out_of_sample_segments
    assert all(isinstance(s, tuple) and len(s) == 2 for s in segs)
    assert segs[0] == ("2022-01-01", "2023-12-31")


def test_price_for_factor_is_hfq() -> None:
    """因子必须用后复权（前复权会重算历史价格 → 前视）。"""
    cfg = load_config()
    assert cfg.data.price_for_factor == "hfq"
    assert cfg.data.price_for_account == "none"


def test_defaults_used_when_file_missing(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.yaml", tmp_path / "nope2.yaml")
    assert isinstance(cfg, AppConfig)
    assert cfg.cost.commission_rate == pytest.approx(CostSettings().commission_rate)


def test_load_yaml_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_yaml(tmp_path / "absent.yaml") == {}


def test_load_yaml_rejects_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_yaml(p)


def test_deep_merge_recursive() -> None:
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    over = {"a": {"y": 20, "z": 30}}
    assert deep_merge(base, over) == {"a": {"x": 1, "y": 20, "z": 30}, "b": 3}


def test_deep_merge_does_not_mutate_inputs() -> None:
    base = {"a": {"x": 1}}
    over = {"a": {"x": 2}}
    deep_merge(base, over)
    assert base == {"a": {"x": 1}}


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    p = tmp_path / "b.yaml"
    p.write_text("cost:\n  commission_rate: 0.0003\n  future_key: 42\n", encoding="utf-8")
    cfg = load_config(p, tmp_path / "absent.yaml")
    assert cfg.cost.commission_rate == pytest.approx(0.0003)


def test_symbol_file_overrides_base(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("backtest:\n  initial_cash: 100000\n", encoding="utf-8")
    sym = tmp_path / "sym.yaml"
    sym.write_text("backtest:\n  initial_cash: 300000\n", encoding="utf-8")
    assert load_config(base, sym).backtest.initial_cash == pytest.approx(300_000.0)


def test_pit_financial_lag_defined() -> None:
    pit = load_config().pit
    assert pit.financial_report_lag_days["annual"] >= 120
    assert pit.daily_bar == "15:00"
