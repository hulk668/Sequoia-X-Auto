"""涨停洗盘策略：昨日涨停后今日放量收阴但不破昨收。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy, is_limit_up

logger = get_logger(__name__)


class LimitUpShakeoutStrategy(BaseStrategy):
    """涨停洗盘策略。

    选股条件（向量化，严禁 iterrows）：
    1. 昨日涨停：按**板块幅度**判定（主板 10% / 创业板·科创板 20% / 北交所 30%，
       见 `base.is_limit_up`），不再用一刀切的 9.5% 近似；
    2. 今日收阴：今日 close < 今日 open
    3. 今日放量：今日 volume > **前20日**（不含今日）均量的 2.0 倍
    4. 支撑不破：今日 low >= 昨日 close

    条件 3 原本是「今日量 > 昨日量 × 2」。但昨日是涨停日、本身往往已经爆量，
    在它基础上再翻倍把交集压到只剩 3%，几乎选不出票；改用 20 日均量后
    与「放量」的常规含义一致，也和其它策略的口径统一。

    """

    # 至少 3 根 K 线（前日、昨日、今日）；下面的均量窗口不足时自然为 NaN，
    # 会被显式判空跳过，不影响 3 根即可入场的前两条条件。
    _MIN_BARS: int = 3

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足涨停洗盘条件的股票代码列表。

        Returns:
            满足条件的股票代码列表。
        """
        selected: list[str] = []

        for symbol, df in self.bars_by_symbol().items():
            try:
                if len(df) < self._MIN_BARS:
                    continue

                # 前20日均量（不含今日）。K 线不足 21 根时为 NaN → 条件 3 不成立
                vol_ma20 = df["volume"].shift(1).rolling(20).mean()
                vol_base = vol_ma20.iloc[-1]

                # 取最近三根 K 线（向量化索引，无 iterrows）
                prev2 = df.iloc[-3]  # 前日
                prev1 = df.iloc[-2]  # 昨日
                today = df.iloc[-1]  # 今日

                # 条件 1：昨日涨停（按该股所在板块的涨跌幅上限判定）
                limit_up_yesterday = is_limit_up(
                    symbol, prev1["close"], prev2["close"]
                )
                # 条件 2：今日收阴
                bearish_today = today["close"] < today["open"]
                # 条件 3：今日放量（对比前20日均量）
                volume_surge = pd.notna(vol_base) and today["volume"] > vol_base * 2.0
                # 条件 4：支撑不破
                support_hold = today["low"] >= prev1["close"]

                if limit_up_yesterday and bearish_today and volume_surge and support_hold:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] LimitUpShakeoutStrategy 计算失败：{exc}")
                continue

        logger.info(f"LimitUpShakeoutStrategy 选出 {len(selected)} 只股票")
        return selected
