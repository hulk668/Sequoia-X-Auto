"""GitHub Actions 工作流文件的静态校验。

**为什么需要这个**：YAML 合法 ≠ GitHub 能接受。

GitHub 在 YAML 之上还会再跑一遍自己的**表达式解析器**，它的报错只在
推送到远端、点 "Run workflow" 的那一刻才弹出来：

    Invalid workflow file
    (Line: 128, Col: 14): An expression was expected

本地用 `yaml.safe_load()` 校验是**看不出来的** —— 文件是合法 YAML，只是 GitHub 不认。

踩过的坑（`seed.yml` 曾因此整个文件非法、工作流完全跑不起来）：
在 `run:` 的 shell 注释里写了 Actions 表达式的定界符，且中间是空的。
GitHub 扫描的是**整段 run 文本，连注释一起解析**，解析到空表达式直接判文件非法。

所以这里只校验「会不会让 GitHub 拒绝」的硬性规则：
定界符配对、表达式非空、起始 token 是已知上下文。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# 与 Actions 表达式定界符拼接，避免本文件的字面量被误认为需要求值的表达式。
_OPEN = "$" + "{{"
_CLOSE = "}" + "}"

_EXPR = re.compile(re.escape(_OPEN) + r"(.*?)" + re.escape(_CLOSE), re.S)

# GitHub 表达式合法的起始 token：上下文引用，或字面量 / 函数调用 / 逻辑非
_VALID_START = re.compile(
    r"^(?:"
    r"github|env|vars|job|jobs|steps|runner|secrets|strategy|matrix|needs|inputs"
    r")\."
    r"|^!"
    r"|^'"
    r'|^"'
    r"|^-?\d"
    r"|^(?:true|false|null)$"
    r"|^(?:contains|startsWith|endsWith|format|join|toJSON|fromJSON"
    r"|hashFiles|success|always|cancelled|failure)\("
)


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert files, f"{WORKFLOW_DIR} 下没有找到任何工作流文件"
    return files


def _iter_with_line(text: str):
    """产出 (行号, 表达式原文)。"""
    for m in _EXPR.finditer(text):
        yield text[: m.start()].count("\n") + 1, m.group(1)


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_expression_delimiters_are_balanced(path: Path) -> None:
    """每个开定界符都必须有闭合的定界符。

    漏一个 `}}` 的话 GitHub 会一路吞到文件末尾再报错，很难定位。
    """
    text = path.read_text(encoding="utf-8")
    opened = text.count(_OPEN)
    closed = text.count(_CLOSE)
    assert opened == closed, (
        f"{path.name}: 表达式定界符不配对 —— 开了 {opened} 个，只闭合了 {closed} 个"
    )
    assert opened == len(list(_iter_with_line(text))), (
        f"{path.name}: 有无法解析的表达式（可能存在嵌套或跨行异常）"
    )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_no_empty_expressions(path: Path) -> None:
    """空的表达式定界符对会让整个 workflow 文件非法。

    这正是 seed.yml 曾经的故障：写在 shell 注释里，GitHub 照样解析。
    """
    text = path.read_text(encoding="utf-8")
    offenders = [
        line for line, inner in _iter_with_line(text) if not inner.strip()
    ]
    assert not offenders, (
        f"{path.name}: 第 {offenders} 行出现空表达式。"
        "GitHub 会扫描整段 run 文本（含注释），空表达式会让文件直接非法。"
    )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_expression_starts_with_known_context(path: Path) -> None:
    """表达式必须以已知上下文或字面量/函数开头，否则 GitHub 解析失败。"""
    text = path.read_text(encoding="utf-8")
    bad = [
        (line, inner)
        for line, inner in _iter_with_line(text)
        if inner.strip() and not _VALID_START.match(inner.strip())
    ]
    assert not bad, f"{path.name}: 非法表达式 {bad}"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_has_trigger_and_jobs(path: Path) -> None:
    """基本结构检查：必须有顶层 on: 与 jobs:。"""
    text = path.read_text(encoding="utf-8")
    assert re.search(r"(?m)^on:", text), f"{path.name}: 缺少顶层 on: 触发器"
    assert re.search(r"(?m)^jobs:", text), f"{path.name}: 缺少顶层 jobs:"
