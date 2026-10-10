"""定增公告监控策略：推送最近发布的定向增发公告。"""

from datetime import date, timedelta

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class PrivatePlacementStrategy(BaseStrategy):
    """定增公告监控策略。

    数据源：akshare stock_qbzf_em()（东方财富-全部增发）
    逻辑：筛选最近 7 天内发行日期的定向增发公告，纳入当轮汇总推送。
    """

    _LOOKBACK_DAYS: int = 7  # 回看天数，覆盖一周内的新公告

    def run(self) -> list[str]:
        """拉取定增公告，返回近期有定向增发的股票代码列表。"""
        try:
            import akshare as ak

            df = ak.stock_qbzf_em()
        except Exception as exc:
            logger.error(f"PrivatePlacementStrategy 获取定增数据失败：{exc}")
            return []

        if df is None or df.empty:
            logger.info("PrivatePlacementStrategy 无定增数据")
            return []

        # 解析阶段的每一步都依赖 akshare 的列名（发行方式/发行日期/股票代码），
        # 上游一改字段就是 KeyError。必须一起兜住 —— 否则异常会冒出 run()，
        # 被 main.py 的外层 except 接走并 sys.exit(1)，整轮推送全丢。
        try:
            return self._parse(df)
        except Exception as exc:
            logger.error(
                f"PrivatePlacementStrategy 解析定增数据失败（上游字段可能已变化）：{exc}"
            )
            return []

    def _parse(self, df) -> list[str]:
        """从原始 DataFrame 里筛出近 N 天的定向增发公告，返回去重后的代码列表。"""
        # 只保留定向增发（排除公开增发）
        df = df[df["发行方式"] == "定向增发"]

        if df.empty:
            return []

        # 按发行日期过滤：只保留最近 N 天内的公告
        today = date.today()
        cutoff = today - timedelta(days=self._LOOKBACK_DAYS)

        df["发行日期"] = pd.to_datetime(df["发行日期"], errors="coerce")
        df = df.dropna(subset=["发行日期"])
        df = df[df["发行日期"].dt.date >= cutoff]

        if df.empty:
            logger.info("PrivatePlacementStrategy 近期无新定增公告")
            return []

        # 按发行日期降序（最新的在前）
        df = df.sort_values("发行日期", ascending=False)

        # 提取股票代码（去掉可能的前缀，保留纯数字）
        symbols = df["股票代码"].astype(str).str.extract(r"(\d{6})")[0].dropna().tolist()

        # 去重（同一只票可能有多次定增）
        seen = set()
        unique_symbols = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                unique_symbols.append(s)

        logger.info(f"PrivatePlacementStrategy 选出 {len(unique_symbols)} 只股票")
        return unique_symbols
