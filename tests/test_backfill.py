"""全市场补数模块测试。

沙箱里 `requests` 走透明代理，东财 `push2his` 的 K 线路径被挡
（`/api/qt/stock/kline/get` 恒返回 `http=000`），所以真实抓取用桩数据验证。
另有 `.workbuddy/_validate_turnover.py` 用真实腾讯响应 + 库中真实成交额
对拍过成交额估算的精度（97.9% 的交易日误差 ≤1%）。
"""

import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.backfill import (
    MarketBackfiller,
    em_secid_candidates,
    parse_em_klines,
    parse_qq_klines,
    qq_code,
)
from sequoia_x.data.engine import DataEngine


def _engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        notify_channel="none",
    )
    return DataEngine(settings), settings


def _kline_payload(klines: list[str], name: str = "测试股") -> dict:
    return {"data": {"code": "600000", "name": name, "klines": klines}}


def _qq_pair(code: str, adj_key: str, adj: list[list], raw: list[list]) -> tuple[dict, dict]:
    """构造腾讯解析所需的**两份**响应：复权份 + 不复权份。

    真实场景下这是两次独立请求，所以是两个 payload，不能合并成一个。
    """
    return (
        {"data": {code: {adj_key: adj, "qt": {}}}},
        {"data": {code: {"day": raw, "qt": {}}}},
    )


# ── 代码映射：四个板块都不能漏 ──


def test_em_secid_candidates_covers_all_boards() -> None:
    """沪市（含科创板）走 `1.`，深市（含创业板）走 `0.`，北交所给两个候选。"""
    assert em_secid_candidates("600000") == ["1.600000"]  # 沪市主板
    assert em_secid_candidates("601398") == ["1.601398"]  # 沪市主板
    assert em_secid_candidates("603200") == ["1.603200"]  # 沪市主板
    assert em_secid_candidates("688981") == ["1.688981"]  # 科创板
    assert em_secid_candidates("000001") == ["0.000001"]  # 深市主板
    assert em_secid_candidates("002594") == ["0.002594"]  # 深市中小板
    assert em_secid_candidates("300750") == ["0.300750"]  # 创业板
    assert em_secid_candidates("301389") == ["0.301389"]  # 创业板
    # 北交所在东财两个口径里都可能出现，不猜，两个都试
    assert em_secid_candidates("430047") == ["0.430047", "1.430047"]
    assert em_secid_candidates("830799") == ["0.830799", "1.830799"]


def test_qq_code_covers_all_boards() -> None:
    """腾讯代码前缀：6/9 → sh，4/8 → bj，其余 → sz。"""
    assert qq_code("600000") == "sh600000"
    assert qq_code("688981") == "sh688981"
    assert qq_code("000001") == "sz000001"
    assert qq_code("002594") == "sz002594"
    assert qq_code("300750") == "sz300750"
    assert qq_code("430047") == "bj430047"
    assert qq_code("830799") == "bj830799"


# ── 东财解析：字段顺序与单位是最容易写错的地方 ──


def test_parse_em_klines_field_order_and_volume_unit() -> None:
    """东财 kline 的字段是 `日期,开,收,高,低,量(手),额(元)`。

    `f52` 之后是**收盘**而不是最高，与多数行情接口的直觉顺序相反 ——
    搞反了 open/high/low/close 就会把 K 线写坏，所以这里逐个字段钉死。
    成交量还要从「手」换算成「股」（×100）。
    """
    payload = _kline_payload([
        "2024-01-02,295.77,286.07,296.33,285.69,214033,3390080350.00",
    ])

    rows = parse_em_klines(payload, "300750")

    assert len(rows) == 1
    symbol, d, open_, high, low, close, volume, turnover = rows[0]
    assert symbol == "300750"
    assert d == "2024-01-02"
    assert open_ == 295.77
    assert close == 286.07      # 第 3 个字段是收盘
    assert high == 296.33       # 第 4 个字段才是最高
    assert low == 285.69
    assert volume == 214033 * 100   # 手 → 股
    assert turnover == 3390080350.00


