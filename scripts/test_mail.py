"""邮件通知自检：连接 → 登录 → 发一封测试邮件。

**为什么需要它**：邮件配置只在 CI 定时任务里才会真正用上，配错了要等一整天
（或手动触发一次 Actions）才知道。这个脚本在本地 5 秒就能验完，而且会把
SMTP 抛出的原始错误翻成人话，直接指出该去改哪里。

用法::

    python scripts/test_mail.py            # 完整链路：连接 → 登录 → 发测试邮件
    python scripts/test_mail.py --no-send  # 只测到登录为止，不发信
    python scripts/test_mail.py --preview  # 只渲染示例邮件，不联网

配置来源与主程序一致：环境变量 > config.local.toml > .env > 默认值。
"""

from __future__ import annotations

import argparse
import smtplib
import socket
import ssl
import sys
from datetime import date
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 🔴 必须放在 sys.path 最前：site-packages 里可能残留一份旧的 sequoia_x，
# 从别处运行会被静默遮蔽（见 RUNNING.md 的说明）。
sys.path.insert(0, str(ROOT))

from sequoia_x.core.config import Settings  # noqa: E402
from sequoia_x.notify.email_sender import EmailNotifier  # noqa: E402

# 渲染预览用的示例数据
SAMPLE_ITEMS: list[tuple[str, list[str]]] = [
    ("MaVolumeStrategy", ["601975", "600018", "601866", "600519", "000858"]),
    ("TurtleTradeStrategy", ["601919", "600036", "601398", "000001"]),
    ("RpsBreakoutStrategy", ["300750", "002594", "688981", "601899"]),
]
SAMPLE_NAMES = {
    "601975": "招商南油", "600018": "上港集团", "601866": "中远海发",
    "600519": "贵州茅台", "000858": "五粮液",
    "601919": "中远海控", "600036": "招商银行", "601398": "工商银行",
    "000001": "平安银行",
    "300750": "宁德时代", "002594": "比亚迪", "688981": "中芯国际",
    "601899": "紫金矿业",
}
SAMPLE_BOARDS = {
    "601975": "港口航运", "600018": "港口航运", "601866": "港口航运",
    "600519": "白酒", "000858": "白酒",
    "601919": "航运", "600036": "银行", "601398": "银行", "000001": "银行",
    "300750": "电池", "002594": "汽车整车", "688981": "半导体",
    "601899": "贵金属",
}


def _mask(text: str) -> str:
    """只露头尾，避免把密码/授权码整串打到终端上。"""
    if not text:
        return "(空)"
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def _explain(exc: BaseException) -> list[str]:
    """把异常翻译成「去哪儿改」的提示。"""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return [
            f"SMTP 认证被拒（{exc.smtp_code}）。按可能性从高到低排查：",
            "  1. smtp_password 填的是邮箱【登录密码】而不是【客户端授权码】",
            "     —— 多数邮箱必须用单独生成的授权码 / app password；",
            "  2. 邮箱后台还没开启 SMTP 或「三方客户端」服务；",
            "  3. 授权码已失效（改过邮箱登录密码会作废），重新生成一个；",
            "  4. 复制时带进了空格或换行符。",
        ]
    if isinstance(exc, ssl.SSLError):
        return [
            "TLS/SSL 握手失败。多半是【端口与加密方式不匹配】：",
            "  465 是隐式 SSL，587 是 STARTTLS —— 本程序按端口自动选择，",
            "  若邮箱只开放某一种组合，改 smtp_port 换一种试。",
        ]
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return [
            "连接超时。检查网络能否访问该域名、端口是否被防火墙拦截、",
            "  邮箱后台是否限制了来源 IP。",
        ]
    if isinstance(exc, ConnectionRefusedError):
        return ["端口被拒绝。该端口可能未开放，换 465 或 587 再试。"]
    if isinstance(exc, socket.gaierror):
        return ["域名解析失败。检查 smtp_host 拼写（要服务器域名，不是网页网址）。"]
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return ["收件人被拒。检查 mail_to 地址是否存在、是否被对方反垃圾策略拦下。"]
    return []


def _print_config(settings: Settings, notifier: EmailNotifier) -> None:
    port = int(settings.smtp_port or 465)
    mode = "隐式 SSL（SMTP_SSL）" if port == 465 else "STARTTLS"
    print("── 本次使用的配置 ─────────────────────────────")
    print(f"  服务器    {settings.smtp_host}:{port}   [{mode}]")
    print(f"  发件人    {notifier._sender()}  （显示名 {settings.mail_from_name}）")
    print(f"  账号      {settings.smtp_user}")
    print(f"  密码      {_mask(settings.smtp_password)}")
    print(f"  收件人    {', '.join(notifier._recipients()) or '(未配置)'}")
    print("──────────────────────────────────────────────\n")


