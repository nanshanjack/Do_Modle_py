"""配置加载：YAML → 冻结 dataclass。

本模块**不依赖 ``do_modle`` 内任何其它模块**（只依赖标准库与 pyyaml），
因此可以被任何层安全引用，不会造成循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

__all__ = [
    "CostSettings",
    "MatchingSettings",
    "PITSettings",
    "RiskSettings",
    "DataSettings",
    "BacktestSettings",
    "SymbolSettings",
    "AppConfig",
    "load_yaml",
    "deep_merge",
    "load_config",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass(frozen=True)
class CostSettings:
    """交易成本。默认值 = 用户实际费率（万一 + 免五）。"""

    commission_rate: float = 0.0001
    min_commission: float = 0.0
    stamp_tax_rate: float = 0.0005
    transfer_rate: float = 0.00001
    slippage: float = 0.0005


@dataclass(frozen=True)
class MatchingSettings:
    execution_price: str = "next_open"
    volume_limit_pct: float = 0.10
    partial_fill_policy: str = "cancel"
    insufficient_cash_policy: str = "shrink"


@dataclass(frozen=True)
class PITSettings:
    daily_bar: str = "15:00"
    financial_report_lag_days: Mapping[str, int] = field(
        default_factory=lambda: {"q1": 30, "q3": 30, "h1": 60, "annual": 120}
    )
    express_report_lag_days: int = 1
    shipping_index_lag_days: int = 2


@dataclass(frozen=True)
class RiskSettings:
    max_order_value_pct: float = 1.0
    max_position_pct: float = 1.0
    daily_turnover_limit_pct: float = 0.30
    consecutive_loss_circuit: int = 5
    min_order_value_warning: float = 20_000.0


@dataclass(frozen=True)
class DataSettings:
    provider: str = "baostock"
    store: str = "./data_store"
    start: str = "2015-01-01"
    price_for_factor: str = "hfq"
    price_for_account: str = "none"


@dataclass(frozen=True)
class BacktestSettings:
    start: str = "2016-01-01"
    end: str = "2026-09-01"
    initial_cash: float = 200_000.0
    in_sample_end: str = "2021-12-31"
    out_of_sample_segments: tuple[tuple[str, str], ...] = (
        ("2022-01-01", "2023-12-31"),
        ("2024-01-01", "2026-09-01"),
    )


@dataclass(frozen=True)
class SymbolSettings:
    symbol: str = "sh.601872"
    name: str = "招商轮船"
    board: str = "MAIN"
    benchmark: str = "self_buy_and_hold"


@dataclass(frozen=True)
class AppConfig:
    data: DataSettings = field(default_factory=DataSettings)
    cost: CostSettings = field(default_factory=CostSettings)
    matching: MatchingSettings = field(default_factory=MatchingSettings)
    pit: PITSettings = field(default_factory=PITSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    backtest: BacktestSettings = field(default_factory=BacktestSettings)
    stock: SymbolSettings = field(default_factory=SymbolSettings)


def load_yaml(path: str | Path) -> dict[str, Any]:
    """读取 YAML 文件；文件不存在时返回空 dict。"""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML 根节点必须是映射: {p}")
    return data


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并两个映射，``override`` 优先。"""
    out: dict[str, Any] = dict(base)
    for key, val in override.items():
        if key in out and isinstance(out[key], Mapping) and isinstance(val, Mapping):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _build(cls: type, raw: Mapping[str, Any] | None):
    """用映射构造冻结 dataclass，忽略未知键（前向兼容）。"""
    raw = raw or {}
    names = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in raw.items() if k in names}
    if cls is BacktestSettings and "out_of_sample_segments" in kwargs:
        kwargs["out_of_sample_segments"] = tuple(
            tuple(seg) for seg in kwargs["out_of_sample_segments"]
        )
    return cls(**kwargs)


def load_config(
    base_path: str | Path | None = None,
    symbol_path: str | Path | None = None,
) -> AppConfig:
    """加载并合并配置。

    ``config/base.yaml`` 提供全局参数，``config/stock_*.yaml`` 提供标的覆盖。
    """
    base_raw = load_yaml(base_path or DEFAULT_CONFIG_DIR / "base.yaml")
    sym_raw = load_yaml(symbol_path or DEFAULT_CONFIG_DIR / "stock_601872.yaml")
    merged = deep_merge(base_raw, sym_raw)
    return AppConfig(
        data=_build(DataSettings, merged.get("data")),
        cost=_build(CostSettings, merged.get("cost")),
        matching=_build(MatchingSettings, merged.get("matching")),
        pit=_build(PITSettings, merged.get("pit")),
        risk=_build(RiskSettings, merged.get("risk")),
        backtest=_build(BacktestSettings, merged.get("backtest")),
        stock=_build(SymbolSettings, merged.get("stock")),
    )
