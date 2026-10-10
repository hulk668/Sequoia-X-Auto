"""策略引擎属性测试。"""

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.ma_volume import MaVolumeStrategy


# Feature: sequoia-x-v2, Property 9: 策略 run() 返回值类型正确
@given(
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=0, max_size=3, unique=True,
    )
)
@h_settings(max_examples=30, deadline=None)
def test_strategy_run_returns_list_of_str(symbols: list[str]) -> None:
    """属性 9：run() 应返回 list[str]，每个元素为非空字符串。"""
    # ignore_cleanup_errors：Windows 上 SQLite 句柄释放有延迟，清理临时目录会偶发
    # PermissionError，与断言无关（详见 test_data_engine.py 的同类说明）。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            pushplus_token="test-token",
        )
        engine = DataEngine(settings)

        with patch.object(engine, "get_all_symbols", return_value=symbols):
            with patch.object(engine, "get_ohlcv", return_value=pd.DataFrame()):
                strategy = MaVolumeStrategy(engine=engine, settings=settings)
                result = strategy.run()

    assert isinstance(result, list)
    assert all(isinstance(s, str) and len(s) > 0 for s in result)


class _FakeEngine:
    """最小引擎替身：只提供策略真正会调用的两个方法。"""

    db_path = ":memory:"

    def __init__(self, data: dict[str, pd.DataFrame]) -> None:
        self._data = data

    def get_local_symbols(self) -> list[str]:
        return list(self._data)

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        return self._data[symbol]


def _settings() -> Settings:
    return Settings(pushplus_token="test-token", _env_file=None)


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """按顺序给行补上 symbol/date，返回 get_ohlcv 形态的 DataFrame。"""
    dates = pd.date_range("2026-01-01", periods=len(rows), freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame([{"symbol": "600000", "date": d, **r} for d, r in zip(dates, rows)])


def _bar(close: float, volume: float = 1_000_000.0) -> dict:
    return {
        "open": close, "high": close, "low": close, "close": close,
        "volume": volume, "turnover": close * volume,
    }


# ── 边界：最少 K 线根数 ──


def test_ma_volume_requires_21_bars() -> None:
    """均线金叉要比较「昨日」的 ma20，所以至少要 21 根 K 线。

    20 根时昨日那行的 ma20 还是 NaN，比较恒为 False —— 守卫写成 20 会让人误以为
    「20 根就够」，这里把它钉死成 21。
    """
    from sequoia_x.strategy.ma_volume import MaVolumeStrategy as S

    assert S._MIN_BARS == 21

    # 先逐日阴跌 20 天（此时 ma5 < ma20），第 21 根放量跳空拉起 → 形成金叉
    rows = [_bar(10.0 - 0.05 * i) for i in range(20)]
    rows.append(_bar(12.0, volume=50_000_000.0))
    df21 = _make_df(rows)

    assert S(engine=_FakeEngine({"600000": df21}), settings=_settings()).run() == ["600000"]

    # 砍到 20 根 → 守卫拦掉（金叉需要昨日的 ma20，此时还没有）
    df20 = df21.head(20)
    assert S(engine=_FakeEngine({"600000": df20}), settings=_settings()).run() == []


def test_uptrend_limit_down_requires_61_bars() -> None:
    """上升趋势要看昨日的 ma60，所以至少要 61 根 K 线。"""
    from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy as S

    assert S._MIN_BARS == 61


# ── 健壮性：策略内部出错不能冒出 run() ──


def test_private_placement_survives_upstream_schema_change(monkeypatch) -> None:
    """akshare 改字段名（列不存在）时应返回空列表，而不是抛 KeyError 冒出 run()。

    这类异常会一路冒到 main.py 的最外层 except → sys.exit(1)，
    把其它策略已经算出来的选股结果一起丢掉。
    """
    from sequoia_x.strategy.private_placement import PrivatePlacementStrategy

    stub = types.ModuleType("akshare")
    # 列名与实现期望的（发行方式/发行日期/股票代码）不一致
    stub.stock_qbzf_em = lambda: pd.DataFrame({"证券代码": ["600000"], "增发方式": ["定向增发"]})
    monkeypatch.setitem(sys.modules, "akshare", stub)

    strategy = PrivatePlacementStrategy(engine=_FakeEngine({}), settings=_settings())
    assert strategy.run() == []


def test_turtle_sorts_by_turnover_without_baostock() -> None:
    """海龟排序只用库内成交额，且绝不依赖 baostock。

    历史教训：原来的实现会调 baostock 取流通市值来排序，免费服务一挂整轮就白跑；
    而且外部接口取的是 date.today()，与策略判定所用的库内最后交易日可能对不上。
    现在改成按当日 turnover 降序，数据全来自本地库。
    """
    from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy

    # 排序一旦退回 baostock，这个方法名下就会重新出现 _get_market_caps
    assert not hasattr(TurtleTradeStrategy, "_get_market_caps")

    def _breakout_df(turnover: float) -> pd.DataFrame:
        """20 天横盘（高点 9.5）+ 第 21 根放量突破前高、收阳、真涨。"""
        rows = [_bar(9.5) for _ in range(20)]
        rows.append({
            "open": 10.0, "high": 10.8, "low": 9.9, "close": 10.5,
            "volume": turnover / 10.5, "turnover": turnover,
        })
        return _make_df(rows)

    engine = _FakeEngine({
        "600000": _breakout_df(150_000_000.0),
        "600001": _breakout_df(900_000_000.0),  # 成交额最大 → 应排第一
        "600002": _breakout_df(300_000_000.0),
    })
    strategy = TurtleTradeStrategy(engine=engine, settings=_settings())

    assert strategy.run() == ["600001", "600002", "600000"]

