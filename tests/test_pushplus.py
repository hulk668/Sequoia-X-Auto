"""PushPlus 推送渲染测试（纯字符串拼装，不发网络请求）。"""

import re
import unicodedata
from pathlib import Path

import pytest

from sequoia_x.notify.pushplus import (
    PushPlusNotifier,
    _display_width,
    _pad_to,
)


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """不吃 config.local.toml，避免本地真实 token 影响测试。"""
    import sequoia_x.core.config as cfg_module

    monkeypatch.setattr(cfg_module, "LOCAL_CONFIG_FILE", Path("__no_such_config__.toml"))


def _notifier() -> PushPlusNotifier:
    from sequoia_x.core.config import Settings

    return PushPlusNotifier(Settings(pushplus_token="test-token"))


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _head_width(line: str) -> int:
    """列表项里「板块名 + 填充空格」的显示宽度（个股起始列）。"""
    return _width(line[2 : line.find("[")].replace("**", ""))


# 用真实口径的数据：板块来自东财 EM2016 行业分类第二级
NAMES = {
    "601975": "招商南油",
    "601326": "秦港股份",
    "601298": "青岛港",
    "600900": "长江电力",
    "600886": "国投电力",
    "603200": "上海洗霸",
    "600350": "山东高速",
    "600085": "同仁堂",
}
# 港口航运 3 只、电力 2 只 → 各占一行；其余 3 只各成一块 → 合并成两行
BOARDS = {
    "601975": "港口航运",
    "601326": "港口航运",
    "601298": "港口航运",
    "600900": "电力",
    "600886": "电力",
    "603200": "环保",
    "600350": "公路铁路",
    "600085": "中药生产",
}


# ── 工具函数 ──


def test_display_width_counts_wide_chars_as_two() -> None:
    """半角单位：一个汉字 = 2，一个半角字符 = 1。"""
    assert _display_width("港口航运") == 8
    assert _display_width("电力") == 4
    assert _display_width("REITs") == 5
    assert _display_width("CART细胞疗法") == 12  # CART(4) + 细胞疗法(8)


def test_pad_to_is_exact_width() -> None:
    """填充串宽度必须精确，否则列对不齐。"""
    for target in range(0, 17):
        assert _display_width(_pad_to(target)) == target


def test_xueqiu_code_prefix() -> None:
    """6→SH，4/8→BJ，其余→SZ。"""
    n = _notifier()
    assert n._to_xueqiu_code("600519") == "SH600519"
    assert n._to_xueqiu_code("000001") == "SZ000001"
    assert n._to_xueqiu_code("300750") == "SZ300750"
    assert n._to_xueqiu_code("830799") == "BJ830799"


# ── 按行业板块排版（方案 B）──


def test_multi_stock_boards_each_own_a_line() -> None:
    """只数 ≥2 的板块各占一个列表项：板块名加粗、个股从同一列开始。"""
    n = _notifier()
    rows = n._format_boards(list(NAMES), NAMES, BOARDS).splitlines()

    assert rows[0].startswith("- **港口航运**")
    assert rows[1].startswith("- **电力**")
    # 只有两个板块是多只，其余必须被合并 —— 绝不能出现「某板块独占一行」
    assert sum(1 for r in rows if r.startswith("- **")) == 2


def test_single_stock_boards_are_packed_with_board_suffix() -> None:
    """只有 1 只的板块压成若干行，写成 `名称（板块）`，板块信息不丢。"""
    n = _notifier()
    rows = n._format_boards(list(NAMES), NAMES, BOARDS).splitlines()
    packed = rows[2:]

    assert len(packed) == 2  # 3 个单只板块 ÷ 每行 2 只
    assert all(r.startswith("- ") and not r.startswith("- **") for r in packed)
    for name, board in (("同仁堂", "中药生产"), ("山东高速", "公路铁路"), ("上海洗霸", "环保")):
        assert f"[{name}](https://xueqiu.com/S/" in "".join(packed)
        assert f"（{board}）" in "".join(packed)


def test_board_is_bold_and_stocks_use_enumeration_comma() -> None:
    """板块名加粗；同一板块内个股用「、」隔开，而不是 ` · `。"""
    n = _notifier()
    text = n._format_boards(list(NAMES), NAMES, BOARDS)

    assert "**港口航运**" in text
    assert "[招商南油](https://xueqiu.com/S/SH601975)、[秦港股份]" in text
    assert " · " not in text


def test_stock_column_is_aligned() -> None:
    """多只板块的各行，个股起始列必须一致，竖着扫一眼就是一张表。"""
    n = _notifier()
    text = n._format_boards(list(NAMES), NAMES, BOARDS)

    cols = {
        _head_width(line)
        for line in text.splitlines()
        if line.startswith("- **") and "[" in line
    }
    assert len(cols) == 1, f"个股起始列不统一：{cols}"


