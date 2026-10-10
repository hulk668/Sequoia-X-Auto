"""配置管理模块。

配置来源优先级（高 → 低）：

1. **环境变量**（GitHub Actions Secrets 走这条；也方便临时覆盖一两个值）
2. **config.local.toml**（本地配置文件，已加入 .gitignore，不会被提交）
3. 代码内默认值

**本地只需要维护 config.local.toml 一个文件**：
`cp config.example.toml config.local.toml` 后填自己的值即可。
它不会被提交，所以 token / 授权码可以放心写在里面。

> 为什么不再支持 `.env`：它加载后是以**环境变量**身份参与配置的，优先级高于
> `config.local.toml` —— 留一个陈旧的 `.env` 会静默盖掉配置文件里的值，
> 排查起来很费劲。本地配置收敛到单一文件后这类问题就不存在了。
"""

from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator
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

    # ── 通知通道 ──
    # email  发邮件（默认）
    # none   不发通知（只跑数据/只调策略时用，跳过邮件参数校验）
    notify_channel: str = "email"

    # ── 邮件 / SMTP ──
    # 465 走隐式 SSL，其余端口按 STARTTLS 升级
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    # 注意：多数邮箱这里要填「客户端授权码」而不是登录密码
    smtp_password: str = ""
    mail_from: str = ""  # 留空则用 smtp_user
    mail_from_name: str = "SequoiaX-AutoPlus 选股"
    # 收件人，多个用逗号分隔（中英文逗号、分号都能识别）
    mail_to: str = ""
    # 仅做策略选股、跳过所有数据抓取（不碰 akshare，也不刷新股票名称）
    skip_sync: bool = False

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",  # 放行未定义的变量
    )

    @field_validator("skip_sync", mode="before")
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

    @model_validator(mode="after")
    def _validate_channels(self) -> "Settings":
        """校验通知通道配置是否自洽。

        `notify_channel = "email"` 时邮件参数必须齐全，否则**启动就报错** ——
        比跑完几千只股票再在推送阶段失败体面得多。
        只跑数据/只调策略时把 `notify_channel` 设成 `"none"` 即可跳过校验。
        """
        channel = (self.notify_channel or "email").strip().lower()
        object.__setattr__(self, "notify_channel", channel)

        allowed = {"email", "none"}
        if channel not in allowed:
            raise ValueError(
                f"notify_channel 取值非法：{channel!r}。可选 {' / '.join(sorted(allowed))}。"
            )
        if channel == "none":
            return self  # 明确表示不发通知，跳过校验

        if not self._has_mail_config():
            raise ValueError(
                "notify_channel=email，但邮件参数不全。请设置 SMTP_HOST / SMTP_USER / "
                "SMTP_PASSWORD / MAIL_TO（本地写在 config.local.toml，CI 上配成 repository secret）。"
                "只想跑策略不要通知的话，把 notify_channel 设成 \"none\"。"
            )
        return self

    def _has_mail_config(self) -> bool:
        """邮件通道所需参数是否齐全（mail_from 缺省时会回落到 smtp_user）。"""
        return all(
            str(v).strip()
            for v in (self.smtp_host, self.smtp_user, self.smtp_password, self.mail_to)
        )

    def effective_channels(self) -> list[str]:
        """实际要使用的通知渠道列表。

        Returns:
            `["email"]`，或 `[]`（`notify_channel = "none"` 时）。
        """
        channel = (self.notify_channel or "email").strip().lower()
        if channel == "none":
            return []
        return ["email"]

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

        环境变量 > config.local.toml > 系统 secrets > 默认值。
        """
        return (
            init_settings,
            env_settings,
            _LocalConfigSource(settings_cls),
            file_secret_settings,
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时按「环境变量 > config.local.toml > 默认值」的顺序加载配置。
    若 `notify_channel = "email"` 而邮件参数不全，抛 pydantic_core.ValidationError ——
    与其跑完全部策略才在推送阶段失败，不如一开始就说清楚缺什么。

    Returns:
        Settings: 全局唯一的配置实例。

    Raises:
        pydantic_core.ValidationError: 通知配置缺失或字段类型不匹配时抛出。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
