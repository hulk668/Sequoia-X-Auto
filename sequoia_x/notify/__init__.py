"""通知模块：邮件渠道（主）与 PushPlus。

对外只暴露 `build_notifier(settings)`：按 `Settings.effective_channels()` 的结果
组装出通知器，调用方只需认 `send_digest(...)` 这一个方法。

为什么要这个工厂：主流程不该关心「这次往哪儿发」。渠道的选择完全由配置决定
（`notify_channel`），main.py 里只留一行 `notifier = build_notifier(settings)`，
以后再加渠道（Server 酱、企业微信…）也只用动这个文件。
"""

from collections.abc import Mapping

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class FanOutNotifier:
    """把同一份结果扇出到多个通知器。

    单个渠道失败只记 ERROR、不影响其它渠道 —— 通知是「尽力而为」，
    不能因为邮件发不出去就让已经算好的选股结果丢掉。
    """

    def __init__(self, notifiers: list) -> None:
        self._notifiers = list(notifiers)

    def send_digest(
        self,
        results: Mapping[str, tuple[list[str], str]],
        names: Mapping[str, str] | None = None,
        data_date: str | None = None,
        boards: Mapping[str, str] | None = None,
    ) -> bool:
        """依次调用各通知器，返回是否至少有一个成功。"""
        succeeded = False
        for notifier in self._notifiers:
            try:
                result = notifier.send_digest(
                    results, names, data_date=data_date, boards=boards
                )
                # send_digest 返回 None 的渠道（如 PushPlus）视为已投递
                succeeded = True if result is None else (succeeded or bool(result))
            except Exception as exc:  # noqa: BLE001 - 单个渠道失败不影响其它
                logger.error(f"{type(notifier).__name__} 通知失败：{exc}")
        return succeeded


def build_notifier(settings: Settings) -> FanOutNotifier:
    """按配置组装通知器。

    渠道由 `Settings.effective_channels()` 决定：

    - `["email"]` → 邮件
    - `["pushplus"]` → PushPlus
    - `["email", "pushplus"]` → 两者都发
    - `[]` → 空通知器（结果只落日志）

    Args:
        settings: 已校验过的 Settings（通道参数齐全性由 config 层保证）。

    Returns:
        具备 `send_digest` 方法的通知器。
    """
    channels = settings.effective_channels()
    notifiers: list = []

    if "email" in channels:
        from sequoia_x.notify.email_sender import EmailNotifier

        notifiers.append(EmailNotifier(settings))
    if "pushplus" in channels:
        from sequoia_x.notify.pushplus import PushPlusNotifier

        notifiers.append(PushPlusNotifier(settings))

    if not notifiers:
        logger.warning("未启用任何通知通道，选股结果只会出现在日志里")
    else:
        logger.info(f"通知渠道：{', '.join(channels)}")

    return FanOutNotifier(notifiers)
