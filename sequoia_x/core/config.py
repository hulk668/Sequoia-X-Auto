"""配置管理模块。

配置来源优先级（高 → 低）：

1. **环境变量 / GitHub Actions Secrets**（CI 主渠道）
2. **config.local.toml**（本地配置文件，已加入 .gitignore，不会被提交）
3. **.env**（传统方式，已加入 .gitignore）
4. 代码内默认值

本地跑推荐用 `config.local.toml`：`cp config.example.toml config.local.toml` 后填自己的值即可。
该文件不会被提交，所以 token 不会外泄。
"""

from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# 本地配置文件路径（相对项目根目录）。已在 .gitignore 中忽略。
LOCAL_CONFIG_FILE = Path("config.local.toml")


def _load_local_config() -> dict[str, Any]:
    """读取 config.local.toml。

    文件不存在、缺少 tomllib（Python < 3.11）或解析失败时一律返回空 dict，
    保证任何情况下都不会因为配置文件本身把程序拖挂。
    """
    if not LOCAL_CONFIG_FILE.exists():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - 兼容旧解释器
        return {}
    try:
        with LOCAL_CONFIG_FILE.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:  # noqa: BLE001 - 配置文件损坏不应中断程序
        return {}
    return data if isinstance(data, dict) else {}


class _LocalConfigSource(PydanticBaseSettingsSource):
    """把 config.local.toml 接入 pydantic-settings 的配置源链。"""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: ARG002
        # 不使用单字段粒度取值，统一由 __call__ 返回整份配置
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return _load_local_config()


class Settings(BaseSettings):
    db_path: str = "data/sequoia_v2.db"
    start_date: str = "2024-01-01"
    pushplus_token: str  # 必填字段，缺失或为空时抛出 ValidationError
    strategy_webhooks: dict[str, str] = {}
    # 仅做策略选股、跳过所有数据抓取（baostock 与 akshare 兜底都不跑）
    skip_sync: bool = False
    # baostock 不可用时，是否改用 akshare 兜底拉取增量数据
    enable_akshare_fallback: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # 放行未定义的变量
    )

    @field_validator("skip_sync", "enable_akshare_fallback", mode="before")
    @classmethod
    def _blank_bool_is_false(cls, v: object) -> object:
        """把空字符串按 False 处理。

        GitHub Actions 里用 workflow_dispatch 的 input 注入环境变量时，
        定时触发（schedule）下该 input 取到的是空字符串。pydantic 解析 bool
        遇到 "" 会直接抛 ValidationError，导致定时任务全部失败 —— 这里兜住。
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return False
        return v

    @field_validator("pushplus_token")
    @classmethod
    def _pushplus_token_not_blank(cls, v: str) -> str:
        """拒绝空 token。

        只声明 `pushplus_token: str` 只能拦住"字段完全缺失"的情况；
        如果环境变量存在但值是空字符串（CI 中未配置的 Secret 正是如此），
        空串对 str 类型依然合法，会一路带到推送阶段才以
        PushPlus 的 "token不能为空" 失败。这里提前拦掉，快速报错。
        """
        if not v or not v.strip():
            raise ValueError(
                "PUSHPLUS_TOKEN 为空。本地请在 config.local.toml 中填写 pushplus_token"
                "（可从 config.example.toml 复制），或设置环境变量 PUSHPLUS_TOKEN；"
                "GitHub Actions 上需在仓库 Settings → Secrets and variables → Actions "
                "中配置名为 PUSHPLUS_TOKEN 的 repository secret。"
            )
        return v.strip()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """装配配置源，靠前者覆盖靠后者。

        环境变量 > config.local.toml > .env > 系统 secrets > 默认值。
        """
        return (
            init_settings,
            env_settings,
            _LocalConfigSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    def model_post_init(self, __context: object) -> None:
        """初始化后合并 STRATEGY_WEBHOOK_ 前缀的环境变量到 strategy_webhooks。

        配置文件里的 [strategy_webhooks] 段已由 pydantic 直接解析；
        这里只做环境变量的补充覆盖，方便 CI 用 Secrets 单独配置某个策略的 token。
        """
        import os

        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = dict(self.strategy_webhooks)
        for key, value in os.environ.items():
            # 忽略空字符串：CI 中未配置的 Secret 会被注入为 ""，
            # 若不过滤会覆盖掉配置文件里的值，导致推送使用空 token 而失败。
            if key.upper().startswith(prefix) and value.strip():
                strategy_key = key[len(prefix):].lower()
                webhooks[strategy_key] = value.strip()

        # 使用 object.__setattr__ 绕过 pydantic 的不可变保护
        object.__setattr__(self, "strategy_webhooks", webhooks)

    def get_webhook_url(self, webhook_key: str) -> str:
        """
        根据 webhook_key 返回对应的推送目标标识。

        优先从 strategy_webhooks 查找，找不到则返回 'default'。

        Args:
            webhook_key: 策略标识，如 'ma_volume'、'turtle'。

        Returns:
            对应的 webhook_key 字符串。
        """
        return self.strategy_webhooks.get(webhook_key.lower(), "default")


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时按「环境变量 > config.local.toml > .env > 默认值」的顺序加载配置。
    若必填字段（pushplus_token）缺失或为空，抛出 pydantic_core.ValidationError。

    Returns:
        Settings: 全局唯一的配置实例。

    Raises:
        pydantic_core.ValidationError: 当必填字段缺失或字段类型不匹配时抛出。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
