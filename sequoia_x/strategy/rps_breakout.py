"""RPS 极强动量突破策略：120日涨幅排全市场前 10%，且股价接近 120 日新高。"""

import sqlite3

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 极强动量突破策略。

    选股条件：
    1. RPS >= 90：120 个交易日涨幅在全市场排进前 10%（横截面百分位）；
    2. 接近新高：最新收盘价 >= 过去 120 日最高价的 90%。

    与其它策略不同，这里是**横截面**策略（需要全市场一起排序），
    所以直接一条 SQL 把行情读进内存算，而不是逐只读库。

    Attributes:
        webhook_key: 路由到 'rps' 专属推送 token。
        rps_period: 计算涨幅的回看交易日数。
        rps_threshold: RPS 阈值（百分位）。
    """

    webhook_key: str = "rps"
    rps_period: int = 120
    rps_threshold: int = 90

    def run(self) -> list[str]:
        """返回满足 RPS 动量条件、且股价接近 120 日新高的股票代码列表。

        Returns:
            纯数字股票代码列表（按代码升序）。
        """
        try:
            with sqlite3.connect(self.engine.db_path) as conn:
                df = pd.read_sql("SELECT symbol, date, close, high FROM stock_daily", conn)
        except Exception as exc:
            logger.error(f"读取数据库失败: {exc}")
            return []

        if df.empty:
            return []

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["symbol", "date"])

        # 纵向计算涨幅
        df["close_shift"] = df.groupby("symbol")["close"].shift(self.rps_period)
        df["pct_change"] = (df["close"] - df["close_shift"]) / df["close_shift"]

        # 只在同一天里横向比较：取全库最新交易日那一批。
        # 停牌/退市导致最后一行早于该日期的股票会被自然排除。
        latest_date = df["date"].max()
        latest_df = df[df["date"] == latest_date].copy()
        # 显式挡掉除零/空值（后复权价理论上不会是 0，但脏数据不该变成 inf 冲上榜首）
        latest_df = latest_df[(latest_df["close_shift"] > 0) & latest_df["pct_change"].notna()]

        if latest_df.empty:
            return []

        # 横向排位 (RPS)：rank(pct=True) 的值域是 (0, 1]，第一名恰好是 100。
        # 注意分母是「参与排名的样本数」，不是全市场总数。
        latest_df["rps"] = latest_df["pct_change"].rank(pct=True) * 100
        strong_stocks = latest_df[latest_df["rps"] >= self.rps_threshold].copy()

        # 计算滚动最高价
        roll_high = df.groupby("symbol")["high"].rolling(
            window=self.rps_period, min_periods=self.rps_period // 2
        ).max().reset_index(level=0, drop=True)
        df["roll_high"] = roll_high

        latest_roll_high = df[df["date"] == latest_date][["symbol", "roll_high"]]
        strong_stocks = strong_stocks.merge(latest_roll_high, on="symbol")

        # 突破判定：站在 120 日最高价的 90% 以上
        selected = strong_stocks[strong_stocks["close"] >= strong_stocks["roll_high"] * 0.90]

        logger.info(f"RpsBreakoutStrategy 选出 {len(selected)} 只股票")
        return selected["symbol"].tolist()
