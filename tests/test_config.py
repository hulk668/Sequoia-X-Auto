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
    monkeypatch.setenv("PUSHPLUS_TOKEN", "test-token")

    from sequoia_x.core.config import Settings

    s = Settings(_env_file=None)
    assert s.db_path == db_path


# Feature: sequoia-x-v2, Property 2: 缺失必填字段触发 ValidationError
def test_missing_required_field_raises(monkeypatch) -> None:
    """属性 2：缺少 pushplus_token 时，实例化 Settings 应抛出 ValidationError。"""
    from sequoia_x.core.config import Settings

    monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    assert "pushplus_token" in str(exc_info.value).lower()


def test_blank_token_raises(monkeypatch) -> None:
    """未配置的 GitHub Secret 会以空字符串注入，必须被拦住（否则跑到推送才失败）。"""
    from sequoia_x.core.config import Settings

    monkeypatch.setenv("PUSHPLUS_TOKEN", "   ")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    assert "pushplus_token" in str(exc_info.value).lower()


def test_blank_bool_is_false(monkeypatch) -> None:
    """workflow_dispatch 的 input 在定时触发时为空串，布尔字段应按 False 处理。"""
    from sequoia_x.core.config import Settings

    monkeypatch.setenv("PUSHPLUS_TOKEN", "test-token")
    monkeypatch.setenv("SKIP_SYNC", "")
    monkeypatch.setenv("PREFER_AKSHARE", "")
    monkeypatch.setenv("ENABLE_AKSHARE_FALLBACK", "")

    s = Settings(_env_file=None)
    assert s.skip_sync is False
    assert s.prefer_akshare is False
    assert s.enable_akshare_fallback is False


def test_blank_strategy_webhook_env_ignored(monkeypatch) -> None:
    """STRATEGY_WEBHOOK_ 前缀的空 Secret 不应覆盖配置文件里的值。"""
    from sequoia_x.core.config import Settings

    monkeypatch.setenv("PUSHPLUS_TOKEN", "test-token")
    monkeypatch.setenv("STRATEGY_WEBHOOK_RPS", "")

    s = Settings(_env_file=None, strategy_webhooks={"rps": "from-config"})
    assert s.strategy_webhooks == {"rps": "from-config"}