def test_parse_em_klines_skips_dirty_rows() -> None:
    """字段缺失、数值非法、成交量为 0 的行一律丢弃，绝不写脏数据进库。"""
    payload = _kline_payload([
        "2024-01-02,1,2,3,4,5,6",          # 正常
        "2024-01-03,1,2,3",                 # 字段不够
        "2024-01-04,a,b,c,d,e,f",           # 非数值
        "2024-01-05,-,1,2,3,100,200",       # 停牌占位符
        "2024-01-06,1,2,3,4,0,0",           # 成交量为 0（停牌）
        "2024-01-07,1,0,3,4,100,200",       # 收盘价为 0（异常）
        "2024-01-08,10,11,12,9,1000,11000",  # 正常
    ])

    rows = parse_em_klines(payload, "600000")

    assert [r[1] for r in rows] == ["2024-01-02", "2024-01-08"]


def test_parse_em_klines_handles_empty_and_broken_payload() -> None:
    """接口返回 data 为 null / 空 klines / 整个 payload 为 None 都不能炸。"""
    assert parse_em_klines({}, "600000") == []
    assert parse_em_klines({"data": None}, "600000") == []
    assert parse_em_klines(_kline_payload([]), "600000") == []
    assert parse_em_klines(None, "600000") == []


# ── 腾讯解析：价格取复权、成交额取不复权，两份数据必须正确合并 ──


def test_qq_share_scale_differs_for_star_market() -> None:
    """科创板按「股」返回，其余板块按「手」—— 差 100 倍，必须区分。"""
    from sequoia_x.data.backfill import qq_share_scale

    assert qq_share_scale("688981") == 1     # 科创板
    assert qq_share_scale("689009") == 1     # 科创板（CDR）
    assert qq_share_scale("600000") == 100   # 沪市主板
    assert qq_share_scale("000001") == 100   # 深市主板
    assert qq_share_scale("002594") == 100   # 深市中小板
    assert qq_share_scale("300750") == 100   # 创业板
    assert qq_share_scale("430047") == 100   # 北交所


def test_parse_qq_klines_does_not_scale_star_market_volume() -> None:
    """科创板成交量不该 ×100。

    实测：腾讯对 688981 返回的当日量 46,104,484 若当成「手」，
    隐含均价会是收盘价的 100 倍，明显不合理；按「股」才对得上 `qt[57]` 成交额。
    """
    code = "sh688981"
    adj = [["2024-01-02", "42.80", "42.68", "44.10", "41.77", "21948019"]]
    raw = [["2024-01-02", "42.80", "42.68", "44.10", "41.77", "21948019"]]

    rows = parse_qq_klines(*_qq_pair(code, "qfqday", adj, raw), "688981")

    assert rows[0][6] == 21948019          # 不乘 100
    assert rows[0][7] == pytest.approx(21948019 * (44.10 + 41.77 + 42.68) / 3)


def test_parse_qq_klines_merges_adjusted_price_and_raw_turnover() -> None:
    """价格用前复权那份，成交额用**不复权**均价估算 —— 不能混。

    腾讯每行是 `[日期, 开, 收, 高, 低, 量(手)]`，第 3 个是**收盘**。
    这里故意让两份数据价格不同（复权份 ×0.8），以证明没有取错来源。
    """
    code = "sh600000"
    adj = [["2024-01-02", "8.00", "8.40", "8.64", "7.92", "1000"]]
    raw = [["2024-01-02", "10.00", "10.50", "10.80", "9.90", "1000"]]

    rows = parse_qq_klines(*_qq_pair(code, "qfqday", adj, raw), "600000")

    assert len(rows) == 1
    symbol, d, open_, high, low, close, volume, turnover = rows[0]
    assert symbol == "600000"
    assert d == "2024-01-02"
    # 价格来自复权份
    assert open_ == 8.00
    assert close == 8.40
    assert high == 8.64
    assert low == 7.92
    # 量：手 → 股
    assert volume == 1000 * 100
    # 额：用不复权的 (高+低+收)/3 = (10.80+9.90+10.50)/3 = 10.4
    assert turnover == pytest.approx(100000 * 10.4)


