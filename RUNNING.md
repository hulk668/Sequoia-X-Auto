# 本地运行指南 | Local Setup

> 面向在本机跑 `main.py` 的场景。部署到 GitHub Actions 见 [README 的部署章节](README.md)。

---

## 0. 环境要求

| 项 | 要求 |
|---|---|
| Python | **>= 3.10**，推荐 **3.11+**（本地配置文件用标准库 `tomllib`，3.10 会退化成只读 `.env`） |
| 操作系统 | Windows / macOS / Linux 均可 |
| 网络 | 能访问东方财富（akshare，默认主源）与 baostock（可选备用源） |

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
> flat-layout 自动发现容易报错。

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

# 数据更新直接走 akshare，不探测 baostock（推荐：baostock 免费服务不稳定）
prefer_akshare = true

# 完全不更新数据：跳过抓数据、直接用库里现有数据选股（跑策略调试时用）
skip_sync = false

# baostock 探测不通时是否允许回退 akshare（prefer_akshare 关掉时才有意义）
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

种子压缩包**不在仓库里**，在 **GitHub Release** 上（压缩后 75.9MB，覆盖全市场 5223 只）：

```bash
TAG=$(cat data/seed/VERSION)          # 例如 seed-2026-10-10

# Git Bash / macOS / Linux（需要 gh CLI）
gh release download "$TAG" --pattern 'sequoia_v2.db.gz' --dir data/seed
gunzip -c data/seed/sequoia_v2.db.gz > data/sequoia_v2.db

# 没有 gh 也行，直接 curl（公开仓库可匿名下载）
curl -L -o data/seed/sequoia_v2.db.gz \
  "https://github.com/hulk668/Sequoia-X-Auto/releases/download/$TAG/sequoia_v2.db.gz"
gunzip -c data/seed/sequoia_v2.db.gz > data/sequoia_v2.db
```

最省事的办法是直接到浏览器打开
<https://github.com/hulk668/Sequoia-X-Auto/releases>，找到 `data/seed/VERSION` 里写的那个 tag，
下载 `sequoia_v2.db.gz`，用 7-Zip / WSL 解压到 `data/sequoia_v2.db`。

**C. 补数（把库里没有的股票一次灌满）**

```bash
# 默认 400 根日K、东财优先腾讯兜底，全市场约 90 秒
python main.py --backfill

# 先小批试跑
python main.py --backfill --limit 100

# 指定保留根数与数据源
python main.py --backfill --bars 250 --source qq
```

补数走两条 HTTP 通道（东财 `push2his` → 腾讯 `web.ifzq.gtimg.cn`），
**不依赖 baostock**（它的历史 K 线接口实测会卡死不返回）。任务可中断续跑：
中途 Ctrl+C 后重跑会自动跳过已完成的股票。

> 种子里的数据截止日期见 [data/seed/README.md](data/seed/README.md)。
> 首次运行 `main.py` 会自动把缺口补到最新。

---

## 4. 日常运行

```bash
python main.py
```

做的事：增量同步最新行情 → 刷新股票名称 → 跑全部策略 → 反查板块 → **汇总成一条** PushPlus 推送。

再看看不更新数据、直接用库里现有数据选股：

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

数据更新通道的优先级：

| 配置 | 行为 |
|---|---|
| `PREFER_AKSHARE=true`（推荐） | **直接走 akshare**，跳过 baostock 探测 |
| 默认（不设） | 先探测 baostock：可达 → 8 进程并行拉取；不可达 → 回退 akshare（`enable_akshare_fallback=false` 时则跳过同步） |
| `SKIP_SYNC=true` | 完全不更新数据，基于库中现有数据选股，推送里标注 `⚠️ 数据截止 YYYY-MM-DD` |

无论走哪条通道，akshare 拿到的数据都会按「锚点 + 比例换算」对齐到库中的后复权序列
（换算公式见 [README](README.md) 的「数据更新」一节）。

---

## 5. 数据更新的三种方式 / 常用参数

### 5.1 换数据源：用 akshare（推荐）

