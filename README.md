# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送至 PushPlus。

数据层以 [baostock](http://baostock.com)（免费、无需注册）为主源拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite；该接口不可用时自动降级到 akshare（东方财富源）兜底。

---

## 两种运行模式

```bash
python main.py               # 日常模式：增量补数据 + 跑完所有策略 + 汇总后一次性 PushPlus 推送
python main.py --backfill     # 回填模式：全市场历史K线一次性灌入（约12分钟，接口不稳定时慎用）
```

本地从零跑起来看 **[RUNNING.md](RUNNING.md)**。

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 海龟突破：20日新高 + 成交额过亿 + 阳线防诱多，按涨幅排序 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 高而窄的旗形整理突破 |
| **LimitUpShakeout** | 涨停洗盘回踩确认 |
| **UptrendLimitDown** | 上升趋势中的跌停反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |
| **PrivatePlacement** | 定增公告监控：近 7 日发布定向增发公告 |

---

## 快速开始 | Quick Start

> 📖 完整的本地运行说明（数据库准备、配置项、常见问题）见 **[RUNNING.md](RUNNING.md)**。

### 环境要求

- Python >= 3.10（推荐 3.11+）

### 1. 安装依赖

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install akshare baostock "pydantic-settings>=2.0" python-dotenv rich pandas requests
```

### 2. 配置

```bash
cp config.example.toml config.local.toml
# 编辑 config.local.toml，填入 pushplus_token
```

`config.local.toml` 已加入 `.gitignore`，不会被提交。也支持传统的 `.env`（见 `.env.example`）。
优先级：**环境变量 > `config.local.toml` > `.env` > 默认值**。

### 3. 准备数据库

新克隆的仓库里没有 `data/sequoia_v2.db`（被 `.gitignore` 排除），从种子解压即可：

```bash
gunzip -c data/seed/sequoia_v2.db.gz > data/sequoia_v2.db
```

（也可用 `python main.py --backfill` 全量回填，但较慢且回填接口本身不稳定。）

### 4. 日常运行

```bash
python main.py
```

建议配合 crontab 每个交易日收盘后自动执行：

```cron
15 19 * * 1-5 cd /root/Sequoia-X && .venv/bin/python main.py >> log.txt 2>&1
```

---

## GitHub Actions 部署 | Deploy on Actions

无需服务器，直接跑在 GitHub Actions 上。工作流文件：`.github/workflows/daily.yml`。

### 1. 配置 Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `PUSHPLUS_TOKEN` | ✅ | PushPlus 全局推送 Token |
| `STRATEGY_WEBHOOK_MA_VOLUME` | 可选 | 均线放量策略专属 Token |
| `STRATEGY_WEBHOOK_TURTLE` | 可选 | 海龟突破策略专属 Token |
| `STRATEGY_WEBHOOK_FLAG` | 可选 | 高窄旗形策略专属 Token |
| `STRATEGY_WEBHOOK_SHAKEOUT` | 可选 | 涨停洗盘策略专属 Token |
| `STRATEGY_WEBHOOK_LIMIT_DOWN` | 可选 | 上升跌停策略专属 Token |
| `STRATEGY_WEBHOOK_RPS` | 可选 | RPS 突破策略专属 Token |
| `STRATEGY_WEBHOOK_PRIVATE_PLACEMENT` | 可选 | 定增策略专属 Token |

未配置的策略会自动回落到全局 `PUSHPLUS_TOKEN`（空值会被忽略，不会覆盖默认 token）。

> **⚠️ Token 从哪来？**
> 在 Actions 上只能来自 GitHub Secrets —— 工作流是通过
> `PUSHPLUS_TOKEN: ${{ secrets.PUSHPLUS_TOKEN }}` 把它注入成环境变量的。
>
> `.env` / `config.local.toml` / `.env.example` 在 Actions 上都**不可用**：
> - `.env` 与 `config.local.toml` 被 `.gitignore` 排除，checkout 时根本不存在；
> - `.env.example` / `config.example.toml` 只是给人复制用的**模板**，
>   程序永远不会读它们。
>
> **千万不要把真实 token 写进模板文件** —— 它会被提交进仓库、公开可见。
> 如果曾经写过，请立刻去 PushPlus 后台重置 token 并更新 Secret，
> 光删文件没用（git 历史里还在）。
>
> 本地运行怎么配 token，见 **[RUNNING.md](RUNNING.md)**。

工作流第一步就会校验 `PUSHPLUS_TOKEN` 是否为空，没配会立刻报错退出，
不会白跑几分钟才在推送阶段失败。

### 2. 定时运行

已配置 `cron: '15 11 * * 1-5'`，即**每周一至周五北京时间 19:15** 自动执行增量更新 + 选股 + 推送。
也可以在 Actions 页面点 **Run workflow** 手动立即执行一次。

### 3. 数据持久化说明

数据库 `data/sequoia_v2.db` 有 100MB+，超过 GitHub 单文件 100MB 限制且被 `.gitignore` 排除，
因此**数据库本身不入库**，改用「缓存 + 种子」两级方案：

1. **滚动缓存**：每轮运行前恢复最近一次缓存，运行后保存新副本 —— 这是正常路径。
   缓存 key 里带了**种子文件的哈希**，所以种子一更新、旧缓存自动失效并重新引导，
   不需要手工去 Actions → Caches 清理。
2. **种子引导**：缓存未命中时（首次运行、种子更新、或缓存被 GitHub 清理），
   自动解压仓库内的 `data/seed/sequoia_v2.db.gz` 作为初始数据库。

两级都没有数据时任务才会失败并输出明确错误，避免"跑绿了但一只都没选出来"的假成功。

> **回填接口（baostock 全市场历史 K 线）当前数据仍有问题**，因此不放进自动化流程。
> 需要回填时在本机手动执行 `python main.py --backfill`。
> 种子文件的详情、当前覆盖面与重新生成方式见 [`data/seed/README.md`](data/seed/README.md)。

⚠️ 当前种子只覆盖约 1332 只股票（全市场约 5200 只），策略每天只扫描这部分市场。
补齐历史数据后请重新生成种子。

### 4. 数据更新与 baostock 可用性

增量同步走 baostock 的 `query_history_k_data_plus`。这个免费接口**并不总是可用**，
常见报错（都是 baostock 内部用 `print()` 直接打到 stdout 的）：

```
服务器连接失败，请稍后再试。 / 接收数据异常，请稍后再试。 / timed out
服务器连接失败，请稍后再试。   →  login: code=10002007 msg=网络接收错误。
```

**同步前会先做一次最小查询探测**（`_probe_baostock()`）：

- 可达 → 正常拉起 8 进程并行同步；
- 不可达 → **自动改用 akshare 兜底**（见下）；
- 显式关闭兜底（`enable_akshare_fallback = false`）时 → 跳过本次同步，只留一条 WARNING：

  ```
  WARNING  baostock 数据接口不可用（登录失败：网络接收错误。｜baostock 输出：服务器连接失败，请稍后再试。）
           ，跳过本次增量同步。数据库数据截止 2026-10-08，本次选股将基于该日期及之前的数据。
  ```

#### akshare 兜底（`sync_via_akshare`）

baostock 不可用时，自动改用 akshare（东方财富源）拉增量数据。

⚠️ **不能直接写入**：不同数据源的**复权基准不同**。实测同一只票同一日（sh600000 / 2026-10-09）：

| 数据源 | 复权方式 | 收盘价 |
|---|---|---|
| baostock | 后复权 | 127.52 |
| 东方财富 | 后复权 | 102.23 |

而且两者的日收益率也不一致 —— 直接拼接会让价格序列出现断层，均线、RPS 排名全部失真。

所以兜底只借用 akshare **前复权序列的相对涨跌**，用库中已有的后复权收盘做锚点换算：

```
k = 库中最后一日后复权收盘 ÷ 该日的前复权收盘
今日后复权价 = 今日前复权价 × k
```

用前复权而非不复权，是为了在除权日也能拿到正确的复权收益率。
实测该换算能**精确复现** baostock 的后复权值（相对误差 0）。
成交量单位也做了对齐（akshare 是「手」，库中存「股」，×100）。

跳过同步时**选股会基于数据库里已有的数据**，所以推送消息会标出真实的数据截止日期：

```
**日期：** 2026-10-09（⚠️ 数据截止 2026-10-08）
```

数据与运行日期一致时不会显示这个提醒。

也可以手动强制跳过：`workflow_dispatch` 勾选 `skip_sync`，或在本机设 `SKIP_SYNC=1`
（跳过全部数据抓取——baostock 与 akshare 兜底都不跑，只跑策略，用于快速出结果）。

### 5. 推送方式

一轮运行会**先跑完全部策略并汇总，最后统一推送一次**，不再每个策略单独发一条消息：

- 所有策略默认都走全局 `PUSHPLUS_TOKEN` → 整轮只发 **1 条**汇总消息；
- 若配置了策略专属 token，则按推送目标分组，**每个目标一条**（不同目标之间不会合并）；
- 无选股结果的策略自动跳过，不会出现在消息里。

**股票名称**取自数据库的 `stock_name` 表，而不是每次推送都去查 baostock：

- 每次运行会尝试刷新一次（单次请求拉全市场，约 30s），失败只告警、不影响流程；
- 名称随数据库一起被缓存，所以某次刷新失败时会沿用上一次的名称；
- 若某只股票始终匹配不到名称，会退化为显示代码（如 `SH603325`），
  同时在日志里给出 `N/M 只股票未匹配到名称` 的告警。

> 之所以不逐只查询名称：baostock 在 CI 环境（海外 runner 共享 IP）请求量上来后
> 会开始返回空结果，导致推送里大量股票只剩代码。改成单次批量拉取并落库可规避。

消息形如（策略以中文名展示，并附带该策略的买点说明）：

```
日期：2026-10-09
策略数：3
选股合计：6 只
---

均线金叉+放量突破（2 只）｜买点：5日线上穿20日线，且成交量放大到20日均量的1.5倍
[贵州茅台](...) [平安银行](...)

海龟突破新高（1 只）｜买点：突破近20日最高价，成交额超1亿、收阳且真涨
...
```

各策略的中文名与买点对应关系（定义在 `sequoia_x/notify/pushplus.py` 的 `STRATEGY_DISPLAY`）：

| 类名 | 中文名 | 买点 |
| --- | --- | --- |
| `MaVolumeStrategy` | 均线金叉+放量突破 | 5日线上穿20日线，且成交量放大到20日均量的1.5倍 |
| `TurtleTradeStrategy` | 海龟突破新高 | 突破近20日最高价，成交额超1亿、收阳且真涨 |
| `HighTightFlagStrategy` | 高位旗形缩量 | 40日大涨后近10日缩量窄幅横盘，且不跌破高位 |
| `LimitUpShakeoutStrategy` | 涨停次日洗盘 | 昨日涨停、今日放量收阴但不破昨收，洗盘不破位 |
| `UptrendLimitDownStrategy` | 上升趋势跌停错杀 | 20日线上穿60日线走多头，今日放量跌停，博错杀反抽 |
| `RpsBreakoutStrategy` | RPS极强动量 | 120日涨幅排全市场前10%，且股价接近120日新高 |
| `PrivatePlacementStrategy` | 定增公告监控 | 近7日发布定向增发公告 |

> 新增策略时在 `STRATEGY_DISPLAY` 里登记一条即可；未登记的类名会原样显示类名、不带买点。
> 日志里仍打印英文类名，方便排查。

### 6. 注意事项

- **定时任务依赖默认分支**：工作流文件必须合入默认分支后 `schedule` 才会生效。
- **60 天不活跃会被禁用**：仓库连续 60 天无提交，GitHub 会自动暂停定时任务，需在 Actions 页面手动重新启用；
  仓库有其它提交活动即可保持激活。
- **cron 可能延迟**：GitHub 定时任务在高峰期可能延迟数分钟到数十分钟，且不保证 100% 触发。
- **缓存会被清理**：`actions/cache` 连续 7 天未被访问会被 GitHub 清掉，
  届时工作流会自动从 `data/seed/` 的种子重新引导，无需人工干预。

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── .github/workflows/daily.yml  # GitHub Actions：定时选股（缓存+种子两级数据）
├── main.py                      # 入口：argparse 分发日常/回填模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── RUNNING.md                   # 【本地运行指南】安装 / 配置 / 建库 / 排错
├── config.example.toml          # 本地配置文件模板（cp 成 config.local.toml 后填值）
├── config.local.toml            # 【已 gitignore】本机真实配置，不会被提交
├── .env.example                 # 传统环境变量模板（仍兼容）
├── data/                        # SQLite 数据库（运行时生成，不入 git）
│   └── seed/sequoia_v2.db.gz    # 数据库种子（Actions 冷启动用，详见同目录 README）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   └── engine.py            # 数据引擎（baostock 回填 + 增量同步 + SQLite）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   └── rps_breakout.py      # RPS 突破策略
│   └── notify/
│       ├── pushplus.py          # PushPlus 汇总推送（当前使用）
│       └── feishu.py            # 飞书 Webhook 推送（历史遗留）
└── tests/                       # 属性测试（hypothesis）
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册、无限流）
- **复权方式**：后复权（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **日常增量**：8 进程并行通过 baostock 拉取，2~3 分钟完成全市场更新

---

## 许可证 | License

MIT
