# 本地运行指南 | Local Setup

> 面向在本机跑 `main.py` 的场景。部署到 GitHub Actions 见 [README 的部署章节](README.md)。

---

## 0. 环境要求

| 项 | 要求 |
|---|---|
| Python | **>= 3.10**，推荐 **3.11+**（本地配置文件用标准库 `tomllib`，3.10 会退化成只读 `.env`） |
| 操作系统 | Windows / macOS / Linux 均可 |
| 网络 | 能访问 baostock（行情主源）与东方财富（akshare 兜底源） |

---

## 1. 安装依赖

```bash
# 1) 建虚拟环境（可选但推荐）
python -m venv .venv

# 2) 激活
.venv\Scripts\activate          # Windows (cmd / PowerShell)
source .venv/bin/activate       # macOS / Linux / Git Bash

# 3) 装依赖
python -m pip install --upgrade pip
pip install akshare baostock "pydantic-settings>=2.0" python-dotenv rich pandas requests
```

> 不要用 `pip install .`：本项目 `pyproject.toml` 没有 `[build-system]`，
> 且根目录存在 `build/` 目录，flat-layout 自动发现容易报错。

---

## 2. 配置（只改一个文件）

```bash
cp config.example.toml config.local.toml
```

然后编辑 `config.local.toml`，至少要填 `pushplus_token`：

```toml
db_path = "data/sequoia_v2.db"
start_date = "2024-01-01"

# 必填：https://www.pushplus.plus/push1.html
pushplus_token = "你的token"

# 接口不通时改成 true，跳过抓数据、直接用库里现有数据选股
skip_sync = false

# baostock 挂了是否自动改用 akshare 兜底
enable_akshare_fallback = true
```

**`config.local.toml` 已加入 `.gitignore`，不会被提交**，可以放心写真实 token。

<details>
<summary>配置优先级 & 兼容旧方式</summary>

优先级从高到低：**环境变量 > `config.local.toml` > `.env` > 默认值**。

也就是说你仍可以用 `.env`（`cp .env.example .env`），或者临时用环境变量覆盖：

```bash
PUSHPLUS_TOKEN=xxx python main.py
```

给某个策略单独配推送 token，在 `config.local.toml` 里加：

```toml
[strategy_webhooks]
turtle = "该策略专属的token"
```

策略标识见 `sequoia_x/strategy/*.py` 里的 `webhook_key`：
`ma_volume` / `turtle` / `flag` / `shakeout` / `limit_down` / `rps` / `private_placement`。
未配置的策略自动使用全局 `pushplus_token`。
</details>

---

## 3. 准备数据库（三选一）

数据库 `data/sequoia_v2.db` 被 `.gitignore` 排除，所以新克隆的仓库里没有。

**A. 已经跑过 → 跳过**

**B. 从种子解压（推荐，几秒完成）**

仓库自带一份压缩快照，覆盖 1332 只股票：

```bash
# Git Bash / macOS / Linux
gunzip -c data/seed/sequoia_v2.db.gz > data/sequoia_v2.db

# Windows PowerShell
# (需要 gzip，或直接用 7-Zip / WSL 解压 data/seed/sequoia_v2.db.gz)
```

**C. 全量回填（慢，约 10 分钟以上，且回填接口本身不稳定）**

```bash
python main.py --backfill
```

> 种子里的数据截止日期见 [data/seed/README.md](data/seed/README.md)。
> 首次运行 `main.py` 会自动把缺口补到最新。

---

## 4. 日常运行

```bash
python main.py
```

做的事：增量同步最新行情 → 刷新股票名称 → 跑全部策略 → **汇总成一条** PushPlus 推送。

再加上 `--skip-sync` 可以**跳过全部数据抓取**，直接用库里现有数据选股：

```bash
python main.py --skip-sync
```

流程日志会打印数据截止日期，例如：

```
数据库数据截止日期：2026-10-09
执行策略：MaVolumeStrategy
MaVolumeStrategy 选出 24 只股票
...
PushPlus 推送成功 [MaVolumeStrategy + TurtleTradeStrategy + ...]
```

数据源不可用时会自动降级：

| 情况 | 行为 |
|---|---|
| baostock 正常 | 8 进程并行拉增量（默认路径） |
| baostock 挂了，akshare 可用 | 自动切 **akshare 兜底**，按比例换算到现有后复权序列 |
| 两个都挂了 | 跳过同步，基于库中现有数据选股，推送里标注 `⚠️ 数据截止 YYYY-MM-DD` |

---

## 5. 跳过增量同步 / 常用参数

**跳过增量同步**有三种方式，任选其一（效果相同）：

| 方式 | 命令 / 配置 | 适用场景 |
|---|---|---|
| **命令行开关**（最省事） | `python main.py --skip-sync` | 临时跑一次，不动配置文件 |
| **环境变量** | Git Bash / macOS / Linux：`SKIP_SYNC=1 python main.py`<br>Windows cmd：`set SKIP_SYNC=1 && python main.py`<br>PowerShell：`$env:SKIP_SYNC=1; python main.py` | 临时跑，或写进脚本 |
| **配置文件** | `config.local.toml` 里 `skip_sync = true` | 长期生效，每次跑都跳过 |

> ⚠️ 用配置文件方式记得**改回 `false`**，否则会一直跳过同步、数据停在旧日期。
> 命令行开关只作用于当次运行，不会写回文件，所以日常临时跳过推荐用它。

跳过同步后：不碰 baostock、不碰 akshare、不刷新股票名称，只读本地数据库跑策略。
推送消息里的日期会是**数据的真实截止日**，例如 `日期：2026-10-10（⚠️ 数据截止 2026-10-09）`。

**其它参数：**

| 场景 | 做法 |
|---|---|
| 关闭 akshare 兜底 | `config.local.toml` 里 `enable_akshare_fallback = false` |
| 换数据库位置 | `config.local.toml` 里改 `db_path` |
| 回填历史 | `python main.py --backfill` |
| 跑测试 | `pip install pytest hypothesis pytest-mock` 后 `pytest` |

---

## 6. 常见问题

**Q：提示 `PUSHPLUS_TOKEN 为空`？**
`config.local.toml` 里的 `pushplus_token` 没填，或者填成了空字符串。

**Q：推送里日期带 `⚠️ 数据截止 ...`？**
说明本次没抓到新数据（通常是交易日还没收盘，或数据源不通）。
只要日期是最近一个交易日就正常；如果停在很早的日期，跑一次 `python main.py` 补数据。

**Q：结果和昨天一模一样？**
先看推送里的日期。日期也没变 = 数据没更新（不是策略的问题）——
确定性策略在相同输入下必然给出相同输出。

**Q：daily 任务报"未找到 data/sequoia_v2.db"？**
本地按第 3 步解压种子；Actions 上会自动从 `data/seed/` 引导。

**Q：`config.local.toml` 会不会不小心提交？**
不会，已在 `.gitignore`。可用 `git check-ignore -v config.local.toml` 自查。