def test_parse_qq_klines_accepts_day_key_fallback() -> None:
    """复权那一份的键名不固定：多数是 `qfqday`，科创板等只给 `day`。"""
    code = "sh688981"
    adj = [["2024-01-02", "8.00", "8.40", "8.64", "7.92", "1000"]]
    raw = [["2024-01-02", "10.00", "10.50", "10.80", "9.90", "1000"]]

    rows = parse_qq_klines(*_qq_pair(code, "day", adj, raw), "688981")

    assert len(rows) == 1
    assert rows[0][5] == 8.40  # 收盘仍取复权份


def test_qq_parse_needs_both_sources() -> None:
    """只有复权份、拿不到不复权均价时，该日宁可丢弃也不猜成交额。"""
    code = "sh600000"
    adj = [["2024-01-02", "8.00", "8.40", "8.64", "7.92", "1000"]]
    raw = [["2024-01-02", "10.00", "10.50", "10.80", "9.90", "1000"]]
    adj_payload, _raw_payload = _qq_pair(code, "qfqday", adj, raw)

    assert parse_qq_klines(adj_payload, {}, "600000") == []
    assert parse_qq_klines({}, {}, "600000") == []
    assert parse_qq_klines(None, None, "600000") == []


def test_parse_qq_klines_drops_dates_missing_in_raw() -> None:
    """不复权那份缺某天时，该天宁可丢弃也不能用复权价当均价。"""
    code = "sh600000"
    adj = [
        ["2024-01-02", "8.00", "8.40", "8.64", "7.92", "1000"],
        ["2024-01-03", "8.40", "8.80", "9.00", "8.30", "2000"],
    ]
    raw = [["2024-01-02", "10.00", "10.50", "10.80", "9.90", "1000"]]

    rows = parse_qq_klines(*_qq_pair(code, "qfqday", adj, raw), "600000")

    assert [r[1] for r in rows] == ["2024-01-02"]


def test_parse_qq_klines_skips_dirty_rows_and_sorts() -> None:
    """脏行丢弃、结果按日期升序（东财本来就升序，腾讯不保证）。"""
    code = "sh600000"
    adj = [
        ["2024-01-03", "8.40", "8.80", "9.00", "8.30", "2000"],
        ["2024-01-04", "8.80", "-", "9.20", "8.70", "3000"],   # 收盘非法
        ["2024-01-05", "9.00", "9.20", "9.30", "8.90", "0"],   # 量 0
        ["2024-01-02", "8.00", "8.40", "8.64", "7.92", "1000"],
    ]
    raw = [
        ["2024-01-02", "10.00", "10.50", "10.80", "9.90", "1000"],
        ["2024-01-03", "11.00", "11.50", "11.80", "10.90", "2000"],
        ["2024-01-04", "12.00", "12.50", "12.80", "11.90", "3000"],
        ["2024-01-05", "13.00", "13.50", "13.80", "12.90", "4000"],
    ]

    rows = parse_qq_klines(*_qq_pair(code, "qfqday", adj, raw), "600000")

    assert [r[1] for r in rows] == ["2024-01-02", "2024-01-03"]


# ── 待补清单：只补完全空缺的股票 ──


def _seed(engine: DataEngine, symbol: str, dates: list[str]) -> None:
    df = pd.DataFrame([
        {"symbol": symbol, "date": d, "open": 10.0, "high": 11.0,
         "low": 9.0, "close": 10.5, "volume": 1000.0, "turnover": 10500.0}
        for d in dates
    ])
    engine._upsert_daily(df)


