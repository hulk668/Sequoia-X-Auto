"""配置管理属性测试。"""

from pathlib import Path

import pytest
from hypothesis import given, HealthCheck
from hypothesis import settings as h_settings
from hypothesis import strategies as st
from pydantic import ValidationError

# 测试期间把本地配置文件指向一个不存在的路径，保证断言只受环境变量影响
_NO_LOCAL_CONFIG = "__no_such_config__.toml"


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """隔离外部配置：不吃 config.local.toml / .env，只认测试自己设的环境变量。"""
    import sequoia_x.core.config as cfg_module

    monkeypatch.setattr(cfg_module, "LOCAL_CONFIG_FILE", Path(_NO_LOCAL_CONFIG))


# Feature: sequoia-x-v2, Property 1: 环境变量覆盖配置默认值
@given(
    db_path=st.text(
        min_size=1,
        max_size=100,
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/_.-"
        ),
    )
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_env_overrides_default(db_path: str, monkeypatch) -> None:
    """属性 1：任意合法 db_path 通过环境变量设置后，Settings 实例应反映该值。"""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("NOTIFY_CHANNEL", "none")

    from sequoia_x.core.config import Settings

    s = Settings()
    assert s.db_path == db_path


# Feature: sequoia-x-v2, Property 2: 缺少必填的通知参数触发 ValidationError
def test_missing_mail_config_raises(monkeypatch) -> None:
    """属性 2：选了 email 但邮件参数不全时，实例化 Settings 应抛出 ValidationError。"""
    from sequoia_x.core.config import Settings

    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("MAIL_TO", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(notify_channel="email")
    assert "邮件参数不全" in str(exc_info.value)


def test_blank_mail_param_raises(monkeypatch) -> None:
    """未配置的 GitHub Secret 会以空字符串注入，必须被拦住（否则跑到推送才失败）。"""
    from sequoia_x.core.config import Settings

    monkeypatch.setenv("SMTP_PASSWORD", "   ")

    with pytest.raises(ValidationError) as exc_info:
        Settings(notify_channel="email", smtp_host="smtp.example.com",
                 smtp_user="bot@example.com", mail_to="ops@example.net")
    assert "邮件参数不全" in str(exc_info.value)


def test_blank_bool_is_false(monkeypatch) -> None:
    """workflow_dispatch 的 input 在定时触发时为空串，布尔字段应按 False 处理。"""
    from sequoia_x.core.config import Settings

    monkeypatch.setenv("NOTIFY_CHANNEL", "none")
    monkeypatch.setenv("SKIP_SYNC", "")

    s = Settings()
    assert s.skip_sync is False


def test_none_channel_skips_mail_validation() -> None:
    """notify_channel=none 时不校验邮件参数 —— 本地只调策略的场景。"""
    from sequoia_x.core.config import Settings

    s = Settings(notify_channel="none")

    assert s.effective_channels() == []
