"""邮件通知模块：把选股结果渲染成一份 HTML 邮件发出。

**为什么要有这条通道**：PushPlus 的正文有 2 万字上限，全市场股票池补齐到
5223 只之后，一轮选股很容易超过（实测收到 `code:999 发送内容过大`）。
邮件没有这个限制，而且能排版得更好看。

**邮件客户端的坑**（决定了下面为什么这么写）：

- **`<style>` 块与 class 选择器会被大量客户端剥掉**（Gmail 会砍掉一部分，
  国内客户端更激进）。所以样式**全部内联**，不写 `<style>`、不用 class。
- **flex / grid 基本不可用** → 布局用嵌套 `<table>`（这也是表格在邮件里的正统用法）。
- **`border-radius` / `linear-gradient` 属于渐进增强**：Outlook 等不支持会退化成直角/纯色，
  但信息不会丢，所以照用。
- 所有动态文本（股票名、板块名）都要 **HTML 转义** —— 股票名里出现过 `*ST` 之类，
  板块名更是纯文本来源，不转义会被当成标签吞掉。

结构上是一份 `multipart/alternative`：纯文本在前、HTML 在后，
老客户端自动退化成纯文本版。
"""

import html
import smtplib
import ssl
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.notify.strategies import (
    CIRCLED,
    FOOTER,
    STRATEGY_DISPLAY,
    xueqiu_url,
)

logger = get_logger(__name__)

# ── 配色（浅色邮件底，A 股语境下红为主色）──
_C_PAGE_BG = "#f4f5f7"
_C_CARD_BG = "#ffffff"
_C_HEAD_BG = "#1f2937"
_C_HEAD_BG2 = "#374151"
_C_TITLE = "#1f2328"
_C_TEXT = "#4b5563"
_C_MUTED = "#8a94a6"
_C_BORDER = "#e8eaed"
_C_LINK = "#1565c0"
_C_ACCENT = "#c62828"  # 主色（红）
_C_CHIP_BG = "#f2f4f7"
_C_ENTRY_BG = "#fff5f5"
_C_ENTRY_BAR = "#e53935"
_C_EXIT_BG = "#f5f7fa"
_C_EXIT_BAR = "#90a4ae"

_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
    "'Hiragino Sans GB','Microsoft YaHei',sans-serif"
)

_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# 邮件正文里「只有 1 只的板块」不单独占一组，统一并到「其他板块」，
# 避免 20 多个单只板块把邮件撑得过长（与 PushPlus 版的处理思路一致）。
_SINGLES_TITLE = "其他板块"


def _esc(text: object) -> str:
    """HTML 转义。动态文本（股票名/板块名）一律先过这里。"""
    return html.escape(str(text), quote=True)


