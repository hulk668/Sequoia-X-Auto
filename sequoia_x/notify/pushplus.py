"""PushPlus 通知模块：将选股结果通过 PushPlus API 推送至微信/多渠道。

PushPlus API 文档：https://www.pushplus.plus/doc/guide/api.html

推送策略：所有策略先跑完并汇总，最后统一推送，不逐个策略单独发消息。
同一推送目标（token）下的多个策略合并为一条消息；
若所有策略都用全局 token（默认情况），则整轮只产生一条推送。

本模块只负责「渲染 + 发送」，不主动拉取行情/名称/题材数据 ——
那些都由 DataEngine 提供并作为参数传入，避免通知层意外打到行情接口。

🔴 **排版必须遵守 PushPlus 的 markdown 方言**（实测结论，改排版前先看这段）：

- 模板走的是标准 **GFM**：**单个 `\\n` 会被折叠成空格**（软换行）。
  所以「靠 `\\n` 分行」是无效的 —— 之前用 `\\n` 拼的「每行 N 只」从未生效，
  实际渲染出来是一整段，由渲染器在随机位置折行，会把一只股票名劈成两半。
- 真正能换行的只有三种：**空行 `\\n\\n`**（起新段落）、**`---`**、**列表项 `- `**。
- `**加粗**`、`[链接](url)`、`---` 都正常渲染，说明是标准解析器。
- 对齐只能靠 `U+3000` 全角空格 / `U+00A0` 不换行空格 —— ASCII 空格在 HTML 里会被折叠。

因此正文结构固定为：
    标题段落 \\n\\n 买点段落 \\n\\n 卖点段落 \\n\\n 板块列表（每个板块一个 `- ` 列表项）

板块口径：**东财 EM2016 行业分类的第二级**（由 DataEngine.get_boards() 提供），
不是「概念题材」—— 后者会把 `一带一路`、`央国企改革` 这类泛主题排在前面，
实测把招商南油标成「一带一路」，而它的真实行业是「港口航运」。
"""

import json
from collections.abc import Mapping, Sequence
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.notify.strategies import (
    CIRCLED as _CIRCLED,
    FOOTER as _FOOTER,
    STRATEGY_DISPLAY,
    to_xueqiu_code as _to_xueqiu_code,
)

logger = get_logger(__name__)

# PushPlus 发送接口
PUSHPLUS_API_URL = "https://www.pushplus.plus/send"

# ── 排版硬约束 ──
#
# 🔴 PushPlus 的 markdown 模板走的是标准 GFM：**单个 `\n` 会被折叠成空格**
#    （软换行），只有 `\n\n`（新段落）、`---`、以及**列表项 `- `** 才会真正换行。
#    实测证据：`📅 日期\n📊 N 只` 渲染出来挤在同一行；而 `**加粗**`、`[链接]()`、
#    `---` 都正常 —— 所以这里一律用「列表项」来强制换行，
#    绝不能再指望靠 `\n` 分行（早期版本就是这么在随机位置劈开股票名的）。
#
# 一只都没有行业数据时的兜底排版：每个列表项里放几只（没有「（板块）」尾巴，可以多放）
_PLAIN_PER_LINE = 5
# 只有 1 只的板块合并成一行，每行放几只（带「（板块）」尾巴，所以比上面少）
_SINGLES_PER_LINE = 2
# 板块列的最大宽度（半角单位）。超过就不再补空格，否则个股会被推到屏幕外
_MAX_BOARD_COL = 16
# 同一板块内个股之间的分隔符（顿号，比 ` · ` 更像中文里的并列）
_SEP = "、"
# 全角空格（2 个半角宽）与不换行空格（1 个半角宽）。
# HTML 只折叠 ASCII 空白，U+3000 / U+00A0 会被原样保留 —— 靠它们才能真正对齐。
_PAD_WIDE = "\u3000"
_PAD_HALF = "\u00a0"


