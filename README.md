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

### 2. 定时运行

已配置 `cron: '15 11 * * 1-5'`，即**每周一至周五北京时间 19:15** 自动执行增量更新 + 选股 + 推送。
也可以在 Actions 页面点 **Run workflow** 手动立即执行一次。

### 3. 数据持久化说明

数据库 `data/sequoia_v2.db` 有 117MB，超过 GitHub 单文件 100MB 限制且被 `.gitignore` 排除，
因此**不入库**，改用 `actions/cache` 滚动缓存持久化：

- 每轮运行前按 `restore-keys: sequoia-db-` 恢复最近一次缓存；
- 运行后用 `sequoia-db-<run_id>` 保存新副本；
- ⚠️ **缓存为空时任务会直接失败**：工作流已移除自动回填，缓存未命中时无法增量更新，
  会以明确错误退出，避免"跑绿了但一只都没选出来"的假成功。

> 回填接口（baostock 全市场历史 K 线）当前数据仍有问题，因此不放进自动化流程。
> 需要回填时在本机手动执行 `python main.py --backfill`。
> 若要让 Actions 上的缓存有初始数据，可后续改为从 Release 附件下载现成数据库。

### 4. 推送方式

一轮运行会**先跑完全部策略并汇总，最后统一推送一次**，不再每个策略单独发一条消息：

- 所有策略默认都走全局 `PUSHPLUS_TOKEN` → 整轮只发 **1 条**汇总消息；
- 若配置了策略专属 token，则按推送目标分组，**每个目标一条**（不同目标之间不会合并）；
- 无选股结果的策略自动跳过，不会出现在消息里。

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
- **缓存会被清理**：`actions/cache` 连续 7 天未被访问会被 GitHub 清掉，届时需重新准备初始数据库。

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── .github/workflows/daily.yml  # GitHub Actions：定时选股 + 手动回填
├── main.py                      # 入口：argparse 分发日常/回填模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库（运行时生成，不入 git）
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
