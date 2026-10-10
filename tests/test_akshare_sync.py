"""akshare 增量同步的边界与并发行为测试。

覆盖两处曾经踩坑的地方：

1. **周末白跑**：手动在周末触发时，全市场 5000+ 只会一只都拿不到新数据，
   按老逻辑要发完所有请求才知道 —— 白拉十几分钟。
2. **pool.map 按提交顺序产出**：前面某只慢就会挡住后面已完成的结果，
   日志表现为长时间纹丝不动。改成 as_completed 后要保证结果仍对回各自的股票。
"""

import sqlite3
import time
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data import engine as eng_module
from sequoia_x.data.engine import DataEngine


def _make_engine(tmp_dir: str) -> DataEngine:
    return DataEngine(
        Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            notify_channel="none",
        )
    )


def _seed(engine: DataEngine, rows: list[tuple[str, str, float]]) -> None:
    """写入初始行情（symbol, date, close）—— 充当换算锚点。"""
    df = pd.DataFrame(
        [
            {
                "symbol": s,
                "date": d,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1000.0,
                "turnover": 1000.0 * c,
            }
            for s, d, c in rows
        ]
    )
    with sqlite3.connect(engine.db_path) as conn:
        df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")


# ── 1. 周末跳过 ──────────────────────────────────────────────


def test_weekend_is_skipped_without_any_request(monkeypatch, tmp_path) -> None:
    """周末（A 股不开盘）应直接返回 0，且一个拉取请求都不发。"""
    engine = _make_engine(str(tmp_path))
    _seed(engine, [("000001", "2026-10-09", 10.0)])

    called: list = []
    monkeypatch.setattr(eng_module, "_ak_fetch_one", lambda t: called.append(t) or [])

    assert engine.sync_via_akshare(today="2026-10-10") == 0  # 周六
    assert engine.sync_via_akshare(today="2026-10-11") == 0  # 周日
    assert called == [], "周末不应发出任何拉取请求"


def test_weekday_still_fetches(monkeypatch, tmp_path) -> None:
    """工作日不受周末跳过逻辑影响（2026-10-09 是周五）。"""
    engine = _make_engine(str(tmp_path))
    _seed(engine, [("000001", "2026-10-08", 10.0)])

    called: list = []
    monkeypatch.setattr(eng_module, "_ak_fetch_one", lambda t: called.append(t) or [])

    engine.sync_via_akshare(today="2026-10-09")
    assert len(called) == 1


def test_bad_today_format_does_not_crash(monkeypatch, tmp_path) -> None:
    """today 格式异常时不该抛异常，交给下游逻辑自然处理。"""
    engine = _make_engine(str(tmp_path))
    _seed(engine, [("000001", "2026-10-08", 10.0)])
    monkeypatch.setattr(eng_module, "_ak_fetch_one", lambda t: [])

    assert engine.sync_via_akshare(today="not-a-date") == 0


# ── 2. 乱序完成下结果仍要对齐 ────────────────────────────────


def test_results_align_to_symbol_despite_out_of_order(monkeypatch, tmp_path) -> None:
    """as_completed 按完成顺序产出，结果必须仍对回各自的股票。

    故意让**提交顺序最早**的那只最后返回 —— pool.map 的语义下这会让
    整批一起等它；改 as_completed 后虽然收集顺序变了，
    但写入的数据不能串到别的股票上。
    """
    engine = _make_engine(str(tmp_path))
    _seed(
        engine,
        [
            ("000001", "2026-10-08", 10.0),
            ("000002", "2026-10-08", 20.0),
            ("000003", "2026-10-08", 30.0),
        ],
    )

    new_close = {"000001": 20.0, "000002": 40.0, "000003": 60.0}
    anchor_close = {"000001": 10.0, "000002": 20.0, "000003": 30.0}
    delay = {"000001": 0.20, "000002": 0.10, "000003": 0.0}

    def fake_fetch(task):
        sym = task[0]
        time.sleep(delay[sym])
        # 返回格式：(date, open, close, high, low, volume(手), turnover(元))
        return [
            ["2026-10-08", 10.0, anchor_close[sym], 10.0, 10.0, 100, 1000],  # 锚点行
            ["2026-10-09", 11.0, new_close[sym], 12.0, 9.0, 100, 1000],  # 新增行
        ]

    monkeypatch.setattr(eng_module, "_ak_fetch_one", fake_fetch)

    written = engine.sync_via_akshare(today="2026-10-09")
    assert written == 3

    with sqlite3.connect(engine.db_path) as conn:
        got = dict(
            conn.execute("SELECT symbol, close FROM stock_daily WHERE date = '2026-10-09'")
        )
    # 锚点价 == 库中价 → 换算系数 k=1，写入的收盘应等于 new_close 且与 symbol 一一对应
    assert got == {"000001": 20.0, "000002": 40.0, "000003": 60.0}


def test_single_failure_does_not_break_others(monkeypatch, tmp_path) -> None:
    """某只抛异常/返回空时，其余股票仍应正常写入。"""
    engine = _make_engine(str(tmp_path))
    _seed(
        engine,
        [("000001", "2026-10-08", 10.0), ("000002", "2026-10-08", 20.0)],
    )
    anchor_close = {"000001": 10.0, "000002": 20.0}

    def fake_fetch(task):
        sym = task[0]
        if sym == "000001":
            raise RuntimeError("模拟东财抽风")
        return [
            ["2026-10-08", 10.0, anchor_close[sym], 10.0, 10.0, 100, 1000],
            ["2026-10-09", 11.0, 40.0, 12.0, 9.0, 100, 1000],
        ]

    monkeypatch.setattr(eng_module, "_ak_fetch_one", fake_fetch)

    assert engine.sync_via_akshare(today="2026-10-09") == 1
    with sqlite3.connect(engine.db_path) as conn:
        got = dict(
            conn.execute("SELECT symbol, close FROM stock_daily WHERE date = '2026-10-09'")
        )
    assert got == {"000002": 40.0}


# ── 3. requests 默认超时补丁 ─────────────────────────────────


def test_request_timeout_patch_idempotent_and_respects_explicit(monkeypatch) -> None:
    """补超时只做一次；调用方显式传的 timeout 不被覆盖。"""
    import requests

    monkeypatch.setattr(eng_module, "_REQUESTS_TIMEOUT_PATCHED", False, raising=False)

    captured: dict = {}

    def spy(self, method, url, **kwargs):  # noqa: ANN001
        captured.clear()
        captured.update(kwargs)
        return "ok"

    # 把 spy 当成「原函数」，这样补丁包装后的调用会经过它，便于观察 kwargs
    monkeypatch.setattr(requests.Session, "request", spy)
    eng_module._ensure_request_timeout()
    patched = requests.Session.request
    assert patched is not spy

    # 再调一次不应再包一层
    eng_module._ensure_request_timeout()
    assert requests.Session.request is patched

    patched(object(), "GET", "http://example.invalid")
    assert captured["timeout"] == eng_module._AK_HTTP_TIMEOUT

    patched(object(), "GET", "http://example.invalid", timeout=99)
    assert captured["timeout"] == 99, "显式传入的 timeout 必须保留"
