"""PushPlus 通知模块：将选股结果通过 PushPlus API 推送至微信/多渠道。

PushPlus API 文档：https://www.pushplus.plus/doc/guide/api.html
"""

import json
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

# PushPlus 发送接口
PUSHPLUS_API_URL = "https://www.pushplus.plus/send"


class PushPlusNotifier:
    """PushPlus 推送器。

    根据策略的 webhook_key 路由到对应的推送目标。
    若 webhook_key 未在 Settings.strategy_webhooks 中配置，
    则 fallback 到 default，使用全局 pushplus_token 推送。
    """

    def __init__(self, settings: Settings) -> None:
        """
        初始化 PushPlusNotifier。

        Args:
            settings: Settings 实例，提供 pushplus_token 配置。
        """
        self.settings = settings

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
        """通过 baostock 批量查询股票名称，返回 {code: name} 映射。"""
        import baostock as bs
        bs.login()
        mapping = {}
        for code in symbols:
            prefix = "sh" if code.startswith(("6", "9")) else "sz"
            rs = bs.query_stock_basic(code=f"{prefix}.{code}")
            while rs.next():
                row = rs.get_row_data()
                mapping[code] = row[1]  # 第2个字段是股票名称
        bs.logout()
        return mapping

    def _build_content(self, symbols: list[str], strategy_name: str) -> str:
        """生成 PushPlus 消息正文（Markdown 格式）。"""
        today = date.today().strftime("%Y-%m-%d")
        names = self._get_stock_names(symbols)

        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            name = names.get(code, xq_code)
            links.append(f"[{name}](https://xueqiu.com/S/{xq_code})")

        symbol_text = " ".join(links) if links else "（无选股结果）"

        return (
            f"**日期：** {today}\n"
            f"**策略：** {strategy_name}\n"
            f"**选股数量：** {len(symbols)}\n"
            f"---\n"
            f"**选股列表：**\n{symbol_text}"
        )

    def send(
        self,
        symbols: list[str],
        strategy_name: str,
        webhook_key: str = "default",
    ) -> None:
        """
        将选股结果格式化为 Markdown 消息并 POST 至 PushPlus API。

        根据 webhook_key 从 Settings 中查找专属 token；
        若未配置，则 fallback 到全局 pushplus_token。

        Args:
            symbols: 选股结果代码列表。
            strategy_name: 策略名称，用于消息标题。
            webhook_key: 策略标识，用于路由到对应推送目标。

        Raises:
            不抛出异常，HTTP 失败时记录 ERROR 日志。
        """
        # 根据 webhook_key 路由：配置了专属 token 则用专属，否则用全局 pushplus_token
        token = self.settings.strategy_webhooks.get(
            webhook_key.lower(), self.settings.pushplus_token
        )
        title = f"📈 Sequoia-X 选股播报 | {strategy_name}"
        content = self._build_content(symbols, strategy_name)

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
                    f"PushPlus 推送失败 [{webhook_key}] "
                    f"HTTP状态={resp.status_code} PushPlus响应={resp.text}"
                )
            else:
                logger.info(f"PushPlus 推送成功 [{webhook_key}]，共 {len(symbols)} 只股票")

        except requests.RequestException as exc:
            logger.error(f"PushPlus 推送请求异常 [{webhook_key}]：{exc}")