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
    # auto    配了邮件参数就用邮件，否则回落 PushPlus（默认）
    # email   强制只发邮件
    # pushplus 强制只发 PushPlus
    # both    两边都发
    # none    不发通知（只跑数据的工作流用，跳过通道校验）
    notify_channel: str = "auto"

    # ── PushPlus（已改为可选）──
    # 历史上这里是必填；正文超过 2 万字会被服务端拒绝（code 999），
    # 全市场股票池补齐后很容易触顶，因此新增了邮件通道并把它降级为可选。
    pushplus_token: str = ""
    strategy_webhooks: dict[str, str] = {}

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

        历史上 pushplus_token 是**必填**字段。改成邮件通道后换成
        「至少有一个可用通道」：单一通道模式强制对应参数齐全，auto 模式两者取其一。
        这样既不会出现「配好了邮件却因为没填 PushPlus token 起不来」，
        也不会出现「什么都没配、一路跑到推送阶段才报错」。
        """
        channel = (self.notify_channel or "auto").strip().lower()
        object.__setattr__(self, "notify_channel", channel)

        allowed = {"auto", "email", "pushplus", "both", "none"}
        if channel not in allowed:
            raise ValueError(
                f"notify_channel 取值非法：{channel!r}。可选 {' / '.join(sorted(allowed))}。"
            )
        if channel == "none":
            return self  # 明确表示不发通知，跳过校验

        has_mail = self._has_mail_config()
        has_push = bool(self.pushplus_token and self.pushplus_token.strip())

        if channel == "email" and not has_mail:
            raise ValueError(
                "notify_channel=email，但邮件参数不全。请设置 SMTP_HOST / SMTP_USER / "
                "SMTP_PASSWORD / MAIL_TO（本地写在 config.local.toml，CI 上配成 repository secret）。"
            )
        if channel == "pushplus" and not has_push:
            raise ValueError("notify_channel=pushplus，但 PUSHPLUS_TOKEN 为空。")
        if channel == "both" and not (has_mail and has_push):
            raise ValueError("notify_channel=both，需要同时配好邮件参数与 PUSHPLUS_TOKEN。")
        if channel == "auto" and not (has_mail or has_push):
            raise ValueError(
                "未配置任何通知通道。请在 config.local.toml（或环境变量）中设置邮件参数"
                " SMTP_HOST / SMTP_USER / SMTP_PASSWORD / MAIL_TO，或设置 PUSHPLUS_TOKEN。"
                "详见 RUNNING.md。"
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
            `["email"]` / `["pushplus"]` / `["email", "pushplus"]` / `[]`
        """
        channel = (self.notify_channel or "auto").strip().lower()
        if channel == "none":
            return []
        if channel == "email":
            return ["email"]
        if channel == "pushplus":
            return ["pushplus"]
        if channel == "both":
            return ["email", "pushplus"]
        # auto：优先邮件（没有字数上限、排版更好），没有才回落 PushPlus
        if self._has_mail_config():
            return ["email"]
        if self.pushplus_token and self.pushplus_token.strip():
            return ["pushplus"]
        return []

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


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时按「环境变量 > config.local.toml > 默认值」的顺序加载配置。
    若没有任何可用的通知通道（邮件参数不全、同时也没有 PUSHPLUS_TOKEN），
    抛出 pydantic_core.ValidationError —— 与其跑完全部策略才在推送阶段失败，
    不如一开始就说清楚缺什么。

    Returns:
        Settings: 全局唯一的配置实例。

    Raises:
        pydantic_core.ValidationError: 通知通道配置缺失或字段类型不匹配时抛出。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
