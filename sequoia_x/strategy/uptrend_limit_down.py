"""上升趋势跌停策略：趋势中放量跌停，捕捉错杀机会。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy, is_limit_down

logger = get_logger(__name__)


class UptrendLimitDownStrategy(BaseStrategy):
    """上升趋势跌停策略。

    选股条件（向量化，严禁 iterrows）：
    1. 处于上升趋势：昨日20日均线 > 昨日60日均线
    2. 放量跌停：今日**跌停**（按板块幅度判定，见 `base.is_limit_down`）
                且今日 volume > **前20日**（不含今日）均量的 2.0 倍

    """

    # 需要 61 根而不是 60 根：趋势要比较「昨日」的 ma20/ma60，
    # 昨日那一行也要有完整的 60 日窗口（否则 ma60 是 NaN，比较恒为 False）；
    # 均量改用 shift(1).rolling(20) 后也要 61 根。
    _MIN_BARS: int = 61

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足上升趋势跌停条件的股票代码列表。

        Returns:
            满足条件的股票代码列表。
        """
        selected: list[str] = []

        for symbol, df in self.bars_by_symbol().items():
            try:
                if len(df) < self._MIN_BARS:
                    continue

                # 向量化计算均线
                df["ma20"] = df["close"].rolling(20).mean()
                df["ma60"] = df["close"].rolling(60).mean()
                # shift(1) 把窗口推到「今日之前」，今日的量不参与自己的基准
                df["vol_ma20"] = df["volume"].shift(1).rolling(20).mean()

                prev = df.iloc[-2]  # 昨日
                today = df.iloc[-1]  # 今日

                if pd.isna(prev["ma20"]) or pd.isna(prev["ma60"]) or pd.isna(today["vol_ma20"]):
                    continue

                # 条件 1：上升趋势（昨日均线多头排列）
                uptrend = prev["ma20"] > prev["ma60"]
                # 条件 2：放量跌停
                limit_down = is_limit_down(symbol, today["close"], prev["close"])
                volume_surge = today["volume"] > today["vol_ma20"] * 2.0

                if uptrend and limit_down and volume_surge:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] UptrendLimitDownStrategy 计算失败：{exc}")
                continue

        logger.info(f"UptrendLimitDownStrategy 选出 {len(selected)} 只股票")
        return selected
