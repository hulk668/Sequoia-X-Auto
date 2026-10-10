"""RPS 极强动量突破策略：120日涨幅排全市场前 10%，且股价接近 120 日新高。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 极强动量突破策略。

    选股条件：
    1. RPS >= 90：120 个交易日涨幅在全市场排进前 10%（横截面百分位）；
    2. 接近新高：最新收盘价 >= 过去 120 日最高价的 90%。

    与其它策略不同，这里是**横截面**策略（需要全市场一起排序）。
    但「120 日涨幅」和「120 日滚动最高价」都是**单只票自己的**序列计算，
    横截面只发生在最后一步排名上 —— 所以照样能用共享的全市场快照，
    逐只算出两个标量再统一排名即可，没必要把整张表读成一个长表。

    Attributes:
        webhook_key: 路由到 'rps' 专属推送 token。
        rps_period: 计算涨幅的回看交易日数。
        rps_threshold: RPS 阈值（百分位）。
    """

    webhook_key: str = "rps"
    rps_period: int = 120
    rps_threshold: int = 90
    # 需要 121 根：`shift(120)` 要取到 120 根之前的那一天
    _MIN_BARS: int = 121

    def run(self) -> list[str]:
        """返回满足 RPS 动量条件、且股价接近 120 日新高的股票代码列表。

        Returns:
            纯数字股票代码列表（按代码升序）。
        """
        panel = self.bars_by_symbol()
        if not panel:
            return []

        period = self.rps_period
        # 与原先 rolling(120, min_periods=60) 的口径对齐
        min_periods = period // 2

        # 第 1 步：逐只算两个标量 —— 120 日涨幅、120 日区间最高价
        rows: list[tuple[str, float, float, float]] = []
        for symbol, df in panel.items():
            # 不足 121 根时拿不到 120 根之前的基准，原实现同样会把这类票判成 NaN 丢掉
            if len(df) <= period:
                continue
            base = df["close"].iloc[-1 - period]
            last_close = df["close"].iloc[-1]
            # 显式挡掉除零与空值（后复权价理论上不会是 0，但脏数据不该变成 inf 冲上榜首）
            if pd.isna(base) or pd.isna(last_close) or base <= 0:
                continue

            window = df["high"].iloc[-period:]
            if len(window) < min_periods:
                continue
            roll_high = window.max()
            if pd.isna(roll_high):
                continue

            rows.append(
                (symbol, float(last_close), float(roll_high),
                 float((last_close - base) / base))
            )

        if not rows:
            return []

        # 第 2 步：横截面排名。rank(pct=True) 值域是 (0, 1]，第一名恰好是 100。
        # 注意分母是「参与排名的样本数」，不是全市场总数。
        frame = pd.DataFrame(rows, columns=["symbol", "close", "roll_high", "pct_change"])
        frame["rps"] = frame["pct_change"].rank(pct=True) * 100
        strong = frame[frame["rps"] >= self.rps_threshold]

        # 第 3 步：突破判定 —— 站在 120 日最高价的 90% 以上
        selected = strong[strong["close"] >= strong["roll_high"] * 0.90]

        logger.info(f"RpsBreakoutStrategy 选出 {len(selected)} 只股票")
        return selected["symbol"].tolist()
