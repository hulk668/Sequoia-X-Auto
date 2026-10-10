"""邮件通知测试（渲染 + SMTP 参数选择，全部离线，不发真实邮件）。

为什么这条通道要单独测：邮件正文是**手写 HTML**，最容易出的两类问题是
「动态文本没转义、股票名里的 `<` 把标签撑破」和「邮件客户端不支持
`<style>`/flex、排版塌掉」。前者用字符串断言守住，后者靠「全内联 + 嵌套 table」
的写法保证，这里只断言关键内容确实进了正文。

SMTP 部分只验证**连接参数的选法**（465 隐式 SSL vs 其余端口 STARTTLS）与
**失败不抛异常**，不碰网络。
"""

import smtplib
from pathlib import Path

import pytest

from sequoia_x.notify.email_sender import (
    EmailNotifier,
    _esc,
    _group_by_board,
)
from sequoia_x.notify.strategies import STRATEGY_DISPLAY


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """不吃 config.local.toml，避免本地真实 SMTP 配置影响测试。"""
    import sequoia_x.core.config as cfg_module

    monkeypatch.setattr(cfg_module, "LOCAL_CONFIG_FILE", Path("__no_such_config__.toml"))


MAIL_SETTINGS: dict[str, object] = {
    "notify_channel": "email",
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,
    "smtp_user": "bot@example.com",
    "smtp_password": "secret",
    "mail_to": "ops@example.net",
}


def _notifier(**overrides) -> EmailNotifier:
    from sequoia_x.core.config import Settings

    kwargs = dict(MAIL_SETTINGS)
    kwargs.update(overrides)
    return EmailNotifier(Settings(**kwargs))


# 板块分组测试用的一份固定数据：港口航运 3 只、电力 2 只，
# 其余 3 只各成一块（会被合并到「其他板块」）。
NAMES = {
    "601975": "招商南油",
    "601326": "秦港股份",
    "601298": "青岛港",
    "600900": "长江电力",
    "600886": "国投电力",
    "603200": "上海洗霸",
    "600350": "山东高速",
    "600085": "同仁堂",
}
BOARDS = {
    "601975": "港口航运",
    "601326": "港口航运",
    "601298": "港口航运",
    "600900": "电力",
    "600886": "电力",
    "603200": "环保",
    "600350": "公路铁路",
    "600085": "中药生产",
}


# ── 转义 ──


def test_escape_covers_angle_brackets_amp_and_quotes() -> None:
    """HTML 转义必须连引号一起处理 —— 属性值里也会出现动态文本。"""
    assert _esc('<*ST 测试&">') == "&lt;*ST 测试&amp;&quot;&gt;"


def test_dynamic_stock_name_is_escaped_in_html() -> None:
    """🔴 回归防线：股票名里出现 `<`/`&` 时不能把标签撑破。

    真实场景就是 `*ST` 类股票名，加上板块名是纯文本来源，
    不转义会直接被客户端当标签吞掉。
    """
    n = _notifier()
    html_body = n._render_html(
        [("MaVolumeStrategy", ["600001"])], {"600001": "A<B>&C"}, {}, None
    )

    assert "A&lt;B&gt;&amp;C" in html_body
    assert "A<B>&C" not in html_body


def test_dynamic_board_name_is_escaped_in_html() -> None:
    """板块名同样要转义（单只板块会带 `（板块）` 尾巴拼进标签）。"""
    n = _notifier()
    names = {"600001": "测试股"}
    boards = {"600001": "<恶意>&板块"}
    html_body = n._render_html([("MaVolumeStrategy", ["600001"])], names, boards, None)

    assert "&lt;恶意&gt;&amp;板块" in html_body
    assert "<恶意>" not in html_body


# ── 板块分组 ──


def test_group_by_board_splits_multi_single_and_unknown() -> None:
    """多只板块按只数降序、单只板块按名称排序、无板块的单独一列。"""
    big, singles, unknown, groups = _group_by_board(list(NAMES), BOARDS)

    assert big == ["港口航运", "电力"]
    assert singles == sorted(singles)
    assert set(singles) == {"环保", "公路铁路", "中药生产"}
    assert unknown == []
    assert groups["港口航运"] == ["601975", "601326", "601298"]
    assert groups["电力"] == ["600900", "600886"]


def test_group_by_board_puts_codes_without_board_last() -> None:
    """查不到行业的代码进 `unknown`，不混进板块分组。"""
    boards = {k: v for k, v in BOARDS.items() if k != "603200"}
    big, singles, unknown, groups = _group_by_board(list(NAMES), boards)

    assert unknown == ["603200"]
    assert "环保" not in big + singles
    assert groups[""] == ["603200"]


