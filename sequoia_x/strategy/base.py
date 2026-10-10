"""策略基类模块：定义所有选股策略的抽象接口。"""

from abc import ABC, abstractmethod

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine

# ── 涨跌停幅度 ──
#
# A 股不同板块的日涨跌幅上限不一样，用同一个阈值判断「涨停/跌停」必然出错：
# 拿 10% 的阈值去套创业板，一只「涨 12%」的普通票会被误判成「涨停」。
# 下面按代码前缀区分（ST 股 ±5% 无法从代码判断，只能忽略）。
_LIMIT_MAIN = 0.10  # 沪市 60x / 深市 000·001·002·003
_LIMIT_20 = 0.20    # 创业板 300·301 / 科创板 688·689
_LIMIT_30 = 0.30    # 北交所 4xx·8xx
# 判定涨跌停时留的余量。交易所的涨跌停价是**四舍五入到分**的，
# 低价股的比值会略低于名义幅度（如 3.33 → 3.66 只有 9.91%）。
_LIMIT_TOL = 0.005


def price_limit_pct(symbol: str) -> float:
    """返回该代码所属板块的日涨跌幅上限（小数，0.10 表示 ±10%）。

    规则：
    - 创业板（300/301）、科创板（688/689）：±20%
    - 北交所（4/8 开头）：±30%
    - 其余（沪市 60x、深市 000/001/002/003）：±10%

    Args:
        symbol: 纯数字股票代码。

    Returns:
        涨跌幅上限，如 0.10。
    """
    if symbol.startswith(("300", "301", "688", "689")):
        return _LIMIT_20
    if symbol.startswith(("4", "8")):
        return _LIMIT_30
    return _LIMIT_MAIN


def is_limit_up(symbol: str, close: float, prev_close: float) -> bool:
    """按板块幅度判断「今日涨停」。

    用**涨跌幅比例**而不是「等于涨停价」来判断：库里存的是后复权价，
    绝对价格没有意义，但相邻两日的比值与真实涨跌幅一致
    （除非当天正好除权除息，这是本方法的已知局限）。
    """
    if prev_close <= 0:
        return False
    return close >= prev_close * (1 + price_limit_pct(symbol) - _LIMIT_TOL)


def is_limit_down(symbol: str, close: float, prev_close: float) -> bool:
    """按板块幅度判断「今日跌停」（与 is_limit_up 对称）。"""
    if prev_close <= 0:
        return False
    return close <= prev_close * (1 - price_limit_pct(symbol) + _LIMIT_TOL)


class BaseStrategy(ABC):
    """选股策略抽象基类。

    所有具体策略必须继承此类并实现 run() 方法。

    Attributes:
        webhook_key: 策略对应的推送路由标识，用于把不同策略的结果推到不同 token。
            默认为 'default'，将使用全局 Settings.pushplus_token。
            子类可覆盖此属性以路由到专属 token，例如 'ma_volume'。
    """

    webhook_key: str = "default"

    #: 策略计算所需的最少 K 线根数。子类按自己最长的一个窗口覆盖。
    _MIN_BARS: int = 1

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        """
        初始化策略。

        Args:
            engine: DataEngine 实例，用于读取行情数据。
            settings: Settings 实例，用于读取配置。
        """
        self.engine = engine
        self.settings = settings

    def bars_by_symbol(self) -> dict[str, pd.DataFrame]:
        """取全市场 K 线快照，返回 {代码: K 线}。

        **所有策略都应该走这个方法取数，不要自己逐只查库。**
        引擎内部只加载一次（`DataEngine.market_panel()`），7 个策略共享同一份 ——
        逐只查库时每个策略都要把 5000+ 只票捞一遍，实测光取数就 100 秒往上。

        Returns:
            {代码: DataFrame}，按日期升序、索引已重置；
            只含全库最新交易日当天有行情的代码。
        """
        return self.engine.market_panel()

    @abstractmethod
    def run(self) -> list[str]:
        """
        执行选股逻辑，返回选中的股票代码列表。

        Returns:
            满足策略条件的股票代码列表，如 ['000001', '600519']。
            无选股结果时返回空列表。
        """
        ...
