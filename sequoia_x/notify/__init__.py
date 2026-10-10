"""通知模块：只有邮件一条通道。

对外只暴露 `build_notifier(settings)`，调用方只需认 `send_digest(...)` 这一个方法。

`notify_channel = "none"` 时返回空通知器 —— 结果只落在日志里，
方便「只想跑策略、不想收信」的场合（比如本地调策略）。
"""

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class _NoopNotifier:
    """不发任何通知，只在日志里留一句。"""

    def send_digest(
        self,
        results: dict[str, list[str]],
        names: dict[str, str] | None = None,
        data_date: str | None = None,
        boards: dict[str, str] | None = None,
    ) -> bool:
        total = sum(len(v) for v in results.values())
        logger.info(f"通知已关闭（notify_channel=none），{total} 只选股结果只记在日志里")
        return True


def build_notifier(settings: Settings):
    """按配置组装通知器。

    - `notify_channel = "email"`（默认）→ 邮件通知器
    - `notify_channel = "none"` → 空通知器（结果只落日志）

    Args:
        settings: 已校验过的 Settings。

    Returns:
        具备 `send_digest` 方法的通知器。
    """
    if not settings.effective_channels():
        logger.warning("未启用通知通道，选股结果只会出现在日志里")
        return _NoopNotifier()

    from sequoia_x.notify.email_sender import EmailNotifier

    logger.info("通知渠道：邮件")
    return EmailNotifier(settings)