def _run_preview(settings: Settings, notifier: EmailNotifier) -> int:
    """不发信，只把示例邮件渲染出来看排版。"""
    data_date = date.today().strftime("%Y-%m-%d")
    html = notifier._render_html(SAMPLE_ITEMS, SAMPLE_NAMES, SAMPLE_BOARDS, data_date)
    text = notifier._render_text(SAMPLE_ITEMS, SAMPLE_NAMES, SAMPLE_BOARDS, data_date)

    out_dir = ROOT / ".workbuddy" / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "mail_preview.html"
    text_path = out_dir / "mail_preview.txt"
    html_path.write_text(html, encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")

    print("主题:", notifier._subject(SAMPLE_ITEMS, data_date))
    print(f"HTML  {len(html)} 字节 → {html_path}")
    print(f"文本  {len(text)} 字节 → {text_path}")
    return 0


def _build_test_message(settings: Settings, notifier: EmailNotifier) -> MIMEMultipart:
    """构造一封纯文本自检邮件。"""
    sender = notifier._sender()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(
        f"Sequoia-X 邮件自检 · {date.today().strftime('%Y-%m-%d')}", "utf-8"
    )
    msg["From"] = formataddr(
        (str(Header(settings.mail_from_name or "Sequoia-X 选股", "utf-8")), sender)
    )
    msg["To"] = ", ".join(notifier._recipients())
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)

    port = int(settings.smtp_port or 465)
    mode = "隐式 SSL" if port == 465 else "STARTTLS"
    msg.attach(
        MIMEText(
            "这是一封来自 Sequoia-X 的 SMTP 自检邮件。\n\n"
            f"发件账号：{settings.smtp_user}\n"
            f"服务器：{settings.smtp_host}:{port}（{mode}）\n\n"
            "收到这封邮件说明邮件通知链路已打通，"
            "每日选股结果会用同样的方式推送到你的邮箱。",
            "plain",
            "utf-8",
        )
    )
    return msg


def _close(conn: smtplib.SMTP) -> None:
    try:
        conn.quit()
    except Exception:  # noqa: BLE001 - 关闭失败不影响结论
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequoia-X 邮件通知自检")
    parser.add_argument("--no-send", action="store_true", help="只测连接与登录，不发信")
    parser.add_argument("--preview", action="store_true", help="只渲染示例邮件，不联网")
    args = parser.parse_args()

    # 预览只是本地渲染，不需要 SMTP 认证 —— 用 notify_channel="none" 跳过
    # 通道完整性校验，这样还没填 smtp_password 也能先看排版。
    if args.preview:
        try:
            preview_settings = Settings(notify_channel="none")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 读取配置失败：{exc}")
            return 2
        preview_notifier = EmailNotifier(preview_settings)
        print("通知渠道：preview（只渲染，不联网）\n")
        _print_config(preview_settings, preview_notifier)
        return _run_preview(preview_settings, preview_notifier)

    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - pydantic 校验失败要打人话
        print(f"❌ 配置不完整，无法继续：\n   {exc}")
        print("\n   本地请编辑 config.local.toml 补全 smtp_host / smtp_user /")
        print("   smtp_password / mail_to（详见 RUNNING.md 第 5 节）。")
        print("   若只想看邮件排版，用 `--preview`，它不需要密码。")
        return 2

    channels = settings.effective_channels()
    if not channels:
        print("❌ 未启用任何通知通道（notify_channel 可能是 none，或参数不全）。")
        return 2
    print(f"通知渠道：{', '.join(channels)}\n")

    notifier = EmailNotifier(settings)
    _print_config(settings, notifier)

    port = int(settings.smtp_port or 465)
    mode = "隐式 SSL" if port == 465 else "STARTTLS"

    print(f"→ 连接 {settings.smtp_host}:{port}（{mode}）…")
    try:
        conn = notifier._connect()
    except Exception as exc:  # noqa: BLE001 - 自检脚本要打印原始错误
        print(f"❌ 连接失败：{type(exc).__name__}: {exc}")
        for line in _explain(exc):
            print(f"   {line}")
        return 1
    print("✅ 连接成功")

    try:
        conn.login(settings.smtp_user, settings.smtp_password)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 登录失败：{type(exc).__name__}: {exc}")
        for line in _explain(exc):
            print(f"   {line}")
        _close(conn)
        return 1
    print("✅ 登录成功（账号与密码/授权码有效）")

    if args.no_send:
        _close(conn)
        print("\n--no-send 已指定，到此为止。配置可用。")
        return 0

    try:
        conn.send_message(_build_test_message(settings, notifier))
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 发信失败：{type(exc).__name__}: {exc}")
        for line in _explain(exc):
            print(f"   {line}")
        return 1
    finally:
        _close(conn)

    print(f"✅ 测试邮件已发送至 {', '.join(notifier._recipients())}")
    print("   没收到就翻一下垃圾邮件箱 —— 新发件账号首次投递常被判垃圾。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