def _group_by_board(
    symbols: Sequence[str], boards: Mapping[str, str]
) -> tuple[list[str], list[str], list[str], dict[str, list[str]]]:
    """按行业板块分组。

    Returns:
        (多只板块, 单只板块, 无板块代码, {板块: [代码]})
        多只板块按只数降序（同只数保持首次出现顺序），单只板块按名称排序。
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for code in symbols:
        board = boards.get(code, "")
        if board not in groups:
            groups[board] = []
            order.append(board)
        groups[board].append(code)

    big = sorted(
        (b for b in order if b and len(groups[b]) >= 2), key=lambda b: -len(groups[b])
    )
    singles = sorted(b for b in order if b and len(groups[b]) == 1)
    return big, singles, groups.get("", []), groups


class EmailNotifier:
    """邮件通知器。

    通过 SMTP 发信，正文是一份排好版的 HTML（附纯文本 fallback）。
    配置项见 `Settings` 的 `smtp_*` / `mail_to` 字段。
    """

    def __init__(self, settings: Settings) -> None:
        """初始化 EmailNotifier。

        Args:
            settings: Settings 实例，提供 SMTP 与收件人配置。
        """
        self.settings = settings

    # ── 地址与主题 ──

    def _recipients(self) -> list[str]:
        """解析收件人列表，统一半角逗号分隔。

        手写配置时中英文标点混用很常见（`，` `；`），这里一并归一化，
        避免 SMTP 把整个字符串当成一个非法地址。
        """
        raw = self.settings.mail_to or ""
        for ch in ("；", "，", ";", " "):
            raw = raw.replace(ch, ",")
        return [addr.strip() for addr in raw.split(",") if addr.strip()]

    def _sender(self) -> str:
        """发件地址。未单独配置 mail_from 时回落到 smtp_user。"""
        return (self.settings.mail_from or self.settings.smtp_user).strip()

    @staticmethod
    def _subject(items: Sequence[tuple[str, list[str]]], data_date: str | None) -> str:
        """邮件主题。放日期与规模，方便在收件箱列表里直接看出内容。"""
        today = date.today().strftime("%Y-%m-%d")
        total = sum(len(symbols) for _, symbols in items)
        head = f"Sequoia-X 选股播报 · {today}"
        if data_date and data_date != today:
            head += f"（数据截止 {data_date}）"
        return f"{head} · {len(items)} 策略 {total} 只"

    # ── HTML 渲染 ──

    def _render_chips(self, codes: Sequence[str], names: Mapping[str, str]) -> str:
        """把一组股票渲染成可点的胶囊。"""
        parts = []
        for code in codes:
            label = _esc(names.get(code, code))
            url = _esc(xueqiu_url(code))
            parts.append(
                f'<a href="{url}" target="_blank" style="display:inline-block;'
                f"padding:3px 10px;margin:3px 5px 0 0;background-color:{_C_CHIP_BG};"
                f"border-radius:12px;color:{_C_LINK};text-decoration:none;"
                f'font-size:13px;line-height:1.6;">{label}</a>'
            )
        return "".join(parts)

    def _render_board_block(
        self,
        title: str,
        codes: Sequence[str],
        names: Mapping[str, str],
        boards: Mapping[str, str],
        *,
        with_board_suffix: bool = False,
    ) -> str:
        """一个板块分组：板块名（+只数）后面跟股票胶囊。"""
        cells = []
        for code in codes:
            label = _esc(names.get(code, code))
            board = boards.get(code, "")
            if with_board_suffix and board:
                label += f"（{_esc(board)}）"
            url = _esc(xueqiu_url(code))
            cells.append(
                f'<a href="{url}" target="_blank" style="display:inline-block;'
                f"padding:3px 10px;margin:3px 5px 0 0;background-color:{_C_CHIP_BG};"
                f"border-radius:12px;color:{_C_LINK};text-decoration:none;"
                f'font-size:13px;line-height:1.6;">{label}</a>'
            )
        head = (
            f'<div style="font-size:13px;font-weight:700;color:{_C_ACCENT};'
            f'margin:12px 0 2px;">{_esc(title)}'
            f'<span style="color:{_C_MUTED};font-weight:400;margin-left:6px;">'
            f"{len(codes)}</span></div>"
        )
        return head + '<div style="line-height:1.9;">' + "".join(cells) + "</div>"

    def _render_card(
        self,
        index: int,
        strategy_name: str,
        symbols: Sequence[str],
        names: Mapping[str, str],
        boards: Mapping[str, str],
    ) -> str:
        """单个策略卡片：序号徽章 + 中文名 + 只数 / 买点 / 卖点 / 按板块分组的股票。"""
        marker = CIRCLED[index - 1] if 1 <= index <= len(CIRCLED) else str(index)
        info = STRATEGY_DISPLAY.get(strategy_name)
        title = info.name if info else strategy_name

        badge = (
            f'<span style="display:inline-block;width:20px;height:20px;line-height:20px;'
            f"text-align:center;background-color:{_C_ACCENT};color:#ffffff;"
            f'border-radius:6px;font-size:12px;font-weight:700;vertical-align:middle;">'
            f"{marker}</span>"
        )
        name_html = (
            f'<span style="font-size:15px;font-weight:700;color:{_C_TITLE};'
            f'margin-left:8px;vertical-align:middle;">{_esc(title)}</span>'
            f'<span style="font-size:12px;color:{_C_MUTED};margin-left:6px;'
            f'vertical-align:middle;">{len(symbols)} 只</span>'
        )

        rules = ""
        if info:
            if info.entry:
                rules += (
                    f'<div style="background-color:{_C_ENTRY_BG};'
                    f"border-left:3px solid {_C_ENTRY_BAR};border-radius:0 6px 6px 0;"
                    f'padding:8px 12px;font-size:13px;line-height:1.65;color:{_C_TEXT};">'
                    f'🎯 <b style="color:{_C_ACCENT};">买点</b>　{_esc(info.entry)}</div>'
                )
            if info.exit:
                rules += (
                    f'<div style="background-color:{_C_EXIT_BG};'
                    f"border-left:3px solid {_C_EXIT_BAR};border-radius:0 6px 6px 0;"
                    f'padding:8px 12px;font-size:13px;line-height:1.65;color:{_C_TEXT};'
                    f'margin-top:6px;">'
                    f'🛑 <b style="color:#546e7a;">卖点</b>　{_esc(info.exit)}</div>'
                )

        big, singles, unknown, groups = _group_by_board(symbols, boards)
        body = "".join(
            self._render_board_block(b, groups[b], names, boards) for b in big
        )
        # 单只板块合并到一组，带上「（板块）」尾巴，信息一点不丢
        flat_singles = [groups[b][0] for b in singles]
        if flat_singles:
            body += self._render_board_block(
                _SINGLES_TITLE, flat_singles, names, boards, with_board_suffix=True
            )
        if unknown:
            body += self._render_board_block("未分类", unknown, names, boards)

        return (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin-top:16px;border:1px solid {_C_BORDER};'
            f'border-radius:10px;border-collapse:separate;background-color:{_C_CARD_BG};">'
            f'<tr><td style="padding:14px 16px 2px;">{badge}{name_html}</td></tr>'
            f'<tr><td style="padding:6px 16px 0;">{rules}</td></tr>'
            f'<tr><td style="padding:0 16px 14px;">{body}</td></tr>'
            f"</table>"
        )

    def _render_html(
        self,
        items: Sequence[tuple[str, list[str]]],
        names: Mapping[str, str],
        boards: Mapping[str, str],
        data_date: str | None,
    ) -> str:
        """渲染完整 HTML 邮件。"""
        today = date.today().strftime("%Y-%m-%d")
        total = sum(len(symbols) for _, symbols in items)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 头部概览：日期 · 策略数 · 总只数；数据不是当天的显式标出
        warn = ""
        if data_date and data_date != today:
            warn = (
                f'<span style="display:inline-block;margin-left:8px;padding:2px 8px;'
                f'background-color:#7f1d1d;border-radius:10px;font-size:12px;'
                f'color:#fecaca;">⚠️ 数据截止 {_esc(data_date)}</span>'
            )

        cards = "".join(
            self._render_card(i, name, symbols, names, boards)
            for i, (name, symbols) in enumerate(items, start=1)
        )

        return (
            "<!DOCTYPE html>"
            '<html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>Sequoia-X 选股播报</title></head>"
            f'<body style="margin:0;padding:0;background-color:{_C_PAGE_BG};'
            f'-webkit-text-size-adjust:100%;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="background-color:{_C_PAGE_BG};">'
            '<tr><td align="center" style="padding:24px 12px;">'
            f'<table role="presentation" width="680" cellpadding="0" cellspacing="0" '
            f'border="0" style="width:680px;max-width:100%;background-color:{_C_CARD_BG};'
            f'border-radius:12px;overflow:hidden;font-family:{_FONT};">'
            # 头部
            "<tr>"
            f'<td style="background-color:{_C_HEAD_BG};background-image:linear-gradient('
            f"135deg,{_C_HEAD_BG} 0%,{_C_HEAD_BG2} 100%);padding:24px 28px;\">"
            '<div style="font-size:20px;font-weight:700;color:#ffffff;'
            'letter-spacing:0.5px;">📈 Sequoia-X 选股播报</div>'
            f'<div style="margin-top:8px;font-size:13px;color:#cbd5e1;">'
            f"{today} · {len(items)} 个策略 · 共 {total} 只{warn}</div>"
            "</td>"
            "</tr>"
            # 策略卡片
            f'<tr><td style="padding:4px 20px 10px;">{cards}</td></tr>'
            # 页脚
            f'<tr><td style="padding:16px 28px 22px;background-color:#fafbfc;'
            f'border-top:1px solid #eef0f3;font-size:12px;line-height:1.8;color:{_C_MUTED};">'
            f"{_esc(FOOTER)}<br>"
            f"本邮件由 Sequoia-X 自动发送 · 生成于 {stamp}"
            "</td></tr>"
            "</table>"
            "</td></tr></table></body></html>"
        )

    # ── 纯文本 fallback ──

    def _render_text(
        self,
        items: Sequence[tuple[str, list[str]]],
        names: Mapping[str, str],
        boards: Mapping[str, str],
        data_date: str | None,
    ) -> str:
        """纯文本版本：给不支持 HTML 的客户端兜底。"""
        today = date.today().strftime("%Y-%m-%d")
        total = sum(len(symbols) for _, symbols in items)
        lines = [f"Sequoia-X 选股播报 · {today}", f"{len(items)} 个策略 · 共 {total} 只"]
        if data_date and data_date != today:
            lines.append(f"⚠️ 数据截止 {data_date}")
        lines.append("")

        for i, (name, symbols) in enumerate(items, start=1):
            info = STRATEGY_DISPLAY.get(name)
            lines.append(f"{i}. {info.name if info else name}（{len(symbols)} 只）")
            if info and info.entry:
                lines.append(f"   🎯 买点：{info.entry}")
            if info and info.exit:
                lines.append(f"   🛑 卖点：{info.exit}")
            big, singles, unknown, groups = _group_by_board(symbols, boards)
            for board in big:
                codes = groups[board]
                lines.append(f"   [{board}] {len(codes)} 只")
                lines.append("     " + "、".join(names.get(c, c) for c in codes))
            flat = [(groups[b][0], b) for b in singles] + [(c, "") for c in unknown]
            if flat:
                lines.append("   [其他]")
                for code, board in flat:
                    tail = f"（{board}）" if board else ""
                    lines.append(f"     {names.get(code, code)}{tail}")
            lines.append("")

        lines.append(FOOTER)
        return "\n".join(lines)

    # ── 发送 ──

    def _connect(self) -> smtplib.SMTP:
        """建立 SMTP 连接。

        465 走隐式 SSL（SMTP_SSL），其余端口按 STARTTLS 升级 ——
        这是国内主流邮箱（QQ/163/钉钉企业邮箱）与 Gmail 的通用写法。
        """
        host = self.settings.smtp_host
        port = int(self.settings.smtp_port or 465)
        context = ssl.create_default_context()
        if port == 465:
            return smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        conn = smtplib.SMTP(host, port, timeout=20)
        conn.ehlo()
        conn.starttls(context=context)
        conn.ehlo()
        return conn

    def _send(self, subject: str, html_body: str, text_body: str) -> bool:
        """投递一封邮件，失败只记日志不抛异常。"""
        sender = self._sender()
        recipients = self._recipients()
        if not sender or not recipients:
            logger.error("邮件发送跳过：发件人或收件人为空（检查 SMTP_USER / MAIL_TO）")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(
            (str(Header(self.settings.mail_from_name or "Sequoia-X 选股", "utf-8")), sender)
        )
        msg["To"] = ", ".join(recipients)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
        # 纯文本在前、HTML 在后：客户端挑「最靠后且能渲染」的那份
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            conn = self._connect()
            try:
                conn.login(self.settings.smtp_user, self.settings.smtp_password)
                conn.send_message(msg)
            finally:
                try:
                    conn.quit()
                except Exception:  # noqa: BLE001 - 关闭失败不影响发送结果
                    pass
        except smtplib.SMTPAuthenticationError as exc:
            logger.error(
                f"邮件发送失败：SMTP 认证被拒（{exc.smtp_code}）。"
                f"常见原因：用了登录密码而不是「授权码」，或邮箱未开启 SMTP 服务。"
            )
            return False
        except Exception as exc:  # noqa: BLE001 - 通知失败不应中断主流程
            logger.error(f"邮件发送失败：{type(exc).__name__}: {exc}")
            return False

        logger.info(f"邮件已发送至 {', '.join(recipients)}（{len(html_body)} 字节 HTML）")
        return True

    # ── 对外入口 ──

    def send_digest(
        self,
        results: Mapping[str, tuple[list[str], str]],
        names: Mapping[str, str] | None = None,
        data_date: str | None = None,
        boards: Mapping[str, str] | None = None,
    ) -> bool:
        """汇总所有策略的选股结果，渲染成一封邮件发出。

        Args:
            results: {策略名: (选股代码列表, webhook_key)}。无结果的策略自动跳过。
            names: {代码: 股票名称}，由 DataEngine.get_stock_names() 提供。
            data_date: 数据库数据的截止日期（YYYY-MM-DD），与运行日期不一致时会在邮件里标 ⚠️。
            boards: {代码: 行业板块}，由 DataEngine.get_boards() 提供。

        Returns:
            是否发送成功。不抛异常 —— 通知失败不应让整轮选股算作失败。
        """
        active: list[tuple[str, list[str]]] = [
            (name, symbols) for name, (symbols, _key) in results.items() if symbols
        ]
        if not active:
            logger.info("所有策略均无选股结果，跳过邮件通知")
            return True

        names = names or {}
        boards = boards or {}

        all_symbols = [c for _, symbols in active for c in symbols]
        missing = [c for c in all_symbols if c not in names]
        if missing:
            logger.warning(
                f"{len(missing)}/{len(all_symbols)} 只股票未匹配到名称，将显示代码"
                f"（示例：{', '.join(missing[:5])}）"
            )
        logger.info(
            f"板块信息覆盖 {sum(1 for c in all_symbols if c in boards)}/{len(all_symbols)} 只"
        )

        subject = self._subject(active, data_date)
        html_body = self._render_html(active, names, boards, data_date)
        text_body = self._render_text(active, names, boards, data_date)

        logger.info(
            f"结果汇总完成：{len(active)} 个策略有选股结果，"
            f"合计 {len(all_symbols)} 只，合并为 1 封邮件"
        )
        return self._send(subject, html_body, text_body)