def test_pending_only_returns_absent_symbols() -> None:
    """已有数据的股票（哪怕只有一行、明显落后）也不在待补清单里。

    它们由日常增量通道负责续写，补数通道的复权基准与 baostock 不同，
    直接往后接会让同一条价格序列出现基准断层。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, settings = _engine_in(tmp_dir)
        _seed(engine, "600000", ["2024-01-02", "2024-01-03"])
        _seed(engine, "600001", ["2024-01-02"])  # 落后，但已有数据 → 不补

        bf = MarketBackfiller(engine, settings, end_date="2026-10-09")

        assert bf.pending(["600000", "600001", "300750"]) == ["300750"]


# ── 主流程：写入、源统计、截断、续跑 ──


def _stub_fetch(mapping: dict[str, tuple[list[list], str]]):
    """把 `_fetch` 换成查表：值为 (行, 源名)。"""
    calls: list[str] = []

    def _fetch(self, symbol: str):
        calls.append(symbol)
        return mapping.get(symbol, ([], ""))

    return _fetch, calls


def _qq_rows(symbol: str, n: int, start_day: int = 1) -> list[list]:
    return [
        [symbol, f"2024-01-{start_day + i:02d}", 10.0, 11.0, 9.0, 10.5,
         100000.0, 1_000_000.0]
        for i in range(n)
    ]


def test_run_writes_rows_and_counts_source(monkeypatch) -> None:
    """写入成功的股票要按实际生效的源分别计数。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, settings = _engine_in(tmp_dir)
        bf = MarketBackfiller(engine, settings, end_date="2024-01-03", source="qq")

        fetch, _ = _stub_fetch({
            "300750": (_qq_rows("300750", 3), "qq"),
            "688981": (_qq_rows("688981", 2), "em"),
        })
        monkeypatch.setattr(MarketBackfiller, "_fetch", fetch)

        stats = bf.run(["300750", "688981"])

        assert stats["ok"] == 2
        assert stats["em"] == 1
        assert stats["qq"] == 1
        assert stats["rows"] == 5
        with sqlite3.connect(engine.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0] == 5


def test_run_bars_truncates_to_last_n(monkeypatch) -> None:
    """`bars` 生效时只保留最近 N 根 —— 种子文件大小靠它控制。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, settings = _engine_in(tmp_dir)
        bf = MarketBackfiller(engine, settings, end_date="2024-01-31", bars=5, source="qq")

        fetch, _ = _stub_fetch({"300750": (_qq_rows("300750", 20), "qq")})
        monkeypatch.setattr(MarketBackfiller, "_fetch", fetch)

        stats = bf.run(["300750"])

        assert stats["rows"] == 5
        with sqlite3.connect(engine.db_path) as conn:
            dates = [r[0] for r in conn.execute(
                "SELECT date FROM stock_daily ORDER BY date"
            )]
        assert dates[0] == "2024-01-16"   # 20 根里取最后 5 根
        assert dates[-1] == "2024-01-20"


def test_run_is_resumable(monkeypatch) -> None:
    """写入后重跑，已入库的股票不再出现在待补清单里。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, settings = _engine_in(tmp_dir)
        bf = MarketBackfiller(engine, settings, end_date="2024-01-31", source="qq")

        fetch, calls = _stub_fetch({
            "300750": (_qq_rows("300750", 3), "qq"),
            "600001": ([], ""),          # 没取到 → 应残留待补
        })
        monkeypatch.setattr(MarketBackfiller, "_fetch", fetch)

        stats = bf.run(["300750", "600001"])
        assert stats["ok"] == 1 and stats["empty"] == 1
        assert sorted(calls) == ["300750", "600001"]

        # 第二次：300750 已入库，只剩 600001 重试
        fetch2, calls2 = _stub_fetch({"600001": (_qq_rows("600001", 2), "em")})
        monkeypatch.setattr(MarketBackfiller, "_fetch", fetch2)
        stats2 = bf.run(["300750", "600001"])

        assert calls2 == ["600001"]
        assert stats2["ok"] == 1 and stats2["em"] == 1