def test_boards_sorted_by_size_and_no_name_is_split() -> None:
    """板块按只数降序；每只股票的名称必须完整出现在同一行内。"""
    n = _notifier()
    rows = n._format_boards(list(NAMES), NAMES, BOARDS).splitlines()

    assert rows[0][2:].startswith("**港口航运**")
    assert rows[1][2:].startswith("**电力**")
    for name in NAMES.values():
        assert any(f"[{name}]" in r for r in rows), f"{name} 被劈到了多行"


def test_overlong_board_name_keeps_a_separator() -> None:
    """板块名超长（超过列宽上限）时，板块名与个股之间仍要有分隔空格。"""
    n = _notifier()
    long_boards = {c: "特别长的行业板块名称测试" for c in NAMES}

    for row in n._format_boards(list(NAMES), NAMES, long_boards).splitlines():
        if not row.startswith("- **"):
            continue
        prefix = row[2 : row.find("[")].replace("**", "")
        assert prefix.endswith(("\u3000", "\u00a0")), repr(row)


def test_stock_without_board_goes_last_without_suffix() -> None:
    """查不到行业的股票排在最后，且不带「（板块）」尾巴。"""
    n = _notifier()
    boards = {k: v for k, v in BOARDS.items() if k not in ("603200", "600085")}

    rows = n._format_boards(list(NAMES), NAMES, boards).splitlines()
    tail = rows[-1]
    assert "[同仁堂]" in tail or "[上海洗霸]" in tail
    assert "（中药生产）" not in tail
    assert "（环保）" not in tail


# ── 兜底排版 ──


def test_plain_layout_when_no_board_at_all() -> None:
    """一只都没查到行业时退回纯列表，每行多放几只、不带任何板块后缀。"""
    n = _notifier()
    rows = n._format_boards(list(NAMES), NAMES, {}).splitlines()

    assert all(r.startswith("- ") for r in rows)
    assert "（" not in "\n".join(rows)
    assert len(rows) == 2  # 8 只 ÷ 每项 5 只
    assert rows[0].count("](http") == 5


def test_plain_layout_falls_back_to_code() -> None:
    """名称缺失时退回带前缀的代码，不能出现空链接文本。"""
    n = _notifier()
    assert (
        n._format_boards(["600519"], {}, {})
        == "- [SH600519](https://xueqiu.com/S/SH600519)"
    )


# ── 汇总正文 ──


def test_block_never_relies_on_single_newline() -> None:
    """🔴 回归防线：GFM 会把单个 `\\n` 折叠成空格，区块内分行只能用空行或列表项。

    早期排版最致命的 bug 就是靠 `\\n` 分「每行 N 只」，实际渲染是一整段，
    由渲染器在随机位置折行，把股票名劈成两半。
    """
    n = _notifier()
    block = n._render_block(1, "MaVolumeStrategy", ["601975", "600900"], NAMES, BOARDS)

    paragraphs = [p for p in block.split("\n\n") if p]
    assert len(paragraphs) >= 4  # 标题 / 买点 / 卖点 / 板块列表
    for para in paragraphs:
        if para.startswith("- "):
            # 列表段落内部只能用换行分隔列表项，每行都必须是列表项
            assert all(ln.startswith("- ") for ln in para.splitlines()), para
        else:
            assert "\n" not in para, f"段落里出现了会被折叠的裸换行：{para!r}"


def test_build_digest_content_keeps_rules_and_boards() -> None:
    """区块应含序号、中文名、买点、卖点、行业板块与数据时效提醒。"""
    n = _notifier()

    content = n._build_digest_content(
        [("MaVolumeStrategy", ["601975"])],
        {"601975": "招商南油"},
        {"601975": "港口航运"},
        data_date="2020-01-01",
    )

    assert "①" in content
    assert "均线金叉+放量突破" in content
    assert "🎯 买点：" in content
    assert "🛑 卖点：" in content
    assert "（港口航运）" in content
    assert "⚠️ 数据截止 2020-01-01" in content
    assert "东财行业分类" in content  # 页脚声明了板块口径


def test_build_digest_content_unknown_strategy() -> None:
    """未登记的策略类名原样显示且不带买卖点，避免新增策略丢内容。"""
    n = _notifier()

    content = n._build_digest_content(
        [("BrandNewStrategy", ["600519"])],
        {"600519": "贵州茅台"},
        {},
    )

    assert "BrandNewStrategy" in content
    assert "买点" not in content
    assert "贵州茅台" in content
