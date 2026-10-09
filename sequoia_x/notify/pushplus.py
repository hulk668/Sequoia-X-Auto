"""PushPlus 通知模块：将选股结果通过 PushPlus API 推送至微信/多渠道。

PushPlus API 文档：https://www.pushplus.plus/doc/guide/api.html

推送策略：所有策略先跑完并汇总，最后统一推送，不逐个策略单独发消息。
同一推送目标（token）下的多个策略合并为一条消息；
若所有策略都用全局 token（默认情况），则整轮只产生一条推送。
"""

import json
from collections.abc import Mapping, Sequence
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

# PushPlus 发送接口
PUSHPLUS_API_URL = "https://www.pushplus.plus/send"


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
    def _get_stock_names(symbols: list[str]) -> dict[str, str]:
        """通过 baostock 批量查询股票名称，返回 {code: name} 映射。

        整轮只登录一次 baostock，避免逐策略重复握手。

        Args:
            symbols: 股票代码列表（纯数字）。

        Returns:
            {代码: 股票名称} 映射；查询失败的代码不会出现在结果中。
        """
        import baostock as bs

        bs.login()
        mapping: dict[str, str] = {}
        try:
            for code in symbols:
                prefix = "sh" if code.startswith(("6", "9")) else "sz"
                rs = bs.query_stock_basic(code=f"{prefix}.{code}")
                while rs.next():
                    row = rs.get_row_data()
                    mapping[code] = row[1]  # 第2个字段是股票名称
        finally:
            bs.logout()
        return mapping

    def _resolve_token(self, webhook_key: str) -> str:
        """按 webhook_key 解析推送 token，未配置专属 token 时回落全局 pushplus_token。"""
        return self.settings.strategy_webhooks.get(
            webhook_key.lower(), self.settings.pushplus_token
        )

    # ── 消息构建 ──

    def _build_digest_content(
        self,
        items: Sequence[tuple[str, list[str]]],
        names: dict[str, str],
    ) -> str:
        """生成汇总消息正文（Markdown），每个策略渲染为一段。

        Args:
            items: [(策略名, 选股代码列表), ...]，均为有结果的策略。
            names: {代码: 股票名称} 映射。

        Returns:
            Markdown 格式的消息正文。
        """
        today = date.today().strftime("%Y-%m-%d")
        total = sum(len(symbols) for _, symbols in items)

        blocks: list[str] = []
        for strategy_name, symbols in items:
            links: list[str] = []
            for code in symbols:
                xq_code = self._to_xueqiu_code(code)
                name = names.get(code, xq_code)
                links.append(f"[{name}](https://xueqiu.com/S/{xq_code})")
            blocks.append(f"**{strategy_name}**（{len(symbols)} 只）\n" + " ".join(links))

        return (
            f"**日期：** {today}\n"
            f"**策略数：** {len(items)}\n"
            f"**选股合计：** {total} 只\n"
            f"---\n\n" + "\n\n".join(blocks)
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

    def send_digest(self, results: Mapping[str, tuple[list[str], str]]) -> None:
        """汇总所有策略的选股结果后统一推送。

        Args:
            results: {策略名: (选股代码列表, webhook_key)}。
                     无选股结果的策略会被自动过滤，不参与推送。

        Raises:
            不抛出异常；HTTP 失败时记录 ERROR 日志。
        """
        active: list[tuple[str, list[str], str]] = [
            (name, symbols, key) for name, (symbols, key) in results.items() if symbols
        ]
        if not active:
            logger.info("所有策略均无选股结果，跳过推送")
            return

        # 汇总全部代码，只查一次股票名称
        all_symbols: list[str] = []
        for _, symbols, _ in active:
            for code in symbols:
                if code not in all_symbols:
                    all_symbols.append(code)
        names = self._get_stock_names(all_symbols)

        # 按 token 分组：同一推送目标的策略合并为一条消息
        groups: dict[str, list[tuple[str, list[str]]]] = {}
        for name, symbols, key in active:
            groups.setdefault(self._resolve_token(key), []).append((name, symbols))

        logger.info(
            f"结果汇总完成：{len(active)} 个策略有选股结果，合并为 {len(groups)} 条推送"
        )

        for token, items in groups.items():
            content = self._build_digest_content(items, names)
            strategy_names = " + ".join(name for name, _ in items)
            title = f"📈 Sequoia-X 选股播报 | 共 {len(items)} 个策略"
            self._post(token, title, content, strategy_names)
