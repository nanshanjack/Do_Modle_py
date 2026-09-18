"""数据层——数据源抽象、PIT 时点守卫、本地缓存、适配器。

模块划分::

    provider.py         DataProvider 抽象 + PROVIDERS 注册表
    pit.py              PITGuard   时点守卫（防前视的核心）
    cache.py            DataCache  SQLite 本地缓存（幂等 upsert）
    adapters/           具体数据源适配器（baostock / local dump）

**价格口径三分（严禁混用）**：

* 因子计算 → ``AdjustFlag.HFQ``（后复权，历史不变，无前视）
* 成交/涨跌停/账户市值 → ``AdjustFlag.NONE``（不复权 + ``prev_close``）
* 展示 → ``QFQ``（禁止进入任何计算）
"""

from __future__ import annotations

from do_modle.data.cache import DataCache
from do_modle.data.pit import PITGuard
from do_modle.data.provider import PROVIDERS, DataProvider, create_provider, register

__all__ = [
    "PROVIDERS",
    "DataCache",
    "DataProvider",
    "PITGuard",
    "create_provider",
    "register",
]
