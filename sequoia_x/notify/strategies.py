"""策略展示信息与股票链接工具 —— 通知层共用。

把「策略类名 → 中文名 / 买点 / 卖点」这张表，以及股票链接的生成规则放在这里，
是为了让通知层有个单一事实来源：改中文名或买卖点文案，只改这一个文件。
"""

from typing import NamedTuple


def to_xueqiu_code(code: str) -> str:
    """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。"""
    if code.startswith("6"):
        return f"SH{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SZ{code}"


def xueqiu_url(code: str) -> str:
    """股票代码 → 雪球行情页 URL。"""
    return f"https://xueqiu.com/S/{to_xueqiu_code(code)}"


class StrategyInfo(NamedTuple):
    """策略的中文展示信息。

    Attributes:
        name: 中文名。
        entry: 买点（严格对应策略源码里的入选条件）。
        exit: 卖点（该策略对应的经典退出规则，仅供参考）。
    """

    name: str
    entry: str
    exit: str = ""


# 策略序号用的带圈数字
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# 正文页脚提示
FOOTER = "板块为东财行业分类；买卖点为策略信号参考，不构成投资建议"


# 策略展示信息：类名 → 展示信息。
#
# 买点严格对应各策略源码里的入选条件，收到推送时无需回看代码即可看懂信号含义。
# 卖点是该策略对应的**经典退出规则**，只作为参考提示 —— 系统只负责选股，
# 不跟踪持仓、不会推送卖出提醒，仓位同样需要自己管理。
# 新增策略时在此登记；未登记的类名会原样显示类名、不带买卖点。
STRATEGY_DISPLAY: dict[str, StrategyInfo] = {
    "MaVolumeStrategy": StrategyInfo(
        "均线金叉+放量突破",
        "5日线上穿20日线，且成交量放大到20日均量的1.5倍",
        "跌破20日线，或5日线下穿20日线（死叉）",
    ),
    "TurtleTradeStrategy": StrategyInfo(
        "海龟突破新高",
        "突破近20日最高价，成交额超1亿、收阳且真涨",
        "跌破10日最低价（海龟经典退出）",
    ),
    "HighTightFlagStrategy": StrategyInfo(
        "高位旗形缩量",
        "40日大涨后近10日缩量窄幅横盘，且不跌破高位",
        "跌破旗形整理下沿，或跌破20日线",
    ),
    "LimitUpShakeoutStrategy": StrategyInfo(
        "涨停次日洗盘",
        "昨日涨停、今日放量收阴但不破昨收，洗盘不破位",
        "跌破涨停日收盘价（支撑失守）",
    ),
    "UptrendLimitDownStrategy": StrategyInfo(
        "上升趋势跌停错杀",
        "20日线上穿60日线走多头，今日放量跌停，博错杀反抽",
        "反弹回补跌停缺口后离场；跌破60日线止损",
    ),
    "RpsBreakoutStrategy": StrategyInfo(
        "RPS极强动量",
        "120日涨幅排全市场前10%，且股价接近120日新高",
        "RPS 跌破 90，或跌破20日线",
    ),
    "PrivatePlacementStrategy": StrategyInfo(
        "定增公告监控",
        "近7日发布定向增发公告",
        "无固定卖点（事件驱动，需自行判断）",
    ),
}
