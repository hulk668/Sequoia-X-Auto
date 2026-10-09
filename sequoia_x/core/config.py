"""配置管理模块：通过 pydantic-settings 从环境变量或 .env 文件加载系统配置。"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_path: str = "data/sequoia_v2.db"
    start_date: str = "2024-01-01"
    pushplus_token: str  # 必填字段，缺失或为空时抛出 ValidationError
    strategy_webhooks: dict[str, str] = {}

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # <--- 加上这一行！让 Pydantic 放行未定义的变量
    )

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
                "PUSHPLUS_TOKEN 为空。请在 .env 中填写，或设置环境变量 PUSHPLUS_TOKEN；"
                "GitHub Actions 上需在仓库 Settings → Secrets and variables → Actions "
                "中配置名为 PUSHPLUS_TOKEN 的 repository secret。"
            )
        return v.strip()

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):  # type: ignore[override]
        """扩展配置源，支持从环境变量中扫描 STRATEGY_WEBHOOK_ 前缀的键。"""
        from pydantic_settings import EnvSettingsSource
        import os

        sources = super().settings_customise_sources(settings_cls, **kwargs)

        # 扫描环境变量，将 STRATEGY_WEBHOOK_<KEY> 收集到 strategy_webhooks
        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = {}
        for key, value in os.environ.items():
            # 跳过空值：CI 中未配置的 Secret 会被注入为空字符串，
            # 若不过滤会覆盖掉 default，导致推送使用空 token 而失败。
            if key.upper().startswith(prefix) and value.strip():
                strategy_key = key[len(prefix):].lower()
                webhooks[strategy_key] = value.strip()

        # 注入到初始化数据中（通过 init_kwargs source）
        if webhooks:
            original_init = kwargs.get("init_settings")
            # 直接在 env 层注入，通过 model_post_init 处理
            os.environ.setdefault("_STRATEGY_WEBHOOKS_PARSED", "1")
            # 存储解析结果供 model_validator 使用
            cls._parsed_strategy_webhooks = webhooks

        return sources

    def model_post_init(self, __context: object) -> None:
        """初始化后合并 STRATEGY_WEBHOOK_ 前缀的环境变量到 strategy_webhooks。"""
        import os

        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = dict(self.strategy_webhooks)
        for key, value in os.environ.items():
            # 同上：忽略空字符串，未配置的策略自动回落到全局 PUSHPLUS_TOKEN。
            if key.upper().startswith(prefix) and value.strip():
                strategy_key = key[len(prefix):].lower()
                webhooks[strategy_key] = value.strip()

        # 使用 object.__setattr__ 绕过 pydantic 的不可变保护
        object.__setattr__(self, "strategy_webhooks", webhooks)

    def get_webhook_url(self, webhook_key: str) -> str:
        """
        根据 webhook_key 返回对应的 webhook_key，用于路由到不同推送目标。

        优先从 strategy_webhooks 查找，找不到则返回 'default'。

        Args:
            webhook_key: 策略标识，如 'ma_volume'、'breakout'。

        Returns:
            对应的 webhook_key 字符串。
        """
        return self.strategy_webhooks.get(webhook_key.lower(), "default")


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时从环境变量或 .env 文件加载配置。
    若必填字段（pushplus_token）缺失，抛出 pydantic_core.ValidationError。

    Returns:
        Settings: 全局唯一的配置实例。

    Raises:
        pydantic_core.ValidationError: 当必填字段缺失或字段类型不匹配时抛出。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