def test_group_by_board_ties_keep_first_seen_order() -> None:
    """只数相同时保持首次出现顺序，避免每次运行分组顺序乱跳。"""
    symbols = ["1", "2", "3", "4"]
    boards = {"1": "B板块", "2": "A板块", "3": "B板块", "4": "A板块"}

    big, _, _, _ = _group_by_board(symbols, boards)
    assert big == ["B板块", "A板块"]


# ── 地址与主题 ──


def test_recipients_normalizes_all_separators() -> None:
    """中英文逗号/分号/空格混用时都要能拆开，否则 SMTP 会当成一个非法地址。"""
    n = _notifier(mail_to=" a@x.com，b@y.com；c@z.com, d@w.com ")

    assert n._recipients() == ["a@x.com", "b@y.com", "c@z.com", "d@w.com"]


def test_single_recipient_is_returned_as_one_item() -> None:
    """单个地址（本次的真实配置）不能被拆成字符。"""
    n = _notifier()
    assert n._recipients() == ["ops@example.net"]


def test_sender_falls_back_to_smtp_user() -> None:
    """mail_from 留空时用 smtp_user 当发件人。"""
    assert _notifier()._sender() == "bot@example.com"
    assert _notifier(mail_from="noreply@example.com")._sender() == "noreply@example.com"


def test_subject_marks_stale_data_date() -> None:
    """主题里带日期与规模，数据不是当天时显式标注。"""
    items = [("MaVolumeStrategy", ["601975"])]

    fresh = EmailNotifier._subject(items, None)
    assert "1 策略 1 只" in fresh
    assert "数据截止" not in fresh

    assert "数据截止 2020-01-01" in EmailNotifier._subject(items, "2020-01-01")


def test_subject_omits_hint_when_data_date_is_today() -> None:
    """数据就是今天的，不加「数据截止」提示。"""
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")
    subject = EmailNotifier._subject([("MaVolumeStrategy", ["601975"])], today)

    assert "数据截止" not in subject


# ── HTML 渲染 ──


def test_html_renders_rules_board_groups_and_chip_links() -> None:
    """一张卡片里要有：中文名、只数、买点、卖点、板块分组、雪球链接。"""
    n = _notifier()
    html_body = n._render_html([("MaVolumeStrategy", list(NAMES))], NAMES, BOARDS, None)
    info = STRATEGY_DISPLAY["MaVolumeStrategy"]

    assert "①" in html_body
    assert "均线金叉+放量突破" in html_body
    assert _esc(info.entry) in html_body
    assert _esc(info.exit) in html_body
    # 用图标断言，不能用「买点」二字 —— 页脚「买卖点为策略信号参考」里也有
    assert "🎯 " in html_body and "🛑 " in html_body
    assert "https://xueqiu.com/S/SH601975" in html_body
    # 多只板块各自成组；3 个单只板块合并成「其他板块」，板块信息用后缀保留
    assert "港口航运" in html_body
    assert "其他板块" in html_body
    assert "（中药生产）" in html_body


def test_html_marks_stale_data_date_with_warning() -> None:
    """数据不是当天时正文里要顶一个 ⚠️ 标签。"""
    n = _notifier()
    html_body = n._render_html([("MaVolumeStrategy", ["601975"])], NAMES, BOARDS, "2020-01-01")

    assert "⚠️ 数据截止 2020-01-01" in html_body


def test_html_has_no_style_block_or_class_attribute() -> None:
    """🔴 回归防线：`<style>` 与 class 会被邮件客户端剥掉。

    宁可写丑一点的全内联样式，也不能让客户端把整个样式表扔掉。
    """
    n = _notifier()
    html_body = n._render_html([("MaVolumeStrategy", list(NAMES))], NAMES, BOARDS, None)

    assert "<style" not in html_body
    assert "class=" not in html_body
    assert 'style="' in html_body


def test_html_uses_tables_for_layout() -> None:
    """布局必须落在 table 上（flex/grid 在邮件客户端里基本不可用）。"""
    n = _notifier()
    html_body = n._render_html([("MaVolumeStrategy", list(NAMES))], NAMES, BOARDS, None)

    assert html_body.count("<table") >= 2
    assert "display:flex" not in html_body
    assert "display:grid" not in html_body


