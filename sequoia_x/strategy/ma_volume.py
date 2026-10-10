"""均线+成交量选股策略：5日均线上穿20日均线且成交量放大。"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class MaVolumeStrategy(BaseStrategy):
    """均线+成交量选股策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. 5日收盘均线上穿20日收盘均线（金叉）
    2. 当日成交量 > **前20日**（不含今日）均量的 1.5 倍（放量确认）

    均量一律取「今日之前」的 20 天：让今日的量混进它自己的基准里，
    条件会变成一个绕圈的隐式不等式（等价于要求 `今日量 > 前19日均量 × 1.54`），
    和其它策略的写法也对不上。

    Attributes:
        webhook_key: 路由到 'ma_volume' 专属推送 token。
    """

    webhook_key: str = "ma_volume"
    # 需要 21 根而不是 20 根：金叉要比较「昨日」的 ma5/ma20，昨日那一行
    # 也要有完整的 20 日窗口（否则 ma20 是 NaN，比较恒为 False）。
    # 均量改用 shift(1).rolling(20) 后同样需要 21 根。
    _MIN_BARS: int = 21

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足均线金叉+放量条件的股票代码列表。

        Returns:
            满足条件的股票代码列表。
        """
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                # 向量化计算均线和成交量均值
                df["ma5"] = df["close"].rolling(5).mean()
                df["ma20"] = df["close"].rolling(20).mean()
                # shift(1) 把窗口整体推到「今日之前」，今日的量不参与自己的基准
                df["vol_ma20"] = df["volume"].shift(1).rolling(20).mean()

                # 取最后两行判断金叉（昨日 ma5 < ma20，今日 ma5 > ma20）
                last = df.iloc[-1]
                prev = df.iloc[-2]

                golden_cross = (
                    prev["ma5"] < prev["ma20"]
                    and last["ma5"] > last["ma20"]
                )
                volume_surge = last["volume"] > last["vol_ma20"] * 1.5

                if golden_cross and volume_surge:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] 策略计算失败：{exc}")
                continue

        logger.info(f"MaVolumeStrategy 选出 {len(selected)} 只股票")
        return selected
