"""全局测试夹具：让测试与本机配置彻底隔离。

🔴 背景（2026-10-10 的一次真实翻车）：

个别测试文件自带 `_isolate_config` fixture，但**大多数没有** —— 那些用例会读到
开发者本机的 `config.local.toml` 与 `.env`。平时看不出问题，直到本机配置处于
「已选择邮件通道、但还没填密码」这类**合法中间态**时，`Settings` 的通道校验直接抛错，
23 个用例集体变红。代码没问题，是测试不自包含。

这类失败最坑的地方在于：CI 上是绿的（没有本地配置文件），只有开发者本机红，
很容易被当成"环境问题"糊过去。所以这里用 autouse fixture 把两个配置源都堵死：

  - `config.local.toml`：模块级常量，`_load_local_config()` 每次调用时读取，改常量即可
  - `.env`：来自 `SettingsConfigDict(env_file=...)`，换成不存在的文件名

环境变量**故意不隔离** —— 显式传入的 kwargs 优先级最高，测试自己会覆盖需要的字段；
而 CI 上本来就靠环境变量注入，保持真实更符合使用场景。

各测试文件里原有的同名 fixture 现在是冗余的，但保留不动，避免无谓改动。
"""

from pathlib import Path

import pytest

_NO_SUCH_CONFIG = Path("__no_such_config__.toml")
_NO_SUCH_ENV = "__no_such_env__.env"


@pytest.fixture(autouse=True)
def _isolate_local_config(monkeypatch):
    """屏蔽本机配置文件，保证任何用例都只依赖自己显式传入的参数。"""
    import sequoia_x.core.config as cfg_module

    monkeypatch.setattr(cfg_module, "LOCAL_CONFIG_FILE", _NO_SUCH_CONFIG)

    settings_cls = cfg_module.Settings
    monkeypatch.setattr(
        settings_cls,
        "model_config",
        {**settings_cls.model_config, "env_file": _NO_SUCH_ENV},
    )
