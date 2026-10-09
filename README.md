# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送至飞书群。

数据层使用 [baostock](http://baostock.com)（免费、无需注册、无限流）拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite，彻底规避东方财富反爬问题。

---

## 两种运行模式

```bash
python main.py               # 日常模式：增量补数据 + 跑完所有策略 + 汇总后一次性 PushPlus 推送
python main.py --backfill     # 回填模式：全市场历史K线一次性灌入（约12分钟，接口不稳定时慎用）
```

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

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10

### 1. 安装依赖

```bash
# 推荐使用 uv（快速包管理器）
uv sync

# 或者 pip
pip install .
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写飞书 Webhook URL
```

### 3. 首次回填历史数据

```bash
python main.py --backfill
```

约 12 分钟完成 ~5200 只 A 股历史后复权日 K 数据回填。

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
> 只能来自 GitHub Secrets —— 工作流是通过 `PUSHPLUS_TOKEN: ${{ secrets.PUSHPLUS_TOKEN }}`
> 把它注入成环境变量的。
>
> `.env` 和 `.env.example` 在 Actions 上都**不可用**：
> - `.env` 被 `.gitignore` 排除，checkout 时根本不存在；
> - `.env.example` 只是给人 `cp .env.example .env` 用的**模板**，
>   程序里 `load_dotenv()` 默认只读 `.env`，永远不会读 `.env.example`。
>
> **千万不要把真实 token 写进 `.env.example`** —— 它会被提交进仓库、公开可见。
> 如果曾经写过，请立刻去 PushPlus 后台重置 token 并更新 Secret，光删文件没用（git 历史里还在）。

工作流第一步就会校验 `PUSHPLUS_TOKEN` 是否为空，没配会立刻报错退出，
不会白跑几分钟才在推送阶段失败。

### 2. 定时运行

已配置 `cron: '15 11 * * 1-5'`，即**每周一至周五北京时间 19:15** 自动执行增量更新 + 选股 + 推送。
也可以在 Actions 页面点 **Run workflow** 手动立即执行一次。

### 3. 数据持久化说明

数据库 `data/sequoia_v2.db` 有 100MB+，超过 GitHub 单文件 100MB 限制且被 `.gitignore` 排除，
因此**数据库本身不入库**，改用「缓存 + 种子」两级方案：

1. **滚动缓存**：每轮运行前按 `restore-keys: sequoia-db-` 恢复最近一次缓存，
   运行后用 `sequoia-db-<run_id>` 保存新副本 —— 这是正常路径。
2. **种子引导**：缓存未命中时（首次运行、或缓存被 GitHub 清理），
   自动解压仓库内的 `data/seed/sequoia_v2.db.gz` 作为初始数据库。

两级都没有数据时任务才会失败并输出明确错误，避免"跑绿了但一只都没选出来"的假成功。

> **回填接口（baostock 全市场历史 K 线）当前数据仍有问题**，因此不放进自动化流程。
> 需要回填时在本机手动执行 `python main.py --backfill`。
> 种子文件的详情、当前覆盖面与重新生成方式见 [`data/seed/README.md`](data/seed/README.md)。

⚠️ 当前种子只覆盖约 1332 只股票（全市场约 5200 只），策略每天只扫描这部分市场。
补齐历史数据后请重新生成种子。

### 4. 推送方式

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

消息形如：

```
日期：2026-10-09
策略数：3
选股合计：6 只
---
MaVolumeStrategy（2 只）
贵州茅台 SH600519  平安银行 SZ000001

TurtleTradeStrategy（1 只）
...
```

### 5. 注意事项

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
├── .env.example                 # 环境变量模板
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
