"""邮件自检脚本测试（`scripts/test_mail.py`）。

脚本本身要联网才能跑完整链路，所以这里只测**不联网也不碰用户真实配置**的部分：
错误翻译（`_explain`）、密码脱敏（`_mask`）、测试邮件构造。这三块恰恰是最容易
写错又最影响排查体验的地方 —— 配错时给出的提示必须准确指到该改的地方。
"""

import importlib.util
import smtplib
import socket
import ssl
from email.header import decode_header, make_header
from email.utils import getaddresses
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("test_mail_script", _ROOT / "scripts" / "test_mail.py")
assert _spec and _spec.loader
mail_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mail_script)


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """不吃 config.local.toml，避免本地真实 SMTP 配置影响测试。"""
    import sequoia_x.core.config as cfg_module

    monkeypatch.setattr(cfg_module, "LOCAL_CONFIG_FILE", Path("__no_such_config__.toml"))


def _settings(**overrides):
    from sequoia_x.core.config import Settings

    kwargs = {
        "notify_channel": "email",
        "smtp_host": "smtp.111.com",
        "smtp_port": 465,
        "smtp_user": "noreply@example.com",
        "smtp_password": "auth-code-1234",
        "mail_to": "noreply@example.com,ops@example.net",
    }
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


# ── 密码脱敏 ──


def test_mask_keeps_only_head_and_tail() -> None:
    """终端上不能出现完整授权码，但又要能看出「填没填、填的是什么」。"""
    masked = mail_script._mask("abcdefghij")

    assert masked == "ab******ij"
    assert "cdefgh" not in masked


def test_mask_handles_empty_and_short_values() -> None:
    """空值要显式标出来（否则看不出是「没填」还是「填错了」）。"""
    assert mail_script._mask("") == "(空)"
    assert mail_script._mask("ab") == "**"
    assert mail_script._mask("abcd") == "****"


# ── 报错翻译 ──


def test_auth_error_hints_point_at_auth_code() -> None:
    """🔴 最关键的提示：认证被拒九成是把登录密码当授权码填了。"""
    hints = mail_script._explain(smtplib.SMTPAuthenticationError(535, b"auth failed"))
    text = "\n".join(hints)

    assert "535" in text
    assert "授权码" in text
    assert "登录密码" in text


def test_ssl_error_hints_point_at_port_mismatch() -> None:
    """465 是隐式 SSL、587 是 STARTTLS，端口与加密方式必须配对。"""
    text = "\n".join(mail_script._explain(ssl.SSLError("wrong version number")))

    assert "465" in text and "587" in text
    assert "端口" in text


def test_timeout_and_dns_errors_are_distinguished() -> None:
    """超时（网络/防火墙）与域名解析失败是两回事，别给同一句话。"""
    timeout_text = "\n".join(mail_script._explain(TimeoutError("timed out")))
    dns_text = "\n".join(mail_script._explain(socket.gaierror("name or service not known")))
    refused_text = "\n".join(mail_script._explain(ConnectionRefusedError(111, "refused")))

    assert "超时" in timeout_text
    assert "解析失败" in dns_text
    assert "拒绝" in refused_text
    assert dns_text != timeout_text


def test_unknown_error_yields_no_hints() -> None:
    """认不出的异常不要硬编原因，宁可让原始报错自己说话。"""
    assert mail_script._explain(ValueError("something else")) == []


# ── 测试邮件构造 ──


def test_test_message_is_alternative_with_readable_headers() -> None:
    """自检邮件要能被任何客户端打开：纯文本正文 + 正确编码的中文主题/发件人。"""
    from sequoia_x.notify.email_sender import EmailNotifier

    settings = _settings()
    notifier = EmailNotifier(settings)
    msg = mail_script._build_test_message(settings, notifier)

    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.get_payload()] == ["text/plain"]

    body = msg.get_payload()[0].get_payload(decode=True).decode("utf-8")
    assert "自检" in body
    assert "smtp.111.com:465" in body

    # 多个收件人都要出现在 To 里（用解析而不是字符串比对，别被分隔符空格绊倒）
    assert [addr for _name, addr in getaddresses([msg["To"]])] == [
        "noreply@example.com",
        "ops@example.net",
    ]

    raw = msg.as_string()
    assert "Subject: =?utf-8?" in raw
    subject_line = next(ln for ln in raw.splitlines() if ln.startswith("Subject: "))
    assert "邮件自检" in str(make_header(decode_header(subject_line[len("Subject: ") :])))


def test_test_message_uses_starttls_label_when_port_is_587() -> None:
    """正文里的加密方式描述要跟着端口走，避免误导排查方向。"""
    from sequoia_x.notify.email_sender import EmailNotifier

    settings = _settings(smtp_port=587)
    notifier = EmailNotifier(settings)
    body = (
        mail_script._build_test_message(settings, notifier)
        .get_payload()[0]
        .get_payload(decode=True)
        .decode("utf-8")
    )

    assert "STARTTLS" in body
    assert "隐式 SSL" not in body
