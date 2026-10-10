# SequoiaX-AutoPlus

> A 股量化选股系统 | A-Share Quantitative Stock Selection System
>
> 7 个确定性策略 · 收盘后自动选股 · 邮件 / PushPlus 汇总推送 · 可零成本跑在 GitHub Actions 上

---

## 简介 | Introduction

SequoiaX-AutoPlus 是面向 A 股市场的量化选股系统，在 [sngyai/Sequoia-X](https://github.com/sngyai/Sequoia-X)
的 V2 架构与策略集之上做了工程化扩展。

每个交易日收盘后，它会自动完成一整套流程：

```
拉取增量行情 → 刷新股票名称 → 跑完全部策略 → 反查所属板块 → 汇总成一条消息推送
```

设计上只保三条底线：

- **确定性** —— 相同输入必然得到逐字节相同的输出。结果没变，先查数据有没有更新，而不是怀疑策略。
- **可离线复现** —— 全市场 K 线落在本地 SQLite 里，选股不依赖任何外部接口的实时可用性。
- **零成本部署** —— 不需要服务器，GitHub Actions 定时跑，数据用「缓存 + Release 种子」两级持久化。

数据源只有一条：[akshare](https://akshare.akfamily.xyz)（东方财富源）。见
[数据更新](#4-数据更新只走-akshare)。

---

## 致谢 | Acknowledgements

本项目站在 [**sngyai/Sequoia-X**](https://github.com/sngyai/Sequoia-X) 的肩膀上。

| | |
|---|---|
| 上游项目 | [Sequoia-X: 王者回归 \| The King Returns](https://github.com/sngyai/Sequoia-X) |
| 作者 | **YuDong Yang**（[@sngyai](https://github.com/sngyai)） |
| 许可证 | MIT |
| 定位 | A 股自动选股系统 —— 多种技术形态自动扫描，收盘后运行并推送 |

SequoiaX-AutoPlus 的 **V2 架构骨架与全部 6 个 K 线策略**（TurtleTrade / MaVolume /
HighTightFlag / LimitUpShakeout / UptrendLimitDown / RpsBreakout）均源自该项目：

- 策略的入选条件、回看窗口、排序口径保持原样，未做主观改动；
- `sequoia_x/strategy/` 下的类名与文件名也沿用上游，便于与源码对照。

本仓库在此基础上的扩展见 [本项目的修改](#与上游的差异--changes-from-upstream)。

> 🙏 **如果这个项目对你有帮助，请先去给上游 [sngyai/Sequoia-X](https://github.com/sngyai/Sequoia-X) 点一个 Star** ——
> 策略思路是它给的，这里做的更多是工程层面的补齐。

### 与上游的差异 | Changes from Upstream

| 方面 | 上游 Sequoia-X | 本项目 SequoiaX-AutoPlus |
|---|---|---|
| 策略集 | 6 个 K 线策略 | 6 个 + **PrivatePlacement**（定增公告监控） |
| 数据源 | baostock | **仅 akshare**（东财源，锚点比例换算对齐复权基准） |
| 通知 | 飞书 Webhook | **邮件（HTML）+ PushPlus**，一轮汇总一条，内容按板块归类 |
| 本地配置 | 环境变量 / `.env` | **收敛到 `config.local.toml` 单文件** |
| 数据持久化 | — | Actions **滚动缓存 + Release 种子**两级冷启动 |
| 取数性能 | 逐只查库 | **全市场快照复用**（见 [性能设计](#性能设计--performance)） |
| 安全 | — | 提交前自动扫描疑似 token 泄漏 |

---

## 内置策略 | Strategies

| 策略类名 | 中文名 | 买点（严格对应源码入选条件） | 卖点（参考） |
|---|---|---|---|
| `TurtleTradeStrategy` | 海龟突破新高 | 突破近 20 日最高价，成交额超 1 亿、收阳且真涨 | 跌破 10 日最低价（海龟经典退出） |
| `MaVolumeStrategy` | 均线金叉+放量突破 | 5 日线上穿 20 日线，且成交量放大到 20 日均量的 1.5 倍 | 跌破 20 日线，或 5 日线下穿 20 日线（死叉） |
| `HighTightFlagStrategy` | 高位旗形缩量 | 40 日大涨后近 10 日缩量窄幅横盘，且不跌破高位 | 跌破旗形整理下沿，或跌破 20 日线 |
| `LimitUpShakeoutStrategy` | 涨停次日洗盘 | 昨日涨停、今日放量收阴但不破昨收，洗盘不破位 | 跌破涨停日收盘价（支撑失守） |
| `UptrendLimitDownStrategy` | 上升趋势跌停错杀 | 20 日线上穿 60 日线走多头，今日放量跌停，博错杀反抽 | 反弹回补跌停缺口后离场；跌破 60 日线止损 |
| `RpsBreakoutStrategy` | RPS 极强动量 | 120 日涨幅排全市场前 10%，且股价接近 120 日新高 | RPS 跌破 90，或跌破 20 日线 |
| `PrivatePlacementStrategy` | 定增公告监控 | 近 7 日发布定向增发公告 | 无固定卖点（事件驱动，需自行判断） |

> **卖点是该策略对应的经典退出规则，只作参考提示。** 系统只负责选股，不跟踪持仓、
> 不会推送卖出提醒，仓位需要自己管理。
>
> 策略展示文案集中在 `sequoia_x/notify/strategies.py` 的 `STRATEGY_DISPLAY`，
> 邮件与 PushPlus 两个渠道共用同一份，避免口径漂移。新增策略时在那里登记一条即可；
> 未登记的类名会原样显示类名、不带买卖点。日志里仍打印英文类名，方便排查。

---

## 快速开始 | Quick Start

> 📖 本地安装、排错、邮箱配置等细节见 **[RUNNING.md](RUNNING.md)**。

### 环境要求

- **Python >= 3.11**（本地配置用标准库 `tomllib` 读取，3.10 及以下不支持）
- 能访问东方财富接口（akshare）
- 操作系统不限：Windows / macOS / Linux 均可

### 1. 安装依赖

```bash
git clone https://github.com/hulk668/SequoiaX-AutoPlus.git
cd SequoiaX-AutoPlus

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

python -m pip install --upgrade pip
pip install akshare "pydantic-settings>=2.0" rich pandas requests
```

> 依赖也声明在 `pyproject.toml` 里，但**不要用 `pip install .`** ——
> 项目是 flat-layout 且没配 `[build-system]`，自动发现容易报错。

### 2. 配置（只改一个文件）

```bash
cp config.example.toml config.local.toml
```

然后编辑 `config.local.toml`，至少配好一组通知通道（推荐邮件）：

```toml
db_path = "data/sequoia_v2.db"
start_date = "2024-01-01"

notify_channel = "auto"          # auto / email / pushplus / both / none

# ── 邮件（推荐）──
# ⚠️ smtp_password 多数邮箱要填「客户端授权码」，不是登录密码
smtp_host = "smtp.qq.com"        # 163 → smtp.163.com；钉钉企业邮箱 → smtp.em.dingtalk.com
smtp_port = 465                  # 465 → 隐式 SSL；其余端口自动走 STARTTLS
smtp_user = "you@qq.com"
smtp_password = "你的授权码"
mail_from = ""                   # 留空则用 smtp_user
mail_to = "收件人@example.com"    # 多个用逗号分隔
```

> **为什么推荐邮件**：PushPlus 正文有 **2 万字上限**，股票池补齐全市场（5000+ 只）
> 后很容易超限，服务端直接报 `code 999 发送内容过大`。邮件没有这个限制，
> 而且收到的是带样式的 HTML 版。

**配置优先级：环境变量 > `config.local.toml` > 代码内默认值。**
`config.local.toml` 已在 `.gitignore` 里，**不会被提交**，可以放心写真实 token。

> ⚠️ 如果按 `notify_channel` 选定的通道参数没配全，程序会在**启动时**直接报错退出 ——
> 比跑完几千只股票再在推送阶段失败体面得多。临时只想跑策略不要通知时，设 `notify_channel = "none"`。

**配完先自检一次（强烈建议）**，5 秒出结果，不必等一整天：

```bash
python scripts/test_mail.py --preview   # 不联网，只渲染示例邮件看排版（不需要密码）
python scripts/test_mail.py --no-send   # 测连接 + 登录，不发信
python scripts/test_mail.py             # 完整链路：连接 → 登录 → 给自己发一封测试邮件
```

### 3. 准备数据库

`data/sequoia_v2.db`（解压后 300MB+）被 `.gitignore` 排除，新克隆的仓库里没有，三种取法：

**A. 从 Release 种子解压（推荐，几秒完成）**

种子压缩包不在仓库里，在 **GitHub Release** 上：

```bash
TAG=$(cat data/seed/VERSION)          # 例如 seed-2026-10-10

# 有 gh CLI
gh release download "$TAG" --pattern 'sequoia_v2.db.gz' --dir data/seed

# 或直接 curl（公开仓库可匿名下载）
curl -L -o data/seed/sequoia_v2.db.gz \
  "https://github.com/hulk668/SequoiaX-AutoPlus/releases/download/$TAG/sequoia_v2.db.gz"

gunzip -c data/seed/sequoia_v2.db.gz > data/sequoia_v2.db
rm -f data/seed/sequoia_v2.db.gz
```

也可以直接到浏览器打开 <https://github.com/hulk668/SequoiaX-AutoPlus/releases>，
找到 `data/seed/VERSION` 里写的那个 tag，下载后用 7-Zip / WSL 解压。

**B. 自己补数（不依赖种子）**

```bash
python main.py --backfill                 # 补库里没有的股票，可中断续跑
python main.py --backfill --limit 100     # 先小批试跑
python main.py --backfill --bars 250 --source qq   # 指定保留根数与数据源
```

**C. 已经跑过 → 跳过**（首次 `main.py` 会自动把缺口补到最新）

### 4. 日常运行

```bash
python main.py
```

想让每个交易日收盘后自动跑，用 crontab（注意 `cron` 用的是本机时区）：

```cron
30 18 * * 1-5 cd /root/SequoiaX-AutoPlus && .venv/bin/python main.py >> log.txt 2>&1
```

---

## 运行模式与常用参数

```bash
python main.py                              # 日常：增量补数据 + 跑全部策略 + 汇总推送
python main.py --skip-sync                  # 完全不更新数据，直接用库中现有数据选股
python main.py --backfill                   # 补数：东财日K拉全市场历史（可中断续跑）
python main.py --backfill --limit 200       # 补数试跑：只补前 200 只
python main.py --backfill --bars 250 --source qq   # 指定保留根数与数据源
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--skip-sync` | off | 完全不更新数据（行情与名称刷新都不跑），只跑策略。等价于配置里 `skip_sync = true` |
| `--backfill` | off | 补数模式，只处理**库里一行都没有的股票** |
| `--bars N` | `400` | 配合 `--backfill`：每只票保留最近 N 根日 K（`0` = 不截断） |
| `--source` | `auto` | 配合 `--backfill`：`auto` = 东财优先、腾讯兜底；`em` / `qq` 强制单一源 |
| `--limit N` | `0` | 配合 `--backfill`：只补前 N 只，试跑用 |

已有数据的票交给日常增量通道 —— 它用「锚点 + 比例换算」续写，能保证与库中序列同一复权基准。

---

## GitHub Actions 部署 | Deploy on Actions

不需要服务器。工作流文件：`.github/workflows/daily.yml`（每日选股）与
`.github/workflows/seed.yml`（手动发布数据库种子）。

### 1. 配置 Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**。

**邮件通知（推荐通道）**：

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `SMTP_HOST` | ✅ | SMTP 服务器：QQ `smtp.qq.com` / 163 `smtp.163.com` / 钉钉企业邮箱 `smtp.em.dingtalk.com` |
| `SMTP_USER` | ✅ | 发件邮箱**完整地址** |
| `SMTP_PASSWORD` | ✅ | **客户端授权码**（多数邮箱不是登录密码） |
| `MAIL_TO` | ✅ | 收件人地址，多个用逗号分隔 |
| `SMTP_PORT` | 可选 | 默认 `465`（隐式 SSL）；填 `587` 会自动走 STARTTLS |
| `MAIL_FROM_NAME` | 可选 | 发件人显示名，默认「SequoiaX-AutoPlus 选股」 |
| `NOTIFY_CHANNEL` | 可选 | `auto`（默认）/ `email` / `pushplus` / `both` / `none` |

**PushPlus（可选，作备用渠道）**：

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `PUSHPLUS_TOKEN` | 可选 | PushPlus 全局推送 Token。⚠️ 正文有 **2 万字上限**，全市场股票池下容易超（`code 999`） |
| `STRATEGY_WEBHOOK_MA_VOLUME` | 可选 | 均线放量策略专属 Token |
| `STRATEGY_WEBHOOK_TURTLE` | 可选 | 海龟突破策略专属 Token |
| `STRATEGY_WEBHOOK_FLAG` | 可选 | 高窄旗形策略专属 Token |
| `STRATEGY_WEBHOOK_SHAKEOUT` | 可选 | 涨停洗盘策略专属 Token |
| `STRATEGY_WEBHOOK_LIMIT_DOWN` | 可选 | 上升跌停策略专属 Token |
| `STRATEGY_WEBHOOK_RPS` | 可选 | RPS 突破策略专属 Token |
| `STRATEGY_WEBHOOK_PRIVATE_PLACEMENT` | 可选 | 定增策略专属 Token |

未配置专属 Token 的策略会自动回落到全局 `PUSHPLUS_TOKEN`（空值会被忽略，不会覆盖默认 token）。

> **⚠️ 凭据只能来自 GitHub Secrets。** 工作流通过 `SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}`
> 这类写法把它注入成环境变量。
>
> `.env` 与 `config.local.toml` 在 Actions 上**根本不存在**（被 `.gitignore` 排除）；
> `config.example.toml` 只是给人复制用的**模板**，程序永远不会读它。
>
> **千万不要把真实凭据写进模板文件** —— 它会被提交进仓库、公开可见。若曾经写过，
> 立刻去对应平台重置并更新 Secret：光删文件没用，git 历史里还在。

工作流的第一步就会校验通知配置：按 `NOTIFY_CHANNEL` 检查对应参数是否齐备，没配会立即报错退出，
不会白跑几分钟才在推送阶段失败。

### 2. 定时运行

已配置 `cron: '30 10 * * 1-5'`，即**每周一至周五北京时间 18:30** 自动执行
「更新数据 + 选股 + 推送」。

> ⚠️ **GitHub 的 cron 一律按 UTC 计算。** 工作流里的 `TZ: Asia/Shanghai` 只影响进程内的
> `date.today()` 与日志时间戳，**不影响调度时刻** —— 北京时间 18:30 = UTC 10:30。
> 改时间时记得换算，照抄北京时间会跑早 8 小时。

也可以在 Actions 页面点 **Run workflow** 手动立即执行一次，此时可勾选 `skip_sync`
完全不更新数据。

### 3. 数据持久化：缓存 + 种子

数据库 300MB+，超过 GitHub 单文件 100MB 硬限制且被 `.gitignore` 排除，因此**数据库本身不入库**，
改用两级方案：

1. **滚动缓存**（正常路径）：每轮运行前恢复最近一次缓存，运行后保存新副本。
   缓存 key 里带了 `data/seed/VERSION` 的哈希 —— 换了新种子，旧缓存自动失效并重新引导，
   不需要手工去 Actions → Caches 清理。
2. **Release 种子**（冷启动）：缓存未命中时（首次运行、种子更新、或缓存被 GitHub 清理），
   从 **GitHub Release** 下载 `sequoia_v2.db.gz`（tag 取自 `data/seed/VERSION`）解压成初始数据库。

两级都拿不到数据时任务才会**明确失败并给出排查方向**，避免「跑绿了但一只都没选出来」的假成功。
除此之外还有一道兜底校验：若股票池少于 `MIN_SYMBOLS`（默认 4000）只，直接拒绝继续 ——
缓存里的库可能退化成残缺池（如只剩沪市主板 1332 只），与其推一份只覆盖 1/4 市场的结果，
不如响亮地失败。

> **种子为什么放 Release 而不是仓库里**：压缩后 ~76MB、解压后 300MB+，进仓库会拖慢
> **每一次** `actions/checkout`（哪怕当天缓存命中、根本用不到），且超过 50MB 后每次 push
> 都会收到大文件警告。放 Release 资产则既不占仓库体积、也不影响 clone。
>
> 种子详情、覆盖面与重新生成方式见 [`data/seed/README.md`](data/seed/README.md)。

当前种子覆盖**全市场 5200+ 只**（沪深主板 + 创业板 + 科创板；北交所两个接口都拿不到日 K）。

### 4. 数据更新：只走 akshare

本项目**只有一条数据通道**：akshare（东方财富源）。早期版本曾并行维护 baostock 通道，
现已整体移除 —— 它的免费服务长期不稳定（同一天可能通、也可能不通），
「先探测再回退」等于每轮白等一次探测，而且它内部用裸 `print()` 往 stdout 吐错误，
不可达时刷出上百行噪音。少一条通道，也就少一份需要对齐口径的差异。

#### 4.1 两源换算（`sync_via_akshare`）

东财数据**不能直接写入**库里，因为**不同数据源的复权基准不同**。实测同一只票同一日
（sh600000 / 2026-10-09）：

| 数据源 | 复权方式 | 收盘价 |
|---|---|---|
| baostock | 后复权 | 127.52 |
| 东方财富 | 后复权 | 102.23 |

而且两者的日收益率也不一致 —— 直接拼接会让价格序列出现断层，均线、RPS 排名全部失真。

所以只借用东财**前复权序列的相对涨跌**，用库中已有的后复权收盘做锚点换算：

```
k = 库中最后一日后复权收盘 ÷ 该日的前复权收盘
今日后复权价 = 今日前复权价 × k
```

用前复权而非不复权，是为了在除权日也能拿到正确的复权收益率。实测该换算能**精确复现**
原后复权序列（相对误差 0）。成交量单位也做了对齐（akshare 是「手」，库中存「股」，×100）。

#### 4.2 跳过数据更新

`python main.py --skip-sync`、`SKIP_SYNC=1 python main.py`、或配置里 `skip_sync = true`，
以及 Actions 手动触发时勾选 `skip_sync` —— **完全不更新数据**（不碰行情，名称刷新也跳过），
只跑策略。

此时选股基于库里已有的数据，所以推送消息会标出真实的数据截止日期：

```
📅 **2026-10-10**（⚠️ 数据截止 2026-10-09）
```

数据与运行日期一致时不会显示这个提醒。

### 5. 推送与排版规则

一轮运行会**先跑完全部策略并汇总，最后统一推送一次**，不再每个策略单独发一条消息：

- 所有策略默认都走全局 `PUSHPLUS_TOKEN` → 整轮只发 **1 条**汇总消息；
- 若配置了策略专属 token，则按推送目标分组，**每个目标一条**（不同目标之间不会合并）；
- 无选股结果的策略自动跳过，不会出现在消息里。

**股票名称**取自数据库的 `stock_name` 表：覆盖率 ≥95% 时直接跳过刷新，不足才批量拉一次；
某只股票始终匹配不到名称时退化为显示代码（如 `SH603325`），并在日志里给出
`N/M 只股票未匹配到名称` 的告警。

**板块**取自东方财富的 **EM2016 行业分类**（`RPT_F10_BASIC_ORGINFO`），取三级分类的
**第二级**（`交通运输-港口航运-航运` → `港口航运`），在正文里按板块归类展示：

- 用**批量查询**（`SECUCODE in (...)`，每批 45 只）而不是逐只请求 —— 90 只股票只要 2 次请求；
- 结果缓存在 `stock_board` 表，**有效期 30 天**；只反查本轮选中的股票，命中缓存不再发请求；
- 北交所（`4`/`8` 开头）在东财 F10 里没有数据，直接跳过不发请求；
- 查不到行业的股票排在最后、不带「（板块）」尾巴。

> ❌ **不要用「核心题材」当板块**。早期版本取东财 F10 `RPT_F10_CORETHEME_BOARDTYPE`
> 里 `IS_PRECISE=1` 的第一条，实测**不可靠**：那套数据会把 `一带一路`、`央国企改革`、
> `沪股通` 这类泛主题排在前面。全量比对 90 只票，与 EM2016 行业**无一只相同** ——
> 例如招商南油被标成「一带一路」，而它的真实行业是「港口航运」；建发股份被标成「白酒」
> （实际是物流）。换成 EM2016 后行业分组也更聚合（42 个板块 / 22 个单只
> vs 旧口径 49 个板块 / 31 个单只）。

消息形如（**多只的板块各占一行**，单只板块压成一行，板块名加粗，个股用顿号相连）：

```
📅 **2026-10-10** 📊 **2** 个策略 · 共 **7** 只

---

**① 均线金叉+放量突破**（6 只）

🎯 买点：5日线上穿20日线，且成交量放大到20日均量的1.5倍

🛑 卖点：跌破20日线，或5日线下穿20日线（死叉）

- **电力**　　　[福能股份](...)、[广西能源](...)
- **港口航运**　[秦港股份](...)、[招商南油](...)
- [山东高速](...)（公路铁路）、[上海洗霸](...)（环保）
- [同仁堂](...)（中药生产）、[大商股份](...)（一般零售）

---

**② 海龟突破新高**（1 只）

🎯 买点：突破近20日最高价，成交额超1亿、收阳且真涨

🛑 卖点：跌破10日最低价（海龟经典退出）

- [中信证券](...)（证券）

---

*板块为东财行业分类；买卖点为策略信号参考，不构成投资建议*
```

#### 排版规则

两条渠道的版式各自独立，改版式前先认准是哪一条：

- **邮件**（`notify/email_sender.py`）：HTML 邮件，**table 布局 + 全内联样式**
  （`<style>` 与 class 会被多数客户端剥掉），`multipart/alternative` 另附纯文本兜底。
  没有字数限制，信息密度更高。
- **PushPlus**（`notify/pushplus.py`）：markdown，受下面这套方言限制。

##### 🔴 PushPlus 的 markdown 方言（改 PushPlus 版式前必读）

模板走的是标准 **GFM**，**单个 `\n` 会被折叠成空格**：

| 写法 | 结果 |
| --- | --- |
| `甲\n乙` | ❌ 渲染成 `甲 乙`（同一行） |
| `甲\n\n乙` | ✅ 两个段落 |
| `甲\n\n---\n\n乙` | ✅ 段落 + 分隔线 |
| `- 甲\n- 乙` | ✅ 两项列表（各自独占一行） |

**这就是老版排版最致命的 bug** —— 当时用 `\n` 拼「每行 N 只」，其实从未生效，
渲染出来是一整段，由渲染器在随机位置折行，把「央企国企改革」劈成「央企国企 / 改革」。
现在一律用**空行分段 + 列表项分行**，并在 `tests/test_pushplus.py` 里放了回归测试
`test_block_never_relies_on_single_newline` 守住这条。

##### 排版约定

| 规则 | 原因 |
| --- | --- |
| **只数 ≥2 的板块各占一个列表项（一行）**，用 `- ` 强制换行 | 换行可靠；渲染器还会给续行加悬挂缩进 |
| **只有 1 只的板块压成若干行**，写作 `名称（板块）` | 实测 90 只票散在 42 个板块、其中 22 个只有 1 只；每只独占一行会让页面拉长近一倍，而那一行只服务一只票 |
| 板块名**加粗**，个股是链接（微信里显示为蓝色） | 靠字重 + 颜色把「板块」和「个股」分成两层 |
| 个股之间用**顿号 `、`** | 中文里的并列就该用顿号，比 ` · ` 更像是同一组 |
| 板块列用**全角空格 `U+3000`** 补齐到等宽 | ASCII 空格在 HTML 里会被折叠，只有 `U+3000`/`U+00A0` 能真正对齐 |
| 半角名（`REITs`）用 `U+00A0` 补零头 | 全角空格只能补整格，不补零头会歪半格 |
| 多只板块按**只数从多到少**排；单只板块按**名称**排 | 本次选股集中在哪几个板块一眼可见，单只的那几行也有秩序 |
| 板块名超长时只留一个空格分隔 | 否则板块名和个股会贴在一起，且个股被推出屏幕 |
| 一只都没查到行业时退回纯列表（每项 5 只） | 没有「（板块）」尾巴，单只短得多，可以多放 |

> 一个板块内个股多、一行放不下时**不手动拆行** —— 交给渲染器折行，
> 列表项的悬挂缩进会让折下来的部分仍然属于同一项。
> 手动拆行反而会因为拿不到「真正的换行手段」而错位。

### 6. 注意事项

- **定时任务依赖默认分支**：工作流文件必须合入默认分支后 `schedule` 才会生效。
- **60 天不活跃会被禁用**：仓库连续 60 天无提交，GitHub 会自动暂停定时任务，需在 Actions
  页面手动重新启用；仓库有其它提交活动即可保持激活。
- **cron 可能延迟**：GitHub 定时任务在高峰期可能延迟数分钟到数十分钟，且不保证 100% 触发。
- **缓存会被清理**：`actions/cache` 连续 7 天未被访问会被 GitHub 清掉，届时工作流会自动从
  **GitHub Release** 下载种子重新引导，无需人工干预。前提是 `data/seed/VERSION` 里写的那个
  tag 的 Release 资产**必须存在**（别删）。
- **密钥泄漏防护**：工作流会在提交前扫描疑似 token。PushPlus 的 token 是 32 位十六进制串，
  正常代码/文档里不应出现这种形态，命中即报错终止。

---

## 性能设计 | Performance

### 全市场快照复用（`DataEngine.market_panel()`）

初版实现里，6 个 K 线策略各自持有「逐只查库」的写法：每个策略先取一遍股票列表，
再对每只票单独 `sqlite3.connect()` + 查询。全市场 5000+ 只 × 6 个策略 ≈ **3 万次连接**，
而 `sqlite3.connect()` 的固定开销比查询本身还大（实测 3.4ms/只 vs 单连接复用 0.9ms/只）。

现在改为**一次加载、全员共享**：

```python
# sequoia_x/strategy/base.py
def bars_by_symbol(self) -> dict[str, pd.DataFrame]:
    """取全市场 K 线快照，返回 {代码: K 线}。
    所有策略都应该走这个方法取数，不要自己逐只查库。"""
    return self.engine.market_panel()
```

`market_panel()` 的三层优化：

1. **单连接复用** —— 一个 `sqlite3.connect()` 跑完全部查询，摊掉连接开销；
2. **尾部截断** —— `ORDER BY date DESC LIMIT 130` 再反转，避免整表读进内存
   （实测整表批量反而更差：26.7s / 峰值内存 1197MB）；
3. **进程内缓存** —— 同一轮内 6 个策略共享一份快照，写入数据后自动作废。

`_PANEL_BARS = 130` 是**硬下限推导值，不是随手取的**：最长回看来自 RPS ——
`shift(120)` 需 121 根、`rolling(120)` 需 120 根，其余策略最多 61 根 → 121 是下限，
取 130 留余量。

> ⚠️ **截断长度必须 ≥ 任何策略的 `_MIN_BARS`**，否则窗口算不满 → 条件恒不成立 →
> **静默少选票**（最难查的一类 bug）。`tests/test_strategy.py` 里有回归测试
> `test_panel_bars_covers_every_strategy` 守着这条 —— 新增回看更长的策略时会直接失败提醒。
>
> 另外，`RpsBreakoutStrategy` 虽然口径上是横截面排名，但「120 日涨幅」与「120 日滚动最高价」
> 都是单只票自己的序列计算，横截面只发生在最后一步 `rank(pct=True)`，
> 所以同样能复用快照，不必整表读一遍。

**实测效果：全流程 105.09s → 30.20s（约 3.5x），6 个策略结果与优化前逐字节一致。**

---

## 目录结构 | Project Structure

```
SequoiaX-AutoPlus/
├── .github/workflows/
│   ├── daily.yml                # 定时选股（缓存 + Release 种子两级数据）
│   └── seed.yml                 # 手动触发：打包数据库并发布为 Release 种子
├── main.py                      # 入口：argparse 分发日常 / 补数模式
├── pyproject.toml               # 依赖声明 + ruff / pytest 配置
├── README.md                    # 本文件
├── RUNNING.md                   # 【本地运行指南】安装 / 配置 / 建库 / 排错
├── config.example.toml          # 配置模板（cp 成 config.local.toml 后填值，本文件会入库）
├── config.local.toml            # 【已 gitignore】本机真实配置，不会被提交
├── scripts/
│   ├── pack_seed.py             # 数据库 → 种子压缩包（裁剪 + VACUUM + gzip -9）
│   └── test_mail.py             # 邮件自检：连接 / 登录 / 发测试邮件 / 渲染预览
├── data/                        # SQLite 数据库（运行时生成，不入 git）
│   └── seed/                    # 只跟踪 VERSION 与 README.md；gz 放 GitHub Release
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理（env > config.local.toml）
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   ├── engine.py            # 数据引擎（akshare 增量同步 + 快照 + 行业反查 + SQLite）
│   │   └── backfill.py          # 补数引擎（东财日K为主、腾讯兜底，多线程 + 可续跑）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类（bars_by_symbol 统一取数入口）
│   │   ├── turtle_trade.py      # 海龟突破新高
│   │   ├── ma_volume.py         # 均线金叉+放量突破
│   │   ├── high_tight_flag.py   # 高位旗形缩量
│   │   ├── limit_up_shakeout.py # 涨停次日洗盘
│   │   ├── uptrend_limit_down.py# 上升趋势跌停错杀
│   │   ├── rps_breakout.py      # RPS 极强动量
│   │   └── private_placement.py # 定增公告监控
│   └── notify/
│       ├── __init__.py          # build_notifier：按配置组装通知渠道
│       ├── strategies.py        # 策略中文名 / 买点 / 卖点 + 股票链接（各渠道共用）
│       ├── email_sender.py      # 邮件汇总推送（HTML 版式 + SMTP 发送）
│       └── pushplus.py          # PushPlus 汇总推送（仅负责渲染 + 发送）
└── tests/                       # 单元 / 属性测试（pytest + hypothesis）
```

---

## 数据说明

- **数据源**：[akshare](https://akshare.akfamily.xyz)（东方财富源），唯一通道
- **复权方式**：库内统一存**后复权**（hfq）—— 历史价格不变，适合增量存储，避免除权导致数据错乱
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **三张表**：
  - `stock_daily` —— 行情 K 线（symbol / date / open / high / low / close / volume / turnover）
  - `stock_name` —— 股票名称（symbol / name）
  - `stock_board` —— 行业板块缓存（30 天有效期，可随时删，删了下一轮自动重建）
- **日常增量**：并发拉取（akshare 走 16 线程 HTTP），2~3 分钟完成全市场更新
- **注意**：策略是**确定性**的 —— 相同的输入必然得到逐字节相同的输出。
  如果两次结果完全一样，先查 `SELECT MAX(date) FROM stock_daily` 确认数据是否真的更新了，
  而不是怀疑策略。

### 开发与测试

```bash
pip install pytest hypothesis pytest-mock    # 仅在开发时需要
pytest -q
```

---

## 许可证 | License

MIT。

上游 [sngyai/Sequoia-X](https://github.com/sngyai/Sequoia-X) 同为 MIT 许可，
本项目在其基础上派生，保留原作者的著作权声明。
