"""PushPlus 通知模块：将选股结果通过 PushPlus API 推送至微信/多渠道。

PushPlus API 文档：https://www.pushplus.plus/doc/guide/api.html

推送策略：所有策略先跑完并汇总，最后统一推送，不逐个策略单独发消息。
同一推送目标（token）下的多个策略合并为一条消息；
若所有策略都用全局 token（默认情况），则整轮只产生一条推送。
"""

import json
from collections.abc import Mapping, Sequence
from datetime import date
from typing import NamedTuple

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

# PushPlus 发送接口
PUSHPLUS_API_URL = "https://www.pushplus.plus/send"

# 推送正文里股票链接每行显示多少只（便于在手机上快速扫读）
_STOCKS_PER_LINE = 5

# 策略序号用的带圈数字
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


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

# 正文页脚提示
_FOOTER = "买卖点为策略信号参考，不构成投资建议"


class PushPlusNotifier:
    """PushPlus 推送器。

    汇总所有策略的选股结果后统一推送。
    按策略的 webhook_key 解析出 token 分组，同一 token 的策略合并为一条消息；
    webhook_key 未在 Settings.strategy_webhooks 中配置时，回落到全局 pushplus_token。
    """

    def __init__(self, settings: Settings) -> None:
        """
        初始化 PushPlusNotifier。

        Args:
            settings: Settings 实例，提供 pushplus_token 及策略专属 token 配置。
        """
        self.settings = settings

    # ── 工具方法 ──

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。"""
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith(("4", "8")):
            return f"BJ{code}"
        return f"SZ{code}"

    @staticmethod
    def _load_stock_names() -> dict[str, str]:
        """兜底：一次性拉取全市场股票名称，返回 {code: name} 映射。

        正常情况下名称由 DataEngine 从本地 stock_name 表提供（main.py 传入），
        这里只在没拿到本地名称时兜底，且**只发一次请求**。

        注意不要退化成逐只 query_stock_basic：请求量大时 baostock 会开始返回空结果，
        导致大部分股票只能显示代码。

        Returns:
            {代码: 股票名称}；失败时返回空 dict。
        """
        import baostock as bs

        try:
            lg = bs.login()
        except Exception as exc:
            logger.warning(f"baostock 登录异常，无法获取股票名称：{exc}")
            return {}

        if lg.error_code != "0":
            logger.warning(f"baostock 登录失败，无法获取股票名称：{lg.error_msg}")
            return {}

        mapping: dict[str, str] = {}
        try:
            rs = bs.query_stock_basic(code_name="", code="")
            while rs.next():
                r = rs.get_row_data()
                # 字段顺序：code, code_name, ipoDate, outDate, type, status
                # type != 1 的是指数等；sh.000001(上证综合指数) 与 sz.000001(平安银行)
                # 数字部分相同，必须过滤掉指数，否则会串名。
                if len(r) < 6 or r[4] != "1":
                    continue
                symbol = r[0].split(".")[-1]
                name = (r[1] or "").strip()
                if symbol and name:
                    mapping[symbol] = name
        except Exception as exc:
            logger.warning(f"获取股票名称异常：{exc}")
        finally:
            bs.logout()

        return mapping

    def _resolve_token(self, webhook_key: str) -> str:
        """按 webhook_key 解析推送 token，未配置专属 token 时回落全局 pushplus_token。"""
        return self.settings.strategy_webhooks.get(
            webhook_key.lower(), self.settings.pushplus_token
        )

    def _format_symbols(self, symbols: Sequence[str], names: Mapping[str, str]) -> str:
        """把股票代码渲染成雪球链接，每行固定只数，便于在手机上扫读。

        Args:
            symbols: 股票代码列表。
            names: {代码: 股票名称} 映射，查不到名称时退回显示带前缀的代码。

        Returns:
            多行 Markdown 文本，行内用 ` · ` 分隔。
        """
        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            links.append(f"[{names.get(code, xq_code)}](https://xueqiu.com/S/{xq_code})")
        return "\n".join(
            " · ".join(links[i : i + _STOCKS_PER_LINE])
            for i in range(0, len(links), _STOCKS_PER_LINE)
        )

    def _render_block(
        self,
        index: int,
        strategy_name: str,
        symbols: Sequence[str],
        names: Mapping[str, str],
    ) -> str:
        """渲染单个策略区块：序号 + 中文名（N 只）/ 买点 / 卖点 / 股票列表。

        未在 STRATEGY_DISPLAY 登记的类名原样显示、不带买卖点，
        保证以后新增策略也不会丢内容。

        Args:
            index: 策略序号，从 1 开始（用于前置带圈数字）。
            strategy_name: 策略类名（如 MaVolumeStrategy）。
            symbols: 该策略选出的股票代码列表。
            names: {代码: 股票名称} 映射。

        Returns:
            该策略的 Markdown 区块。
        """
        marker = _CIRCLED[index - 1] if 1 <= index <= len(_CIRCLED) else f"{index}."
        info = STRATEGY_DISPLAY.get(strategy_name)

        if info is None:
            title = f"**{marker} {strategy_name}**（{len(symbols)} 只）"
            rules: list[str] = []
        else:
            title = f"**{marker} {info.name}**（{len(symbols)} 只）"
            rules = [f"🎯 买点：{info.entry}"]
            if info.exit:
                rules.append(f"🛑 卖点：{info.exit}")

        return "\n".join(
            [title, *rules, "", self._format_symbols(symbols, names)]
        )

    # ── 消息构建 ──

    def _build_digest_content(
        self,
        items: Sequence[tuple[str, list[str]]],
        names: dict[str, str],
        data_date: str | None = None,
    ) -> str:
        """生成汇总消息正文（Markdown），每个策略渲染为一个区块。

        排版目标：一眼看清 —— 头部给时效与规模，每个策略带序号，
        买点/卖点各占一行，股票列表每行固定只数。

        Args:
            items: [(策略名, 选股代码列表), ...]，均为有结果的策略。
            names: {代码: 股票名称} 映射。
            data_date: 数据库数据的截止日期（YYYY-MM-DD）。

        Returns:
            Markdown 格式的消息正文。
        """
        today = date.today().strftime("%Y-%m-%d")
        total = sum(len(symbols) for _, symbols in items)

        # 头部概览：数据时效 + 本次规模
        head = f"📅 **{today}**"
        if data_date and data_date != today:
            # 数据没更新到当天时明确标出，避免把旧数据的结果误当成当天选股
            head += f"（⚠️ 数据截止 {data_date}）"
        head += f"\n📊 **{len(items)}** 个策略 · 共 **{total}** 只"

        blocks = [
            self._render_block(index, strategy_name, symbols, names)
            for index, (strategy_name, symbols) in enumerate(items, start=1)
        ]

        return (
            head
            + "\n\n---\n\n"
            + "\n\n---\n\n".join(blocks)
            + f"\n\n---\n\n*{_FOOTER}*"
        )

    # ── 发送 ──

    def _post(self, token: str, title: str, content: str, tag: str) -> None:
        """向 PushPlus API 投递一条消息，失败只记日志、不抛异常。"""
        payload = {
            "token": token,
            "title": title,
            "content": content,
            "template": "markdown",
        }

        try:
            resp = requests.post(
                PUSHPLUS_API_URL,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp_json = resp.json()

            # PushPlus 成功标志：code == 200
            if resp.status_code != 200 or resp_json.get("code") != 200:
                logger.error(
                    f"PushPlus 推送失败 [{tag}] "
                    f"HTTP状态={resp.status_code} PushPlus响应={resp.text}"
                )
            else:
                logger.info(f"PushPlus 推送成功 [{tag}]")
        except requests.RequestException as exc:
            logger.error(f"PushPlus 推送请求异常 [{tag}]：{exc}")

    def send_digest(
        self,
        results: Mapping[str, tuple[list[str], str]],
        names: Mapping[str, str] | None = None,
        data_date: str | None = None,
    ) -> None:
        """汇总所有策略的选股结果后统一推送。

        Args:
            results: {策略名: (选股代码列表, webhook_key)}。
                     无选股结果的策略会被自动过滤，不参与推送。
            names: {代码: 股票名称}，通常由 DataEngine 从本地 stock_name 表提供。
                   传 None 时退化为自行向 baostock 拉一次全市场名称。
            data_date: 数据库数据的截止日期（YYYY-MM-DD），
                       与运行日期不一致时会在消息里标出 ⚠️ 提醒。

        Raises:
            不抛出异常；HTTP 失败时记录 ERROR 日志。
        """
        active: list[tuple[str, list[str], str]] = [
            (name, symbols, key) for name, (symbols, key) in results.items() if symbols
        ]
        if not active:
            logger.info("所有策略均无选股结果，跳过推送")
            return

        # 汇总全部代码（去重，保持策略顺序）
        all_symbols: list[str] = []
        for _, symbols, _ in active:
            for code in symbols:
                if code not in all_symbols:
                    all_symbols.append(code)

        if names is None:
            names = self._load_stock_names()

        missing = [c for c in all_symbols if c not in names]
        if missing:
            logger.warning(
                f"{len(missing)}/{len(all_symbols)} 只股票未匹配到名称，将显示代码"
                f"（示例：{', '.join(missing[:5])}）"
            )

        # 按 token 分组：同一推送目标的策略合并为一条消息
        groups: dict[str, list[tuple[str, list[str]]]] = {}
        for name, symbols, key in active:
            groups.setdefault(self._resolve_token(key), []).append((name, symbols))

        logger.info(
            f"结果汇总完成：{len(active)} 个策略有选股结果，合并为 {len(groups)} 条推送"
        )

        for token, items in groups.items():
            content = self._build_digest_content(items, names, data_date)
            # 日志 tag 保留英文类名便于排查；推送标题用中文概览
            tag = " + ".join(name for name, _ in items)
            total = sum(len(symbols) for _, symbols in items)
            title = f"📈 Sequoia-X 选股播报 · {len(items)} 策略 {total} 只"
            self._post(token, title, content, tag)