`baostock` 免费服务长期不稳定（同一天可能通、也可能不通），本地推荐直接指定 akshare：

| 方式 | 命令 / 配置 | 适用场景 |
|---|---|---|
| **命令行开关**（最省事） | `python main.py --prefer-akshare` | 临时跑一次，不动配置文件 |
| **环境变量** | Git Bash / macOS / Linux：`PREFER_AKSHARE=true python main.py`<br>Windows cmd：`set PREFER_AKSHARE=true && python main.py`<br>PowerShell：`$env:PREFER_AKSHARE="true"; python main.py` | 临时跑，或写进脚本 |
| **配置文件** | `config.local.toml` 里 `prefer_akshare = true` | 长期生效，每次跑都走 akshare |

### 5.2 完全不更新数据：`skip_sync`

三种方式，任选其一（效果相同）：

| 方式 | 命令 / 配置 |
|---|---|
| **命令行开关** | `python main.py --skip-sync` |
| **环境变量** | `SKIP_SYNC=1 python main.py`（PowerShell：`$env:SKIP_SYNC="1"; python main.py`） |
| **配置文件** | `config.local.toml` 里 `skip_sync = true` |

> ⚠️ 用配置文件方式记得**改回 `false`**，否则会一直跳过更新、数据停在旧日期。
> 命令行开关只作用于当次运行，不会写回文件。

`skip_sync` 优先级高于 `prefer_akshare`：同时打开时**不更新任何数据**。
跳过更新后：不碰 akshare、不碰 baostock、不刷新股票名称，只读本地数据库跑策略。
推送消息里的日期会是**数据的真实截止日**，例如 `📅 **2026-10-10**（⚠️ 数据截止 2026-10-09）`。

### 5.3 其它参数

| 场景 | 做法 |
|---|---|
| 关闭 akshare 回退 | `config.local.toml` 里 `enable_akshare_fallback = false` |
| 换数据库位置 | `config.local.toml` 里改 `db_path` |
| 补齐股票池 | `python main.py --backfill`（东财→腾讯，可中断续跑） |
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
本地按第 3 步从 Release 下载种子解压。
Actions 上会自动从 **GitHub Release** 下载种子（tag 取自 `data/seed/VERSION`）——
跑到这一步说明缓存和 Release 都没取到，检查 `data/seed/VERSION` 里写的那个 tag
在 <https://github.com/hulk668/Sequoia-X-Auto/releases> 上是否存在、资产是否还在。

**Q：`config.local.toml` 会不会不小心提交？**
不会，已在 `.gitignore`。可用 `git check-ignore -v config.local.toml` 自查。

**Q：推送里有些股票没带板块（只显示名称）？**
正常。板块来自东财 **EM2016 行业分类**（三级分类取第二级）：

- 北交所（`4`/`8` 开头）在该接口里没有数据，会显示为纯名称；
- 个别股票确实没有行业数据，也显示为纯名称；
- 首次运行会**批量**反查（每批 45 只，90 只股票只要 2 次请求），之后 30 天内走本地 `stock_board` 缓存。
- 日志里会打印 `板块信息覆盖 N/M 只`，可以据此判断。

> 早期版本用的是东财 F10「核心题材」，实测不可靠：会把 `一带一路`、`央国企改革`
> 这类泛主题排在首位（招商南油被标成「一带一路」而不是「港口航运」）。
> 已经换成 EM2016 行业分类，旧的 `stock_concept` 表在初始化时会被自动删除。

**Q：想改 Actions 的定时时间？**
改 `.github/workflows/daily.yml` 里的 `cron`，注意**必须换算成 UTC**：
北京时间 18:30 = UTC 10:30（写成 `'30 10 * * 1-5'`）。工作流里的 `TZ: Asia/Shanghai`
只影响日志时间戳和 `date.today()`，**不影响调度时刻**，照抄北京时间会跑早 8 小时。

**Q：`stock_board` 表能删吗？**
可以，删掉后下一轮会重新反查并写回。它只是缓存，行情数据不受影响。
（旧版的 `stock_concept` 表已经不在了 —— 初始化时会自动清掉。）
