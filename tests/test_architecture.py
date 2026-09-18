"""架构约束自动断言 —— 架构规则必须可执行，否则三个月后必然被违反。

四条 import 禁令：

1. ``rules`` 不得 import ``data``（只允许 ``objects`` / ``numeric`` / 自身）
2. ``rules`` / ``core`` 不得 import ``strategy``
3. ``strategy`` 不得 import ``evaluation``（归因 ≠ 预测）
4. 任何模块不得 import ``app``

附加约束：``rules`` 层必须零第三方依赖。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "do_modle"

# 每个 do_modle 内部包允许依赖的内部模块白名单
ALLOWED_INTERNAL: dict[str, set[str]] = {
    "do_modle": set(),
    "do_modle.objects": set(),
    "do_modle.numeric": set(),
    "do_modle.config": set(),
    "do_modle.logging_setup": set(),
    "do_modle.rules": {"do_modle.objects", "do_modle.numeric"},
    "do_modle.data": {"do_modle.objects", "do_modle.numeric", "do_modle.config"},
    "do_modle.core": {
        "do_modle.objects",
        "do_modle.numeric",
        "do_modle.rules",
        "do_modle.data",
        "do_modle.config",
    },
    "do_modle.strategy": {
        "do_modle.objects",
        "do_modle.numeric",
        "do_modle.rules",
        "do_modle.data",
        "do_modle.core",
        "do_modle.config",
    },
    "do_modle.execution": {
        "do_modle.objects",
        "do_modle.numeric",
        "do_modle.rules",
        "do_modle.data",
        "do_modle.core",
        "do_modle.config",
    },
    "do_modle.evaluation": {
        "do_modle.objects",
        "do_modle.numeric",
        "do_modle.rules",
        "do_modle.data",
        "do_modle.core",
        "do_modle.config",
    },
    "do_modle.app": {
        "do_modle.objects",
        "do_modle.numeric",
        "do_modle.rules",
        "do_modle.data",
        "do_modle.core",
        "do_modle.strategy",
        "do_modle.execution",
        "do_modle.evaluation",
        "do_modle.config",
        "do_modle.logging_setup",
    },
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(path: Path) -> str:
    name = _module_name(path)
    if path.name == "__init__.py":
        return name
    return name.rsplit(".", 1)[0] if "." in name else name


def _resolve_relative(pkg: str, level: int, module: str | None) -> str:
    parts = pkg.split(".")
    if level > 1:
        parts = parts[: max(0, len(parts) - (level - 1))]
    prefix = ".".join(parts)
    return f"{prefix}.{module}" if module else prefix


def _imported_modules(path: Path) -> set[str]:
    """收集一个模块 import 的全部模块名（绝对化）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    pkg = _package_of(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.add(_resolve_relative(pkg, node.level, node.module))
            elif node.module:
                found.add(node.module)
    return found


def _all_modules() -> list[Path]:
    return sorted(p for p in PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _layer_of(module_name: str) -> str:
    """找到模块所属的层级前缀（最长匹配）。"""
    candidates = [k for k in ALLOWED_INTERNAL if module_name == k or module_name.startswith(k + ".")]
    assert candidates, f"未登记的模块: {module_name}"
    return max(candidates, key=len)


def test_package_tree_is_not_empty() -> None:
    modules = _all_modules()
    assert modules, "未找到任何模块"
    names = {_module_name(p) for p in modules}
    assert "do_modle.rules.cost" in names
    assert "do_modle.rules.position" in names


def test_no_upward_or_forbidden_imports() -> None:
    """核心断言：任何模块只能依赖白名单内的内部模块。"""
    violations: list[str] = []
    for path in _all_modules():
        mod = _module_name(path)
        layer = _layer_of(mod)
        allowed = ALLOWED_INTERNAL[layer]
        for imp in _imported_modules(path):
            if not (imp == "do_modle" or imp.startswith("do_modle.")):
                continue
            target = _layer_of(imp)
            if target == layer or target in allowed:
                continue
            violations.append(f"{mod} -> {imp}")
    assert not violations, "发现越层依赖:\n" + "\n".join(violations)


def test_rules_layer_does_not_import_data() -> None:
    """禁令 1：rules 不得 import data。"""
    for path in _all_modules():
        mod = _module_name(path)
        if not mod.startswith("do_modle.rules"):
            continue
        bad = {i for i in _imported_modules(path) if i.startswith("do_modle.data")}
        assert not bad, f"{mod} 违规依赖 data: {bad}"


def test_rules_layer_has_zero_third_party_deps() -> None:
    """rules 层零第三方依赖（只允许标准库 + do_modle 自身）。"""
    stdlib = set(sys.stdlib_module_names)
    for path in _all_modules():
        mod = _module_name(path)
        if not mod.startswith("do_modle.rules"):
            continue
        for imp in _imported_modules(path):
            top = imp.split(".")[0]
            assert top in stdlib or top == "do_modle", (
                f"{mod} 引入了第三方依赖 '{imp}'；rules 层必须零外部依赖"
            )


def test_strategy_does_not_import_evaluation() -> None:
    """禁令 3：归因 ≠ 预测，strategy 不得 import evaluation。"""
    for path in _all_modules():
        mod = _module_name(path)
        if not mod.startswith("do_modle.strategy"):
            continue
        bad = {i for i in _imported_modules(path) if i.startswith("do_modle.evaluation")}
        assert not bad, f"{mod} 违规依赖 evaluation: {bad}"


def test_only_app_may_import_app() -> None:
    """禁令 4：应用层是叶子，任何模块不得 import app。"""
    for path in _all_modules():
        mod = _module_name(path)
        if mod.startswith("do_modle.app"):
            continue
        bad = {i for i in _imported_modules(path) if i.startswith("do_modle.app")}
        assert not bad, f"{mod} 违规依赖 app: {bad}"


def test_all_shared_objects_defined_in_objects_module() -> None:
    """共享数据契约必须集中在 do_modle.objects，禁止在 rules 里另立一套。"""
    forbidden = {"Bar", "Instrument", "Trade", "OrderRequest", "Signal", "AccountSnapshot"}
    for path in _all_modules():
        mod = _module_name(path)
        if not mod.startswith("do_modle.rules"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        }
        clash = defined & forbidden
        assert not clash, f"{mod} 重复定义了共享契约: {clash}"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