def test_unknown_strategy_renders_class_name_without_rules() -> None:
    """未登记的策略原样显示类名、不硬塞买卖点（新增策略时不会丢内容）。"""
    n = _notifier()
    html_body = n._render_html(
        [("BrandNewStrategy", ["600519"])], {"600519": "贵州茅台"}, {}, None
    )

    assert "BrandNewStrategy" in html_body
    # 页脚含「买卖点」字样，所以这里按规则块的图标判定
    assert "🎯" not in html_body
    assert "🛑" not in html_body
    assert "贵州茅台" in html_body


def test_missing_name_falls_back_to_code() -> None:
    """名称匹配不上时用代码兜底，不能出现空链接。"""
    n = _notifier()
    html_body = n._render_html([("MaVolumeStrategy", ["600519"])], {}, {}, None)

    assert ">600519</a>" in html_body
    assert ">贵州茅台</a>" not in html_body


# ── 纯文本 fallback ──


def test_text_body_is_plain_and_keeps_everything() -> None:
    """纯文本版不能含任何标签，且名称/板块/买卖点一个不少。"""
    n = _notifier()
    text = n._render_text([("MaVolumeStrategy", list(NAMES))], NAMES, BOARDS, None)

    assert "<" not in text
    assert "招商南油" in text
    assert "买点" in text and "卖点" in text
    assert "[港口航运] 3 只" in text
    assert "（中药生产）" in text


def test_text_body_marks_stale_data_date() -> None:
    """纯文本版也要标数据时效。"""
    n = _notifier()
    text = n._render_text([("MaVolumeStrategy", ["601975"])], NAMES, BOARDS, "2020-01-01")

    assert "⚠️ 数据截止 2020-01-01" in text


# ── MIME 结构 ──


def test_message_is_multipart_alternative_plain_first_then_html() -> None:
    """🔴 纯文本在前、HTML 在后 —— 客户端挑「最靠后且能渲染」的那份。"""
    n = _notifier()
    sent: dict[str, object] = {}

    class FakeConn:
        def login(self, user: str, password: str) -> None:
            sent["login"] = (user, password)

        def send_message(self, msg) -> None:
            sent["msg"] = msg

        def quit(self) -> None:
            sent["quit"] = True

    n._connect = lambda: FakeConn()  # type: ignore[method-assign]
    assert n._send("主题", "<b>html</b>", "plain") is True

    msg = sent["msg"]
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.get_payload()] == ["text/plain", "text/html"]
    assert msg.get_payload()[0].get_payload(decode=True).decode("utf-8") == "plain"
    assert msg.get_payload()[1].get_payload(decode=True).decode("utf-8") == "<b>html</b>"

    assert sent["login"] == ("bot@example.com", "secret")
    assert sent["quit"] is True
    assert msg["To"] == "ops@example.net"


def test_subject_is_rfc2047_encoded_on_the_wire() -> None:
    """中文主题必须走 RFC2047 编码，收件箱里才能正确显示。

    注意不能直接断言 `msg["Subject"] == "..."`：`compat32` 策略下取回来的是
    `Header` 对象，真正的编码发生在序列化时，所以要检查 `as_string()` 的结果。
    """
    from email.header import decode_header, make_header

    n = _notifier()
    sent: dict[str, object] = {}

    class FakeConn:
        def login(self, *a) -> None:
            pass

        def send_message(self, msg) -> None:
            sent["msg"] = msg

        def quit(self) -> None:
            pass

    n._connect = lambda: FakeConn()  # type: ignore[method-assign]
    n._send("SequoiaX-AutoPlus 选股播报 · 测试", "<b>h</b>", "t")

    raw = sent["msg"].as_string()
    assert "Subject: =?utf-8?" in raw

    line = next(ln for ln in raw.splitlines() if ln.startswith("Subject: "))
    assert str(make_header(decode_header(line[len("Subject: ") :]))) == (
        "SequoiaX-AutoPlus 选股播报 · 测试"
    )


# ── SMTP 参数选择 ──


def test_connect_uses_implicit_ssl_on_465(monkeypatch) -> None:
    """465 走 SMTP_SSL（QQ/163/钉钉企业邮箱的约定端口），不碰 STARTTLS。"""
    calls: dict[str, object] = {}

    def fake_ssl(host, port, timeout=None, context=None):
        calls["ssl"] = (host, port, timeout, context is not None)
        return "ssl-conn"

    monkeypatch.setattr(smtplib, "SMTP_SSL", fake_ssl)
    monkeypatch.setattr(
        smtplib, "SMTP", lambda *a, **k: pytest.fail("465 不应该走明文 SMTP")
    )

    assert _notifier(smtp_port=465)._connect() == "ssl-conn"
    assert calls["ssl"][:3] == ("smtp.example.com", 465, 20)
    assert calls["ssl"][3] is True


