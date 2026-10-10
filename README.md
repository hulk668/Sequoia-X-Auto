# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送（默认邮件，可选 PushPlus）。

数据层支持两条通道：[baostock](http://baostock.com)（免费、无需注册）与 akshare（东方财富源）。
两者的复权基准不同（见「数据更新」一节），跨源取数一律通过「锚点 + 比例换算」对齐到库中的后复权序列。
默认在 Actions 上直接使用 akshare（`PREFER_AKSHARE=true`），因为 baostock 免费服务长期不稳定；
关掉该开关则回到「先探测 baostock、不可用再回退 akshare」的老路径。

---

## 运行模式

```bash
python main.py                    # 日常模式：增量补数据 + 跑完所有策略 + 汇总后一次性邮件推送
python main.py --prefer-akshare   # 同上，但数据更新直接走 akshare（不探测 baostock）
python main.py --skip-sync        # 完全不更新数据，直接用库中现有数据选股
python main.py --backfill         # 补数模式：把库里没有的股票一次灌满（可中断续跑）
python main.py --backfill --limit 100   # 补数试跑：只补前 100 只
python main.py --backfill --bars 250 --source qq   # 指定保留根数与数据源
```

`--backfill` 相关参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--bars N` | `400` | 每只票保留最近 N 根日 K（`0` = 不截断）。最长的策略回看是 61 根，400 有 6 倍余量 |
| `--source` | `auto` | `auto` = 东财优先、腾讯兜底；`em` / `qq` 强制单一源 |
| `--limit N` | `0` | 只补前 N 只，试跑用 |
| `--backfill-baostock` | — | 旧的 baostock 通道，仅在两条 HTTP 通道都不通时使用 |

补数只处理**库里一行都没有的股票**。已有数据的票交给日常增量通道
（它用「锚点 + 比例换算」续写，能保证与库中序列同一复权基准）。


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
# 编辑 config.local.toml，填入 SMTP 邮件参数（见 RUNNING.md）
```

`config.local.toml` 已加入 `.gitignore`，不会被提交。也支持传统的 `.env`（见 `.env.example`）。
优先级：**环境变量 > `config.local.toml` > `.env` > 默认值**。

### 3. 准备数据库

新克隆的仓库里没有 `data/sequoia_v2.db`（被 `.gitignore` 排除）。种子压缩包不放在仓库里，
而在 **GitHub Release** 上，两种取法：

```bash
# A) 从 Release 下载（需要 gh CLI，已登录）
TAG=$(cat data/seed/VERSION)
gh release download "$TAG" --pattern 'sequoia_v2.db.gz' --dir data/seed
gunzip -c data/seed/sequoia_v2.db.gz > data/sequoia_v2.db

# A') 没有 gh，就直接到浏览器里下：
#     https://github.com/hulk668/Sequoia-X-Auto/releases  → 找 tag → 下载 sequoia_v2.db.gz

# B) 自己补数（不依赖种子，约 2 分钟跑完全市场）
python main.py --backfill        # 默认只补库里没有的股票，可中断续跑
```

（`gh` 未安装时，也可用 `curl -L -o data/seed/sequoia_v2.db.gz \`
`https://github.com/hulk668/Sequoia-X-Auto/releases/download/$TAG/sequoia_v2.db.gz`。）

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

**邮件通知（推荐通道）**：

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `SMTP_HOST` | ✅ | SMTP 服务器：QQ `smtp.qq.com` / 163 `smtp.163.com` / 钉钉企业邮箱 `smtp.em.dingtalk.com` |
| `SMTP_USER` | ✅ | 发件邮箱**完整地址** |
| `SMTP_PASSWORD` | ✅ | **客户端授权码**（多数邮箱不是登录密码） |
| `MAIL_TO` | ✅ | 收件人地址，多个用逗号分隔 |
| `SMTP_PORT` | 可选 | 默认 `465`（隐式 SSL）；填 `587` 会自动走 STARTTLS |
| `MAIL_FROM_NAME` | 可选 | 发件人显示名，默认「Sequoia-X 选股」 |
| `NOTIFY_CHANNEL` | 可选 | `auto`（默认）/ `email` / `pushplus` / `both` / `none` |

**PushPlus（可选，仅作备用）**：

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

> **⚠️ 凭据从哪来？**
> 在 Actions 上只能来自 GitHub Secrets —— 工作流是通过
> `SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}` 这类写法把它注入成环境变量的。
>
> `.env` / `config.local.toml` / `.env.example` 在 Actions 上都**不可用**：
> - `.env` 与 `config.local.toml` 被 `.gitignore` 排除，checkout 时根本不存在；
> - `.env.example` / `config.example.toml` 只是给人复制用的**模板**，
>   程序永远不会读它们。
>
> **千万不要把真实凭据写进模板文件** —— 它会被提交进仓库、公开可见。
> 如果曾经写过，请立刻去对应平台重置并更新 Secret，
> 光删文件没用（git 历史里还在）。
>
> 本地运行怎么配，见 **[RUNNING.md](RUNNING.md)**。

工作流第一步就会校验通知配置：按 `NOTIFY_CHANNEL` 检查对应参数是否齐备，
没配会立刻报错退出，不会白跑几分钟才在推送阶段失败。

### 2. 定时运行

已配置 `cron: '30 10 * * 1-5'`，即**每周一至周五北京时间 18:30** 自动执行「更新数据 + 选股 + 推送」。

> ⚠️ GitHub 的 cron **一律按 UTC 计算**。工作流里的 `TZ: Asia/Shanghai` 只影响进程内的
> `date.today()` 与日志时间戳，**不影响调度时刻** —— 北京时间 18:30 = UTC 10:30。
> 改时间时记得换算，别直接照抄北京时间。

也可以在 Actions 页面点 **Run workflow** 手动立即执行一次，此时可选：
用 akshare 更新数据（默认开）、或勾选 `skip_sync` 完全不更新数据。

### 3. 数据持久化说明

数据库 `data/sequoia_v2.db` 有 300MB+，超过 GitHub 单文件 100MB 限制且被 `.gitignore` 排除，
因此**数据库本身不入库**，改用「缓存 + 种子」两级方案：

1. **滚动缓存**：每轮运行前恢复最近一次缓存，运行后保存新副本 —— 这是正常路径。
   缓存 key 里带了 `data/seed/VERSION` 的哈希，所以换了新种子、旧缓存自动失效并重新引导，
   不需要手工去 Actions → Caches 清理。
2. **种子引导**：缓存未命中时（首次运行、种子更新、或缓存被 GitHub 清理），
   从 **GitHub Release** 下载 `sequoia_v2.db.gz`（tag 取自 `data/seed/VERSION`）解压成初始数据库。

两级都没有数据时任务才会失败并输出明确错误，避免"跑绿了但一只都没选出来"的假成功。

> **种子为什么放 Release 而不是仓库里**：压缩后 ~76MB、解压后 300MB+，进仓库会拖慢
> **每一次** `actions/checkout`（哪怕当天缓存命中、根本用不到），且超过 50MB 后每次 push
> 都会收到 GitHub 的大文件警告。放 Release 资产则既不占仓库体积、也不影响 clone。
>
> **历史 K 线接口不稳定**，因此补数不放进日常自动化流程。
> 需要补数时在本机执行 `python main.py --backfill`（东财优先、腾讯兜底，可中断续跑），
> 或手动触发 Actions 里的「Sequoia-X 发布数据库种子」工作流。
> 种子详情、当前覆盖面与重新生成方式见 [`data/seed/README.md`](data/seed/README.md)。

当前种子覆盖**全市场 5224 只**（沪深主板 + 创业板 + 科创板；北交所两个接口都拿不到日K）。

### 4. 数据更新：akshare 主用 / baostock 可选

#### 4.1 默认：直接用 akshare（`PREFER_AKSHARE=true`）

Actions 上默认开启 `PREFER_AKSHARE`，**跳过对 baostock 的探测**，直接用 akshare（东方财富源）
拉增量数据。原因：baostock 免费服务长期不稳定（同一天可能通、也可能不通），
「先探路再回退」等于每轮都要白等一次探测；而且它内部用裸 `print()` 往 stdout 吐错误，
不可达时会刷出上百行噪音。

```bash
PREFER_AKSHARE=true python main.py     # 环境变量
python main.py --prefer-akshare        # 命令行开关（只作用于本次运行）
```

```toml
# config.local.toml
prefer_akshare = true
```

#### 4.2 可选：回到 baostock 路径

把 `PREFER_AKSHARE` 关掉（`false` / 不设）后，增量同步走 baostock 的
`query_history_k_data_plus`。这个免费接口**并不总是可用**，常见报错
（都是 baostock 内部用 `print()` 直接打到 stdout 的）：

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

#### 4.3 两源换算（`sync_via_akshare`）

上表两条通道共用同一套换算逻辑：akshare 拉到的数据**不能直接写入**。

⚠️ **不同数据源的复权基准不同**。实测同一只票同一日（sh600000 / 2026-10-09）：

| 数据源 | 复权方式 | 收盘价 |
|---|---|---|
| baostock | 后复权 | 127.52 |
| 东方财富 | 后复权 | 102.23 |

而且两者的日收益率也不一致 —— 直接拼接会让价格序列出现断层，均线、RPS 排名全部失真。

所以只能借用 akshare **前复权序列的相对涨跌**，用库中已有的后复权收盘做锚点换算：

```
k = 库中最后一日后复权收盘 ÷ 该日的前复权收盘
今日后复权价 = 今日前复权价 × k
```

用前复权而非不复权，是为了在除权日也能拿到正确的复权收益率。
实测该换算能**精确复现** baostock 的后复权值（相对误差 0）。
成交量单位也做了对齐（akshare 是「手」，库中存「股」，×100）。

#### 4.4 跳过数据更新

`python main.py --skip-sync`、`SKIP_SYNC=1 python main.py`、`config.local.toml` 里设
`skip_sync = true`，或在 Actions 手动触发时勾选 `skip_sync` ——
**完全不更新数据**（baostock 与 akshare 都不跑，名称刷新也跳过），只跑策略。

此时**选股会基于数据库里已有的数据**，所以推送消息会标出真实的数据截止日期：

```
📅 **2026-10-10**（⚠️ 数据截止 2026-10-09）
```

数据与运行日期一致时不会显示这个提醒。

> 注意 `skip_sync` 与 `prefer_akshare` 是两件事：
> 前者「不更新数据」，后者「换一条通道更新数据」。同时打开时 `skip_sync` 优先。

### 5. 推送方式

一轮运行会**先跑完全部策略并汇总，最后统一推送一次**，不再每个策略单独发一条消息：

- 所有策略默认都走全局 `PUSHPLUS_TOKEN` → 整轮只发 **1 条**汇总消息；
- 若配置了策略专属 token，则按推送目标分组，**每个目标一条**（不同目标之间不会合并）；
- 无选股结果的策略自动跳过，不会出现在消息里。

**股票名称**取自数据库的 `stock_name` 表，而不是每次推送都去查 baostock：

- 名称随数据库一起被缓存，覆盖率已 ≥95% 时直接跳过刷新，不足才去拉一次（单次请求，约 30s），
  失败只告警、不影响流程；
- 若某只股票始终匹配不到名称，会退化为显示代码（如 `SH603325`），
  同时在日志里给出 `N/M 只股票未匹配到名称` 的告警。

> 之所以不逐只查询名称：baostock 在 CI 环境（海外 runner 共享 IP）请求量上来后
> 会开始返回空结果，导致推送里大量股票只剩代码。改成单次批量拉取并落库可规避。

**板块**取自东方财富的 **EM2016 行业分类**（`RPT_F10_BASIC_ORGINFO` 接口），
取三级分类的**第二级**（`交通运输-港口航运-航运` → `港口航运`），并在正文里按板块归类展示：

- 用**批量查询**（`SECUCODE in (...)`，每批 45 只）而不是逐只请求 —— 90 只股票只要 2 次请求；
- 结果缓存在 `stock_board` 表，**有效期 30 天**；只反查本轮选中的股票，命中缓存不再发请求；
- 北交所（`4`/`8` 开头）在东财 F10 里没有数据，直接跳过不发请求；
- 查不到行业的股票排在最后、不带「（板块）」尾巴。

> ❌ **不要用「核心题材」当板块**。早期版本取东财 F10 `RPT_F10_CORETHEME_BOARDTYPE`
> 里 `IS_PRECISE=1` 的第一条，实测**不可靠**：那套数据会把 `一带一路`、`央国企改革`、
> `沪股通` 这类泛主题排在前面。全量比对 90 只票，与 EM2016 行业**无一只相同** ——
> 例如招商南油被标成「一带一路」，而它的真实行业是「港口航运」；
> 建发股份被标成「白酒」（实际是物流）。行业分组也更聚合
> （42 个板块 / 22 个单只 vs 旧口径 49 个板块 / 31 个单只）。

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

### 排版规则

两条渠道的版式各自独立，改版式前先认准是哪一条：

- **邮件**（`notify/email_sender.py`）：HTML 邮件，**table 布局 + 全内联样式**
  （`<style>` 与 class 会被多数客户端剥掉），`multipart/alternative` 另附纯文本兜底。
  没有字数限制，信息密度更高。
- **PushPlus**（`notify/pushplus.py`）：markdown，受下面这套方言限制。

#### 🔴 PushPlus 的 markdown 方言（改 PushPlus 版式前必读）

模板走的是标准 **GFM**，**单个 `\n` 会被折叠成空格**：

| 写法 | 结果 |
| --- | --- |
| `甲\n乙` | ❌ 渲染成 `甲 乙`（同一行） |
| `甲\n\n乙` | ✅ 两个段落 |
| `甲\n\n---\n\n乙` | ✅ 段落 + 分隔线 |
| `- 甲\n- 乙` | ✅ 两项列表（各自独占一行） |

**这就是老版排版最致命的 bug** —— 当时用 `\n` 拼「每行 N 只」，其实从未生效，
渲染出来是一整段，由渲染器在随机位置折行，把「央企国企改革」劈成「央企国企 / 改革」。
现在一律用**空行分段 + 列表项分行**，并在 `tests/test_pushplus.py`
里放了回归测试 `test_block_never_relies_on_single_newline` 守住这条。

#### 排版约定

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

数据没更新到当天时，头部会标出真实数据截止日：
`📅 **2026-10-10**（⚠️ 数据截止 2026-10-09）`。

各策略的中文名、买点、卖点（定义在 `sequoia_x/notify/strategies.py` 的 `STRATEGY_DISPLAY`，
邮件与 PushPlus 两个渠道共用这一份文案）：

| 类名 | 中文名 | 买点 | 卖点（参考） |
| --- | --- | --- | --- |
| `MaVolumeStrategy` | 均线金叉+放量突破 | 5日线上穿20日线，且成交量放大到20日均量的1.5倍 | 跌破20日线，或5日线下穿20日线（死叉） |
| `TurtleTradeStrategy` | 海龟突破新高 | 突破近20日最高价，成交额超1亿、收阳且真涨 | 跌破10日最低价（海龟经典退出） |
| `HighTightFlagStrategy` | 高位旗形缩量 | 40日大涨后近10日缩量窄幅横盘，且不跌破高位 | 跌破旗形整理下沿，或跌破20日线 |
| `LimitUpShakeoutStrategy` | 涨停次日洗盘 | 昨日涨停、今日放量收阴但不破昨收，洗盘不破位 | 跌破涨停日收盘价（支撑失守） |
| `UptrendLimitDownStrategy` | 上升趋势跌停错杀 | 20日线上穿60日线走多头，今日放量跌停，博错杀反抽 | 反弹回补跌停缺口后离场；跌破60日线止损 |
| `RpsBreakoutStrategy` | RPS极强动量 | 120日涨幅排全市场前10%，且股价接近120日新高 | RPS 跌破 90，或跌破20日线 |
| `PrivatePlacementStrategy` | 定增公告监控 | 近7日发布定向增发公告 | 无固定卖点（事件驱动，需自行判断） |

> **卖点是该策略对应的经典退出规则，只作参考提示** —— 系统只负责选股，
> 不跟踪持仓、不会推送卖出提醒，仓位需要自己管理。
>
> 新增策略时在 `STRATEGY_DISPLAY` 里登记一条即可；未登记的类名会原样显示类名、不带买卖点。
> 日志里仍打印英文类名，方便排查。

### 6. 注意事项

- **定时任务依赖默认分支**：工作流文件必须合入默认分支后 `schedule` 才会生效。
- **60 天不活跃会被禁用**：仓库连续 60 天无提交，GitHub 会自动暂停定时任务，需在 Actions 页面手动重新启用；
  仓库有其它提交活动即可保持激活。
- **cron 可能延迟**：GitHub 定时任务在高峰期可能延迟数分钟到数十分钟，且不保证 100% 触发。
- **缓存会被清理**：`actions/cache` 连续 7 天未被访问会被 GitHub 清掉，
  届时工作流会自动从 **GitHub Release** 下载种子重新引导，无需人工干预。
  前提是 **`data/seed/VERSION` 里写的那个 tag 的 Release 资产必须存在**（别删）。

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── .github/workflows/
│   ├── daily.yml                # 定时选股（缓存 + Release 种子两级数据）
│   └── seed.yml                 # 手动触发：打包数据库并发布为 Release 种子
├── main.py                      # 入口：argparse 分发日常/补数模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── RUNNING.md                   # 【本地运行指南】安装 / 配置 / 建库 / 排错
├── config.example.toml          # 本地配置文件模板（cp 成 config.local.toml 后填值）
├── config.local.toml            # 【已 gitignore】本机真实配置，不会被提交
├── .env.example                 # 传统环境变量模板（仍兼容）
├── scripts/
│   └── pack_seed.py             # 数据库 → 种子压缩包（裁剪 + VACUUM + gzip -9）
├── data/                        # SQLite 数据库（运行时生成，不入 git）
│   └── seed/                    # 只跟踪 VERSION 与 README.md；gz 放 GitHub Release
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理（env > config.local.toml > .env）
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   ├── engine.py            # 数据引擎（akshare/baostock 增量同步 + 行业反查 + SQLite）
│   │   └── backfill.py          # 补数引擎（东财日K为主、腾讯兜底，多线程 + 可续跑）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   ├── rps_breakout.py      # RPS 突破策略
│   │   └── private_placement.py # 定增公告监控
│   └── notify/
│       ├── __init__.py          # build_notifier：按配置组装通知渠道
│       ├── strategies.py        # 策略中文名/买点/卖点 + 股票链接（各渠道共用）
│       ├── email_sender.py      # 邮件汇总推送（HTML 版式 + SMTP 发送）
│       └── pushplus.py          # PushPlus 汇总推送（仅负责渲染 + 发送）
└── tests/                       # 属性测试（hypothesis）
```

---

## 数据说明

- **数据源**：默认 [akshare](https://akshare.akfamily.xyz)（东方财富源）；可切回 [baostock](http://baostock.com)
- **复权方式**：库内统一存**后复权**（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **三张表**：`stock_daily`（行情 K 线）、`stock_name`（股票名称）、`stock_board`（行业板块缓存）
- **日常增量**：并发拉取（akshare 走 8 线程 HTTP，baostock 走 8 进程），2~3 分钟完成全市场更新
- **注意**：策略是**确定性**的 —— 相同的输入必然得到逐字节相同的输出。
  如果两次结果完全一样，先查 `SELECT MAX(date) FROM stock_daily` 确认数据是否真的更新了，
  而不是怀疑策略。

---

## 许可证 | License

MIT