def _display_width(text: str) -> int:
    """按**半角单位**估算显示宽度：汉字/全角字符 = 2，半角 = 1。

    用来给板块列补空格。不按 len() 算是因为 `REITs`（5 个半角）与
    `储能`（2 个汉字）字符数差一倍但显示宽度相同，照 len() 补会歪掉。
    """
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad_to(width: int) -> str:
    """生成宽度正好为 `width` 个半角单位的填充串（不足时用不换行空格补齐）。"""
    if width <= 0:
        return ""
    return _PAD_WIDE * (width // 2) + _PAD_HALF * (width % 2)


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
        """将纯数字代码转为雪球格式（模块级 `_to_xueqiu_code` 的兼容入口）。"""
        return _to_xueqiu_code(code)

    def _resolve_token(self, webhook_key: str) -> str:
        """按 webhook_key 解析推送 token，未配置专属 token 时回落全局 pushplus_token。"""
        return self.settings.strategy_webhooks.get(
            webhook_key.lower(), self.settings.pushplus_token
        )

    def _link(self, code: str, names: Mapping[str, str]) -> str:
        """把代码渲染成一个雪球链接，链接文本优先用股票名称。"""
        xq_code = self._to_xueqiu_code(code)
        return f"[{names.get(code, xq_code)}](https://xueqiu.com/S/{xq_code})"

    def _link_with_board(
        self, code: str, names: Mapping[str, str], board: str
    ) -> str:
        """`[名称](url)（板块）`；没有板块数据时不带尾巴。"""
        link = self._link(code, names)
        return f"{link}（{board}）" if board else link

    def _format_boards(
        self,
        symbols: Sequence[str],
        names: Mapping[str, str],
        boards: Mapping[str, str],
    ) -> str:
        """**按行业板块**排版：有 2 只以上的板块各占一行，单只板块压成一行。

        排版目标是一眼看分清「这是哪只票」和「它属于哪个板块」：

        - 板块名**加粗**放行首，个股是可点链接 → 靠字重与颜色分层
        - 板块名后面补空格，个股从同一列开始 → 竖着扫一眼就是一张表
        - 板块按只数从多到少排 → 本次选股集中在哪几个板块一眼可见
        - **只有 1 只的板块合并成若干行**，写成 `名称（板块）`：实测 90 只票散在
          42 个板块里、其中 22 个只有 1 只，若让每只都独占一行，页面会拉长近一倍，
          而那一行只服务于一只票；合并后板块信息一点不丢
        - 查不到行业的股票混在合并行里（不带尾巴），固定排在最后
        - 一只都没有行业数据时退回纯列表（每行多放几只，因为不带尾巴）
        - 一行放不下时**交给渲染器折行**：列表项自带悬挂缩进，
          折下来的部分仍在同一项内，不会看起来像别的东西

        Args:
            symbols: 股票代码列表。
            names: {代码: 股票名称} 映射。
            boards: {代码: 行业板块} 映射，缺项即视为无板块。

        Returns:
            Markdown 列表文本。
        """
        # 首次出现顺序建组
        groups: dict[str, list[str]] = {}
        seen: list[str] = []
        for code in symbols:
            board = boards.get(code, "")
            if board not in groups:
                groups[board] = []
                seen.append(board)
            groups[board].append(code)

        has_board = any(seen)
        # 多只板块按只数降序，稳定排序（同只数保持首次出现顺序）
        big = sorted(
            (b for b in seen if b and len(groups[b]) >= 2),
            key=lambda b: -len(groups[b]),
        )
        # 单只板块按名称排序，读起来有秩序
        singles = sorted(b for b in seen if b and len(groups[b]) == 1)

        # 没有行业数据时每行多放几只：不带「（板块）」尾巴，单只短得多
        per_line = _SINGLES_PER_LINE if has_board else _PLAIN_PER_LINE

        # 板块列宽 = 最长板块名（半角单位），设上限避免超长板块名把个股推到屏幕外。
        # +2 是板块名与个股之间那个全角空格，个股因此全部落在同一列上。
        target = min(
            _MAX_BOARD_COL, max((_display_width(b) for b in big), default=0)
        ) + 2

        lines: list[str] = []
        for board in big:
            # 个股起始列。正常情况下是 target；板块名超长时退化成「板块名 + 一个全角空格」，
            # 保证板块名和个股之间至少有一个空格，不会贴在一起。
            start = max(_display_width(board) + 2, target)
            head = f"**{board}**" + _pad_to(start - _display_width(board))
            stocks = _SEP.join(self._link(c, names) for c in groups[board])
            lines.append(f"- {head}{stocks}")

        # 单只板块 + 无板块，统一按行打包
        flat = [
            self._link_with_board(groups[b][0], names, b) for b in singles
        ]
        flat += [self._link(c, names) for c in groups.get("", [])]
        lines += [
            "- " + _SEP.join(flat[i : i + per_line])
            for i in range(0, len(flat), per_line)
        ]

        return "\n".join(lines)

    def _render_block(
        self,
        index: int,
        strategy_name: str,
        symbols: Sequence[str],
        names: Mapping[str, str],
        boards: Mapping[str, str],
    ) -> str:
        """渲染单个策略区块：序号 + 中文名（N 只）/ 买点 / 卖点 / 板块列表。

        用 `\\n\\n`（空行）分段 —— 单 `\\n` 会被 GFM 折叠成空格，
        标题、买点、卖点会挤成一坨（见文件头对 GFM 的说明）。

        未在 STRATEGY_DISPLAY 登记的类名原样显示、不带买卖点，
        保证以后新增策略也不会丢内容。

        Args:
            index: 策略序号，从 1 开始（用于前置带圈数字）。
            strategy_name: 策略类名（如 MaVolumeStrategy）。
            symbols: 该策略选出的股票代码列表。
            names: {代码: 股票名称} 映射。
            boards: {代码: 行业板块} 映射。

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

        return "\n\n".join(
            [title, *rules, self._format_boards(symbols, names, boards)]
        )

    # ── 消息构建 ──

    def _build_digest_content(
        self,
        items: Sequence[tuple[str, list[str]]],
        names: Mapping[str, str],
        boards: Mapping[str, str],
        data_date: str | None = None,
    ) -> str:
        """生成汇总消息正文（Markdown），每个策略渲染为一个区块。

        排版目标：一眼看清 —— 头部给时效与规模，每个策略带序号，
        买点/卖点各占一行，股票列表按行业板块归类、板块名与个股分列对齐。

        Args:
            items: [(策略名, 选股代码列表), ...]，均为有结果的策略。
            names: {代码: 股票名称} 映射。
            boards: {代码: 行业板块} 映射。
            data_date: 数据库数据的截止日期（YYYY-MM-DD）。

        Returns:
            Markdown 格式的消息正文。
        """
        today = date.today().strftime("%Y-%m-%d")
        total = sum(len(symbols) for _, symbols in items)

        # 头部概览：数据时效 + 本次规模。
        # 这里故意用单 `\n`：GFM 会折叠成空格，两行并成一句摘要，反而更紧凑。
        head = f"📅 **{today}**"
        if data_date and data_date != today:
            # 数据没更新到当天时明确标出，避免把旧数据的结果误当成当天选股
            head += f"（⚠️ 数据截止 {data_date}）"
        head += f"\n📊 **{len(items)}** 个策略 · 共 **{total}** 只"

        blocks = [
            self._render_block(index, strategy_name, symbols, names, boards)
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
        boards: Mapping[str, str] | None = None,
    ) -> None:
        """汇总所有策略的选股结果后统一推送。

        Args:
            results: {策略名: (选股代码列表, webhook_key)}。
                     无选股结果的策略会被自动过滤，不参与推送。
            names: {代码: 股票名称}，由 DataEngine.get_stock_names() 提供。
            data_date: 数据库数据的截止日期（YYYY-MM-DD），
                       与运行日期不一致时会在消息里标出 ⚠️ 提醒。
            boards: {代码: 行业板块}，由 DataEngine.get_boards() 提供；
                    为 None 或空 dict 时推送不带板块后缀。

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
            names = {}
        if boards is None:
            boards = {}

        missing = [c for c in all_symbols if c not in names]
        if missing:
            logger.warning(
                f"{len(missing)}/{len(all_symbols)} 只股票未匹配到名称，将显示代码"
                f"（示例：{', '.join(missing[:5])}）"
            )
        logger.info(
            f"板块信息覆盖 {sum(1 for c in all_symbols if c in boards)}/{len(all_symbols)} 只"
        )

        # 按 token 分组：同一推送目标的策略合并为一条消息
        groups: dict[str, list[tuple[str, list[str]]]] = {}
        for name, symbols, key in active:
            groups.setdefault(self._resolve_token(key), []).append((name, symbols))

        logger.info(
            f"结果汇总完成：{len(active)} 个策略有选股结果，合并为 {len(groups)} 条推送"
        )

        for token, items in groups.items():
            content = self._build_digest_content(items, names, boards, data_date)
            # 日志 tag 保留英文类名便于排查；推送标题用中文概览
            tag = " + ".join(name for name, _ in items)
            total = sum(len(symbols) for _, symbols in items)
            title = f"📈 SequoiaX-AutoPlus 选股播报 · {len(items)} 策略 {total} 只"
            self._post(token, title, content, tag)