def test_connect_uses_starttls_on_other_ports(monkeypatch) -> None:
    """非 465（如 587）走明文连接 + STARTTLS 升级，ehlo 要在升级前后各一次。"""
    calls: list[tuple] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("init", host, port))

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self, context=None):
            calls.append(("starttls", context is not None))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(
        smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("587 不应该走隐式 SSL")
    )

    conn = _notifier(smtp_port=587)._connect()

    assert isinstance(conn, FakeSMTP)
    assert calls[0] == ("init", "smtp.example.com", 587)
    assert calls.count(("ehlo",)) == 2
    assert ("starttls", True) in calls


# ── 失败处理 ──


def test_auth_error_returns_false_instead_of_raising() -> None:
    """认证被拒只记日志 —— 通知失败不能让整轮选股算作失败。"""
    n = _notifier()
    n._connect = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        smtplib.SMTPAuthenticationError(535, b"auth failed")
    )

    assert n._send("s", "<b>h</b>", "t") is False


def test_network_error_returns_false_instead_of_raising() -> None:
    """连接超时/域名解析失败同样只记日志。"""
    n = _notifier()
    n._connect = lambda: (_ for _ in ()).throw(TimeoutError("timed out"))  # type: ignore[method-assign]

    assert n._send("s", "<b>h</b>", "t") is False


def test_send_returns_false_when_recipients_missing() -> None:
    """收件人为空时直接跳过，不做无意义的空投递。"""
    n = _notifier(notify_channel="none", mail_to="")

    assert n._recipients() == []
    assert n._send("s", "h", "t") is False


def test_send_digest_skips_when_no_strategy_has_results() -> None:
    """所有策略都没选出票时不发邮件，直接返回成功。"""
    n = _notifier()
    called: list = []
    n._send = lambda *a, **k: called.append(a) or True  # type: ignore[method-assign]

    assert n.send_digest({"MaVolumeStrategy": []}) is True
    assert called == []


def test_send_digest_end_to_end_with_mocked_smtp() -> None:
    """走完整链路：结果 → 渲染 → 投递，正文里能找到股票与买卖点。"""
    n = _notifier()
    captured: dict[str, object] = {}

    class FakeConn:
        def login(self, *a) -> None:
            pass

        def send_message(self, msg) -> None:
            captured["msg"] = msg

        def quit(self) -> None:
            pass

    n._connect = lambda: FakeConn()  # type: ignore[method-assign]

    ok = n.send_digest(
        {"MaVolumeStrategy": list(NAMES)},
        NAMES,
        data_date="2020-01-01",
        boards=BOARDS,
    )

    assert ok is True
    msg = captured["msg"]
    html_body = msg.get_payload()[1].get_payload(decode=True).decode("utf-8")
    assert "招商南油" in html_body
    assert "均线金叉+放量突破" in html_body
    assert "⚠️ 数据截止 2020-01-01" in html_body


# ── 通道装配 ──


def test_build_notifier_email_by_default() -> None:
    """notify_channel 默认就是 email → 装出邮件通知器。"""
    from sequoia_x.core.config import Settings
    from sequoia_x.notify import build_notifier

    notifier = build_notifier(Settings(**MAIL_SETTINGS))

    assert isinstance(notifier, EmailNotifier)


def test_build_notifier_none_channel_sends_nothing() -> None:
    """notify_channel=none → 空通知器，选股结果只落日志。"""
    from sequoia_x.core.config import Settings
    from sequoia_x.notify import build_notifier

    notifier = build_notifier(Settings(notify_channel="none"))

    assert notifier.send_digest({"MaVolumeStrategy": ["600000"]}) is True


def test_email_channel_requires_complete_mail_config() -> None:
    """选了 email 但邮件参数不全 → 启动就报错，不要跑到推送阶段才失败。"""
    from pydantic import ValidationError

    from sequoia_x.core.config import Settings

    with pytest.raises(ValidationError, match="邮件参数不全"):
        Settings(notify_channel="email", smtp_host="smtp.example.com")


def test_unknown_notify_channel_rejected() -> None:
    """notify_channel 只有 email / none 两个合法取值。"""
    from pydantic import ValidationError

    from sequoia_x.core.config import Settings

    with pytest.raises(ValidationError, match="取值非法"):
        Settings(notify_channel="pushplus")