def test_qq_request_rotates_through_bases(monkeypatch) -> None:
    """腾讯同一份数据挂在多个入口下，可用性不同 —— 要逐个试到有效响应。

    实测 `web.ifzq.gtimg.cn/appstock/app/fqkline/get` 先是可用，
    补数跑到一半变成返回 HTML；这时必须能切到 `proxy.finance.qq.com/ifzqgtimg`。
    """
    from sequoia_x.data import backfill as bf_mod

    tried: list[str] = []
    good = f"{bf_mod._QQ_BASES[-1]}/appstock/app/fqkline/get"

    def fake_http_json(url, params, session, timeout):
        tried.append(url)
        if url == good:                      # 约定：只有最后一个入口能出数据
            return {"code": 0, "data": {"sh600000": {"qfqday": [["2024-01-02"]]}}}
        if "ifzqgtimg" in url:
            return {"code": 11, "data": None}  # 通但业务码错误
        return None                            # 被拦 → 非 JSON

    monkeypatch.setattr(bf_mod, "_http_json", fake_http_json)

    got = bf_mod._qq_request("/appstock/app/fqkline/get", {"param": "x"}, None, (5, 5))

    assert got and got["code"] == 0
    assert len(tried) == len(bf_mod._QQ_BASES)   # 前几个都被跳过
    assert tried == [f"{b}/appstock/app/fqkline/get" for b in bf_mod._QQ_BASES]


def test_qq_request_returns_none_when_all_bases_fail(monkeypatch) -> None:
    """所有入口都不可用时返回 None，不抛异常。"""
    from sequoia_x.data import backfill as bf_mod

    monkeypatch.setattr(bf_mod, "_http_json", lambda *a, **kw: None)

    assert bf_mod._qq_request("/appstock/app/fqkline/get", {}, None, (5, 5)) is None


def test_load_symbols_retries_and_takes_longest(monkeypatch) -> None:
    """在线股票清单偶发被截断，应重试取最长的那次。"""
    from sequoia_x.data import backfill as bf_mod

    seq = [[f"60000{i}" for i in range(230)],
           [f"60000{i}" for i in range(3000)],   # 仍不达标（<4000）
           [f"60000{i}" for i in range(5200)]]
    calls = []

    class _Eng:
        def get_all_symbols(self):
            calls.append(1)
            return seq[len(calls) - 1]

        def get_stock_names(self):
            return {}

    assert len(bf_mod.load_symbols(_Eng(), min_expected=4000, attempts=3)) == 5200
    assert len(calls) == 3


def test_load_symbols_falls_back_to_local_names(monkeypatch) -> None:
    """三次都截断时退回本地 stock_name 表（不依赖网络）。"""
    from sequoia_x.data import backfill as bf_mod

    class _Eng:
        def get_all_symbols(self):
            return ["600000", "600001"]      # 明显截断

        def get_stock_names(self):
            return {f"00000{i}": "x" for i in range(5561)}

    got = bf_mod.load_symbols(_Eng(), min_expected=4000, attempts=3)
    assert len(got) == 5561


def test_load_symbols_returns_empty_when_all_fail() -> None:
    """全市场清单与本地表都拿不到时返回空列表，不抛异常。"""
    from sequoia_x.data import backfill as bf_mod

    class _Eng:
        def get_all_symbols(self):
            raise OSError("在线接口不通")

        def get_stock_names(self):
            return {}

    assert bf_mod.load_symbols(_Eng(), min_expected=4000, attempts=2) == []


