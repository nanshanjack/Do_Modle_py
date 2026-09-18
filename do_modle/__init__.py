"""do_modle —— A股量化交易系统（阶段一：601872.SH 招商轮船）。

分层（依赖方向单向，禁止反向 import）::

    objects / numeric          <- 共享数据契约与数值工具
        ^
        |
    rules                      <- A股规则层（零外部依赖，最高优先级）
        ^
        |
    data -> core -> strategy -> execution / evaluation -> app

架构约束由 ``tests/test_architecture.py`` 自动断言。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
