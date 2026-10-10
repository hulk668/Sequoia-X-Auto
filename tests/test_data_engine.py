"""数据引擎属性测试。"""

import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def make_engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例。"""
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        pushplus_token="test-token",
    )
    engine = DataEngine(settings)
    return engine, settings


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    # ignore_cleanup_errors：Windows 上 SQLite 文件句柄释放有延迟，
    # 清理临时目录会偶发 PermissionError（`with sqlite3.connect()` 只提交事务、
    # 不关闭连接，句柄要等引用计数归零才释放）。测试断言不受影响。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        row = {
            "symbol": symbol, "date": str(trade_date),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000.0, "turnover": 10500.0,
        }
        df = pd.DataFrame([row])
        with sqlite3.connect(engine.db_path) as conn:
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]
        assert count == 1


def test_industry_from_em2016_takes_second_level() -> None:
    """东财三级行业分类取第二级：`交通运输-港口航运-航运` → `港口航运`。"""
    from sequoia_x.data.engine import _industry_from_em2016

    assert _industry_from_em2016("交通运输-港口航运-航运") == "港口航运"
    assert _industry_from_em2016("电子设备-半导体-集成电路") == "半导体"
    assert _industry_from_em2016("公用事业-环保-环保") == "环保"
    # 分级不足 / 空值 → 空串，由调用方按「无板块」处理
    assert _industry_from_em2016("综合") == ""
    assert _industry_from_em2016("") == ""
    assert _industry_from_em2016("  交运 - 港口航运  ") == "港口航运"


def test_get_boards_caches_and_skips_bj(monkeypatch) -> None:
    """行业反查：批量请求、只写回查到的、北交所直接跳过、第二次走缓存不再请求。"""
    import sequoia_x.data.engine as eng_module

    calls: list[list[str]] = []

    def fake_fetch(symbols: list[str]) -> dict[str, str]:
        calls.append(list(symbols))
        return {s: "港口航运" for s in symbols if s == "601975"}

    monkeypatch.setattr(eng_module, "_em_fetch_boards", fake_fetch)

    # ignore_cleanup_errors：Windows 上 SQLite 文件句柄释放有延迟，
    # 清理临时目录会偶发 PermissionError（`with sqlite3.connect()` 只提交事务、
    # 不关闭连接，句柄要等引用计数归零才释放）。测试断言不受影响。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)

        got = engine.get_boards(["601975", "000001", "830799"])
        assert got == {"601975": "港口航运"}
        # 830799 是北交所（8 开头），东财没有 F10 数据，不应出现在请求里
        assert calls == [["601975", "000001"]]

        calls.clear()
        assert engine.get_boards(["601975"]) == {"601975": "港口航运"}
        assert calls == []


def test_get_boards_survives_fetch_error(monkeypatch) -> None:
    """反查异常时返回已有缓存，不抛异常、不写脏数据（下轮会重试）。"""
    import sequoia_x.data.engine as eng_module

    def boom(symbols: list[str]) -> dict[str, str]:
        raise RuntimeError("network down")

    monkeypatch.setattr(eng_module, "_em_fetch_boards", boom)

    # ignore_cleanup_errors：Windows 上 SQLite 文件句柄释放有延迟，
    # 清理临时目录会偶发 PermissionError（`with sqlite3.connect()` 只提交事务、
    # 不关闭连接，句柄要等引用计数归零才释放）。测试断言不受影响。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        assert engine.get_boards(["601975"]) == {}
        # 失败不写库 → 第二次再查依然会重新请求（这里仍然返回空）
        assert engine.get_boards(["601975"]) == {}


def test_init_db_drops_legacy_concept_table() -> None:
    """早期版本的 stock_concept 表会被清掉，避免错误的板块数据被误读。"""
    # ignore_cleanup_errors：Windows 上 SQLite 文件句柄释放有延迟，
    # 清理临时目录会偶发 PermissionError（`with sqlite3.connect()` 只提交事务、
    # 不关闭连接，句柄要等引用计数归零才释放）。测试断言不受影响。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)

        with sqlite3.connect(engine.db_path) as conn:
            conn.execute(
                "CREATE TABLE stock_concept ("
                "symbol TEXT PRIMARY KEY, concept TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO stock_concept VALUES ('601975', '一带一路', '2026-01-01')"
            )
            conn.commit()

        engine._init_db()

        with sqlite3.connect(engine.db_path) as conn:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "stock_concept" not in tables
        assert "stock_board" in tables


def test_name_coverage() -> None:
    """覆盖率 = stock_name 覆盖 stock_daily 中股票的比例。"""
    # ignore_cleanup_errors：Windows 上 SQLite 文件句柄释放有延迟，
    # 清理临时目录会偶发 PermissionError（`with sqlite3.connect()` 只提交事务、
    # 不关闭连接，句柄要等引用计数归零才释放）。测试断言不受影响。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        assert engine.name_coverage() == 1.0  # 空库视为齐全，不触发刷新

        with sqlite3.connect(engine.db_path) as conn:
            conn.executemany(
                "INSERT INTO stock_daily (symbol, date, close) VALUES (?, ?, ?)",
                [("600519", "2026-01-01", 1.0), ("000001", "2026-01-01", 1.0)],
            )
            conn.execute("INSERT INTO stock_name (symbol, name) VALUES (?, ?)", ("600519", "贵州茅台"))
            conn.commit()

        assert engine.name_coverage() == 0.5