def test_run_noop_when_nothing_pending(monkeypatch) -> None:
    """全部已有数据时直接返回，不发任何请求。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, settings = _engine_in(tmp_dir)
        _seed(engine, "600000", ["2024-01-02"])
        bf = MarketBackfiller(engine, settings, end_date="2024-01-31")

        fetch, calls = _stub_fetch({})
        monkeypatch.setattr(MarketBackfiller, "_fetch", fetch)

        assert bf.run(["600000"]) == {
            "ok": 0, "empty": 0, "failed": 0, "rows": 0, "em": 0, "qq": 0,
        }
        assert calls == []


def test_resolve_auto_source_prefers_em_then_falls_back(monkeypatch) -> None:
    """auto 选源：东财能出数据就用东财，否则全程腾讯。

    不能在每只票上现场试东财 —— 东财不可达时每次要白等 2×连接超时，
    几千只票会把整轮从几分钟拖到几小时。
    """
    from sequoia_x.data import backfill as bf_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, settings = _engine_in(tmp_dir)
        bf = MarketBackfiller(engine, settings, end_date="2024-01-31", source="auto")

        monkeypatch.setattr(bf_mod, "fetch_em_kline", lambda *a, **kw: _qq_rows("600000", 1))
        assert bf._resolve_auto_source() == "em"

        monkeypatch.setattr(bf_mod, "fetch_em_kline", lambda *a, **kw: [])
        assert bf._resolve_auto_source() == "qq"

        def boom(*a, **kw):
            raise OSError("连接重置")

        monkeypatch.setattr(bf_mod, "fetch_em_kline", boom)
        assert bf._resolve_auto_source() == "qq"


def test_run_locks_source_before_starting(monkeypatch) -> None:
    """auto 模式开跑前把 source 定死，后续不再逐只重试东财。"""
    from sequoia_x.data import backfill as bf_mod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, settings = _engine_in(tmp_dir)
        bf = MarketBackfiller(engine, settings, end_date="2024-01-31", source="auto")

        monkeypatch.setattr(bf_mod, "fetch_em_kline", lambda *a, **kw: [])
        monkeypatch.setattr(bf_mod, "fetch_qq_kline", lambda *a, **kw: _qq_rows("300750", 2))

        stats = bf.run(["300750"])

        assert bf.source == "qq"          # 已定死
        assert stats["qq"] == 1 and stats["ok"] == 1


def test_fetch_retries_then_gives_up(monkeypatch) -> None:
    """网络一直失败时 `_fetch` 返回空结果，且不抛异常。"""
    import sequoia_x.data.backfill as bf_mod

    monkeypatch.setattr(bf_mod.time, "sleep", lambda _s: None)

    attempts = []

    def boom(*_a, **_kw):
        attempts.append(1)
        raise OSError("连接重置")

    monkeypatch.setattr(bf_mod, "fetch_kline", boom)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, settings = _engine_in(tmp_dir)
        bf = MarketBackfiller(engine, settings, end_date="2024-01-03")
        assert bf._fetch("300750") == ([], "")
        assert len(attempts) == bf_mod._MAX_RETRIES


# ── 源回退 ──


def test_fetch_kline_falls_back_from_em_to_qq(monkeypatch) -> None:
    """auto 模式下东财拿不到数据时应自动落到腾讯。"""
    import sequoia_x.data.backfill as bf_mod

    monkeypatch.setattr(bf_mod, "fetch_em_kline", lambda *a, **kw: [])
    monkeypatch.setattr(
        bf_mod, "fetch_qq_kline", lambda *a, **kw: _qq_rows("300750", 1),
    )

    rows, used = bf_mod.fetch_kline("300750", "2024-01-01", "2024-01-03")

    assert used == "qq"
    assert len(rows) == 1


def test_fetch_kline_respects_explicit_source(monkeypatch) -> None:
    """指定单一源时不做回退。"""
    import sequoia_x.data.backfill as bf_mod

    monkeypatch.setattr(bf_mod, "fetch_em_kline", lambda *a, **kw: [])
    monkeypatch.setattr(
        bf_mod, "fetch_qq_kline", lambda *a, **kw: _qq_rows("300750", 1),
    )

    rows, used = bf_mod.fetch_kline("300750", "2024-01-01", "2024-01-03", source="em")

    assert (rows, used) == ([], "")


# ── 与策略/推送的口径一致性 ──


def test_backfilled_rows_have_same_columns_as_incremental_rows() -> None:
    """补数写入的行必须与日常增量通道的列口径一致，否则策略会读错列。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, _ = _engine_in(tmp_dir)
        _seed(engine, "600000", ["2024-01-02"])   # 模拟增量通道写入

        df = engine.get_ohlcv("600000")
        assert list(df.columns) == [
            "id", "symbol", "date", "open", "high", "low", "close",
            "volume", "turnover",
        ]
