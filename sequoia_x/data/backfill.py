"""全市场历史 K 线补数：双数据源驱动，可中断续跑。

## 为什么需要它

原来的 `DataEngine.backfill()` 走 baostock，但 baostock 免费服务长期不稳定 ——
实测 `query_stock_basic` 能通、`query_history_k_data_plus` 却卡死 5 分钟不返回。
补数是个几千只股票的长任务，中途卡死一次就前功尽弃。

这里改用两条更稳的 HTTP 通道（详见下面的「数据源」一节），都能按股票粒度续跑。

## 只补「库里完全没有」的股票

`pending()` 只返回 `stock_daily` 里**一行都没有**的代码，不碰已有数据的股票。
原因：已有数据的股票由日常增量通道（`sync_via_akshare`）用「锚点 + 比例换算」
续写，能保证与库中已有序列同一复权基准。补数通道的复权基准与 baostock 不同，
若直接从库中最后一行往后接，会让**同一条价格序列出现基准断层**。
「没有数据的股票」不存在这个风险，因为整条序列都来自补数通道，内部自洽。

## 复权口径

两条通道都用**前复权（qfq）**价格写入。

前复权与库中既有的后复权看起来不同，但**在同一段日期窗口内，两者只差一个常数因子**
（都是不复权序列 × 分段常数的复权因子，且分段点都在除权日）。因此：

- 日涨跌幅、均线金叉/死叉、突破前高、涨跌停幅度判定 —— 全部是**比值**，完全等价；
- 全部 7 个策略里唯一的绝对值比较是 `turtle` 的成交额门槛（见下），与价格基准无关。

所以前复权序列喂给策略，结果与后复权一致。用前复权而不是不复权，是为了避免
除权日的价格跳空把收益率算错（这正是 akshare 增量通道也用 qfq 的原因）。

## 成交量与成交额单位

- 量：接口给「手」，库里存「股」→ ×100。**科创板（688/689）例外**：接口直接给「股」，
  不乘。实测依据见 `_KC_PREFIXES` 上方的注释（用 `qt` 块反推隐含均价验证过）。
- 额：库里存**元**

## 数据源

| 源 | 接口 | 成交额 | 覆盖 |
|---|---|---|---|
| `em`（优先） | 东财 `push2his.../stock/kline/get` | 接口直接给，**精确** | 沪深两市；北交所无数据 |
| `qq`（兜底） | 腾讯 `proxy.finance.qq.com/ifzqgtimg`（多入口轮询） | **估算**（见下） | 沪深两市；北交所无数据 |

东财 K 线路径在部分网络环境会被整体拦掉（实测某沙箱里对该路径恒返回连接失败），
所以 `qq` 是必备兜底。腾讯侧的入口本身也不稳定，见 `_QQ_BASES` 的说明。

只有 `em` 通道的成交额是接口原值。`qq` 通道不提供成交额字段，改用

    turnover ≈ volume(股) × (high + low + close) / 3     # 用**不复权**价

用 12 只票、7993 个交易日与库中真实成交额对拍：中位数 0.9999，
**97.9% 的交易日误差在 ±1% 内，100% 在 ±3% 内**。
（`close` 单价的误差是 77.3% / 98.2%，`(h+l)/2` 是 95.0% / 99.8%，
所以取 `(h+l+c)/3` 这个组合。）

这个精度只影响 `turtle` 的「成交额 > 1 亿」门槛在边界附近的极少数票，可接受。

## 续跑语义

以 `stock_daily` 里的 symbol 全集判断，中断后重跑自动跳过已完成的股票。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, timedelta

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine

logger = get_logger(__name__)

# ── 东财 ──
_EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# 东财 kline 每行字段顺序（由请求里的 fields2 决定）：
#   f51=日期 f52=开盘 f53=收盘 f54=最高 f55=最低 f56=成交量(手) f57=成交额(元)
# 注意 f52 之后是 **收盘** 而不是最高 —— 与多数行情接口的直觉顺序相反。
_EM_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57"

# ── 腾讯 ──
#
# 腾讯同一份数据挂在多个入口下，可用性还不一样（实测同一时刻）：
#
#   proxy.finance.qq.com/ifzqgtimg  → fqkline/get ✅  kline/kline ✅
#   web.ifzq.gtimg.cn               → fqkline/get ❌（返回 HTML）kline/kline ✅
#   web.ifzq.gtimg.cn/ifzqgtimg     → 两路径都返回 code=11
#   proxy.finance.qq.com            → 两路径都返回 data=null
#
# 而且同一条路径今天通、明天可能就返回 HTML（实测 `web.ifzq.gtimg.cn/appstock/app/fqkline/get`
# 先是可用，补数跑到一半变成 HTML）。所以不写死单个入口，按下面的顺序逐个试，
# 取第一个 code==0 且带日K数组的响应。
_QQ_BASES = (
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://web.ifzq.gtimg.cn",
    "https://web.ifzq.gtimg.cn/ifzqgtimg",
    "https://proxy.finance.qq.com",
)
_QQ_FQ_PATH = "/appstock/app/fqkline/get"
_QQ_RAW_PATH = "/appstock/app/kline/kline"
# 腾讯单次最多返回 640 根日K（传更大的 N 也只给 640），按日期倒推截取。
_QQ_MAX_BARS = 640

# 成交量单位：沪深主板/创业板接口给「手」，库里存「股」
_LOT_TO_SHARE = 100

# 科创板的单位是「股」，不是「手」。
#
# 实测依据：用腾讯响应自带的 `qt` 实时块（`qt[6]`=当日总量、`qt[57]`=当日成交额万元）
# 反推隐含均价 = qt[57]×10000 ÷ day量：
#
#   600000  沪主板   day量 947,958      隐含均价 965.56   收盘 9.54    → 手
#   300750  创业板   day量 541,527      隐含均价 29598.01 收盘 297.75  → 手
#   000001  深主板   day量 1,078,106    隐含均价 1168.45  收盘 11.59   → 手
#   688981  科创板   day量 46,104,484   隐含均价 105.59   收盘 108.42  → 股
#   688111  科创板   day量 5,912,407    隐含均价 223.31   收盘 228.95  → 股
#   688036  科创板   day量 19,022,495   隐含均价 47.83    收盘 48.95   → 股
#
# 三只科创板票的隐含均价都等于收盘价量级，其余板块都要再乘 100 才合理 ——
# 说明科创板按「股」返回。这也说得通：科创板最小买入单位不是 100 股。
_KC_PREFIXES = ("688", "689")

# 复权方式：1 = 前复权（连续，除权日不跳空）
_ADJUST_QFQ = "1"

_DEFAULT_WORKERS = 8
# (连接超时, 读取超时)。读取给得比连接宽，因为 K 线响应有时几十 KB。
_DEFAULT_TIMEOUT = (8, 15)
_MAX_RETRIES = 3

# 北交所：两个接口都没有日K数据（腾讯返回空 day，东财返回 code 9201），跳过。
_BJ_PREFIXES = ("4", "8")


# ────────────────────────────── 代码 → 各源标识 ──────────────────────────────


def em_secid_candidates(symbol: str) -> list[str]:
    """纯数字代码 → 东财 secid 候选（按可能性排序，前面的先试）。

    东财用 `<市场>.` 前缀区分交易所：

    - `1.` 沪市：主板 60x/601/603/605、科创板 688/689
    - `0.` 深市：主板 000/001/002/003、创业板 300/301
    - 北交所（4x/8x）：各接口口径不一，且常返回空，这里给两个候选让调用方逐个试，
      试不到就跳过 —— 不猜。

    Args:
        symbol: 6 位纯数字代码，如 `300750`。

    Returns:
        secid 候选列表，如 `["0.300750"]`；北交所返回两个候选。
    """
    if symbol.startswith(("6", "9")):
        return [f"1.{symbol}"]
    if symbol.startswith(_BJ_PREFIXES):
        return [f"0.{symbol}", f"1.{symbol}"]
    return [f"0.{symbol}"]


def qq_code(symbol: str) -> str:
    """纯数字代码 → 腾讯代码：6/9 → `sh`，4/8 → `bj`，其余 → `sz`。"""
    if symbol.startswith(("6", "9")):
        return f"sh{symbol}"
    if symbol.startswith(_BJ_PREFIXES):
        return f"bj{symbol}"
    return f"sz{symbol}"


def qq_share_scale(symbol: str) -> int:
    """腾讯 `day` 里的成交量换算成「股」要乘的系数。

    科创板（688/689）本来就是「股」→ 返回 1；其余板块是「手」→ 返回 100。
    依据见 `_KC_PREFIXES` 上方注释里的实测数据。
    """
    return 1 if symbol.startswith(_KC_PREFIXES) else _LOT_TO_SHARE


# ────────────────────────────── 解析 ──────────────────────────────


def parse_em_klines(payload: dict, symbol: str) -> list[list]:
    """把东财 kline 响应解析成 `stock_daily` 的行格式。

    单行形如 `2024-01-02,295.77,286.07,296.33,285.69,214033,3390080350.00`，
    按 `_EM_FIELDS2` 的顺序对应 日期/开/收/高/低/量(手)/额(元)。

    解析原则是**宁可少写也不写错**：字段个数不对、数值转换失败、成交量为 0
    的行直接丢弃，绝不把脏数据塞进库。

    Returns:
        `[[symbol, date, open, high, low, close, volume(股), turnover(元)], ...]`。
    """
    klines = ((payload or {}).get("data") or {}).get("klines") or []

    rows: list[list] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        d, o, c, h, low, vol, amount = parts[:7]
        try:
            open_, close_, high_, low_ = float(o), float(c), float(h), float(low)
            volume = float(vol) * _LOT_TO_SHARE
            turnover = float(amount)
        except (TypeError, ValueError):
            continue
        if volume <= 0 or close_ <= 0:
            continue
        rows.append([symbol, d, open_, high_, low_, close_, volume, turnover])
    return rows


def parse_qq_klines(
    adj_payload: dict, raw_payload: dict, symbol: str
) -> list[list]:
    """把腾讯「前复权 + 不复权」两份响应合并成 `stock_daily` 的行格式。

    腾讯每行是 `[日期, 开, 收, 高, 低, 量(手)]`（注意第 3 个是**收盘**），
    **没有成交额字段**。所以用不复权那一份的 `(high + low + close) / 3`
    作为当日均价，乘成交量估算成交额（误差实测 97.9% 在 ±1% 内，见模块 docstring）。

    复权那一份的键名不固定：多数票是 `qfqday`，少数（如科创板 688981）接口
    只给 `day`。两种键都认；价格取复权那一份，均价取不复权那一份。

    Returns:
        `[[symbol, date, open, high, low, close, volume(股), turnover(元)], ...]`，
        按日期升序。缺任一数据源时返回空列表。
    """
    code = qq_code(symbol)
    adj_node = ((adj_payload or {}).get("data") or {}).get(code) or {}
    raw_node = ((raw_payload or {}).get("data") or {}).get(code) or {}

    adj = adj_node.get("qfqday") or adj_node.get("day") or []
    raw = raw_node.get("day") or []
    if not adj or not raw:
        return []

    # 不复权按日期建索引，用于估算成交额
    raw_by_date: dict[str, tuple[float, float, float]] = {}
    for row in raw:
        if len(row) < 6:
            continue
        try:
            # [日期, 开, 收, 高, 低, 量] → 取 (高, 低, 收)
            raw_by_date[row[0]] = (float(row[3]), float(row[4]), float(row[2]))
        except (TypeError, ValueError):
            continue

    rows: list[list] = []
    scale = qq_share_scale(symbol)
    for row in adj:
        if len(row) < 6:
            continue
        try:
            d = row[0]
            open_, close_, high_, low_ = (float(row[1]), float(row[2]),
                                          float(row[3]), float(row[4]))
            volume = float(row[5]) * scale
        except (TypeError, ValueError):
            continue
        if volume <= 0 or close_ <= 0:
            continue

        px = raw_by_date.get(d)
        if px is None:
            continue
        high_r, low_r, close_r = px
        if not (high_r and low_r and close_r):
            continue
        turnover = volume * (high_r + low_r + close_r) / 3

        rows.append([symbol, d, open_, high_, low_, close_, volume, turnover])

    rows.sort(key=lambda r: r[1])
    return rows


# ────────────────────────────── 抓取 ──────────────────────────────


# 每线程一个 Session：复用 TCP/TLS 连接。补数是几千次请求，
# 不做连接复用的话每次都要重新握手，慢好几倍。
_thread_local = threading.local()


def _session():
    """取当前线程的 requests.Session（没有就建一个）。"""
    import requests

    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        # 补数要发几千次请求，失败就重试只会放大耗时；由上层按股票粒度重试
        sess.headers.update({"User-Agent": "Mozilla/5.0"})
        _thread_local.session = sess
    return sess


def _http_json(url: str, params: dict, session, timeout):
    """GET 一个 JSON；失败返回 None（由调用方决定重试或放弃）。"""
    sess = session or _session()
    try:
        resp = sess.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:  # noqa: BLE001 - 单次请求失败由上层重试
        return None


def fetch_em_kline(
    symbol: str, start: str, end: str, *, session=None, timeout=_DEFAULT_TIMEOUT
) -> list[list]:
    """拉单只股票的东财前复权日K（含**精确**成交额）。

    任何异常都吞掉返回 `[]` —— 补数是几千只的长任务，单只票的网络抖动
    不该中断整轮。
    """
    for secid in em_secid_candidates(symbol):
        payload = _http_json(
            _EM_KLINE_URL,
            {
                "secid": secid,
                "fields1": "f1,f2,f3",
                "fields2": _EM_FIELDS2,
                "klt": "101",            # 101 = 日K
                "fqt": _ADJUST_QFQ,
                "beg": start.replace("-", ""),
                "end": end.replace("-", ""),
            },
            session,
            timeout,
        )
        rows = parse_em_klines(payload, symbol) if payload else []
        if rows:
            return rows
    return []


def _qq_request(path: str, params: dict, session, timeout):
    """在 `_QQ_BASES` 里逐个入口试，返回第一个有效响应（`code == 0`）。

    `_http_json` 已经把非 JSON 响应（被拦时返回的 HTML 页面）和网络错误挡掉了，
    这里再校验业务码与数据体，避免「HTTP 通了但内容是错误提示」被当成成功。
    """
    for base in _QQ_BASES:
        payload = _http_json(f"{base}{path}", params, session, timeout)
        if isinstance(payload, dict) and payload.get("code") == 0 and payload.get("data"):
            return payload
    return None


def fetch_qq_kline(
    symbol: str, start: str, end: str, *, session=None, timeout=_DEFAULT_TIMEOUT
) -> list[list]:
    """拉单只股票的腾讯前复权日K（成交额由不复权均价估算）。"""
    code = qq_code(symbol)

    adj = _qq_request(
        _QQ_FQ_PATH,
        {"param": f"{code},day,{start},{end},{_QQ_MAX_BARS},qfq"},
        session, timeout,
    )
    # 不复权：用于估算成交额（复权序列的绝对价位被缩放过，不能直接当均价）
    raw = _qq_request(
        _QQ_RAW_PATH,
        {"param": f"{code},day,{start},{end},{_QQ_MAX_BARS}"},
        session, timeout,
    )
    if not adj or not raw:
        return []
    return parse_qq_klines(adj, raw, symbol)


# 源名 → 抓取函数名（按名字做运行期查找，便于测试替换）
_SOURCES = {"em": "fetch_em_kline", "qq": "fetch_qq_kline"}
_DEFAULT_SOURCE_ORDER = ("em", "qq")


def fetch_kline(
    symbol: str, start: str, end: str, *, source: str = "auto", **kw
) -> tuple[list[list], str]:
    """按 `source` 策略抓一只票，返回 `(行, 实际生效的源名)`。

    Args:
        source: `"auto"` 依次尝试 `em` → `qq`；指定 `"em"` / `"qq"` 则只用该源。

    Returns:
        `(rows, used_source)`；全部失败时 rows 为空、used_source 为空串。
    """
    order = _DEFAULT_SOURCE_ORDER if source == "auto" else (source,)
    for name in order:
        # 按名字从模块全局取函数（而不是在导入期绑定引用），
        # 这样测试替换 fetch_em_kline / fetch_qq_kline 才会真正生效。
        func = globals()[_SOURCES[name]]
        rows = func(symbol, start, end, **kw)
        if rows:
            return rows, name
    return [], ""


def load_symbols(engine: DataEngine, *, min_expected: int = 4000,
                 attempts: int = 3) -> list[str]:
    """取全市场 A 股代码清单，带完整性与兜底校验。

    清单只有一个来源（akshare 的 `stock_info_a_code_name()`），但**一次网络请求
    拿到的东西不能无条件相信** —— 接口偶发返回被截断的结果时，调用方无从分辨，
    补数会静默变成「无事可做」。这里做三层保护：

    1. 重试 `attempts` 次，取最长的那次结果（截断是概率性的，多试一次多数能拿全）；
    2. 长度达到 `min_expected` 才认可；
    3. 仍不达标就退到本地的 `stock_name` 表 —— 那张表是历史上一次性拉全的快照，
       不依赖网络。代价是它含已退市代码（实测 5561 条 vs 在市的 5224 只），
       这些代码取不到行情，会被计成「无数据」，不影响入库。

    Args:
        engine: 数据引擎。
        min_expected: 认可一次结果所需的最少股票数，低于此值视为截断。
        attempts: 重试次数。

    Returns:
        股票代码列表（顺序不定）；三层都拿不到时返回空列表。
    """
    best: list[str] = []
    for _ in range(attempts):
        try:
            syms = engine.get_all_symbols()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"获取全市场清单异常：{exc}")
            continue
        if len(syms) > len(best):
            best = syms
        if len(best) >= min_expected:
            return best

    local = list(engine.get_stock_names())
    if len(local) > len(best):
        logger.warning(
            f"在线清单疑似不完整（最长只有 {len(best)} 只，预期 ≥{min_expected}），"
            f"改用本地 stock_name 表（{len(local)} 条，含已退市代码）"
        )
        return local

    return best


# ────────────────────────────── 补数器 ──────────────────────────────


class MarketBackfiller:
    """把库里**完全没有数据**的股票补齐，可中断续跑。

    Attributes:
        engine: 数据引擎（复用它的库连接与 `_upsert_daily` 幂等写入）。
        start_date: 补齐的起始日期。
        end_date: 目标截止日期。
        bars: 每只票最多保留的 K 线根数（取最近的 N 根），0 表示不截断。
        source: `"auto"` / `"em"` / `"qq"`。
    """

    def __init__(
        self,
        engine: DataEngine,
        settings: Settings,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        bars: int = 0,
        source: str = "auto",
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.start_date = start_date or settings.start_date
        self.end_date = end_date or date.today().strftime("%Y-%m-%d")
        self.bars = bars
        self.source = source

    # ── 待补清单 ──

    def pending(self, symbols: list[str] | None = None) -> list[str]:
        """返回 `stock_daily` 里**一行都没有**的代码（保持入参顺序）。

        只补完全空缺的股票，已有数据的交给日常增量通道 —— 见模块 docstring
        「只补库里完全没有的股票」。
        """
        symbols = symbols or load_symbols(self.engine)
        if not symbols:
            logger.warning("未获取到股票列表，无法确定待补清单")
            return []

        with sqlite3.connect(self.engine.db_path) as conn:
            rows = conn.execute("SELECT DISTINCT symbol FROM stock_daily").fetchall()
        existing = {r[0] for r in rows}

        return [s for s in symbols if s not in existing]

    # ── 主流程 ──

    def run(self, symbols: list[str] | None = None) -> dict[str, int]:
        """执行补数，返回统计 `{"ok", "empty", "failed", "rows", "em", "qq"}`。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        todo = self.pending(symbols)
        if not todo:
            logger.info("所有股票均已有数据，无需补数")
            return {"ok": 0, "empty": 0, "failed": 0, "rows": 0, "em": 0, "qq": 0}

        # auto 模式先探一次路，把通道定下来再开跑。
        # 不能每只票都先试东财：东财不可达时每次要白等 2×连接超时，
        # 几千只票下来能把整轮从几分钟拖到几小时。
        if self.source == "auto":
            self.source = self._resolve_auto_source()

        logger.info(
            f"待补 {len(todo)} 只（{self.start_date} ~ {self.end_date}"
            f"{f'，每只保留最近 {self.bars} 根' if self.bars else ''}），"
            f"并发 {_DEFAULT_WORKERS}，数据源 {self.source} ..."
        )

        stats = {"ok": 0, "empty": 0, "failed": 0, "rows": 0, "em": 0, "qq": 0}
        done = 0

        # 用 as_completed 而不是 pool.map：map 是**按提交顺序**产出结果的，
        # 只要有一只票的请求卡住，后面已经跑完的结果也全都出不来 ——
        # 实测这样会把整轮卡死（进程还在，但十分钟没有任何写入）。
        # as_completed 谁先完成先处理，单只慢票最多拖慢它自己。
        with ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS) as pool:
            futures = {pool.submit(self._fetch, s): s for s in todo}
            for fut in as_completed(futures):
                symbol = futures[fut]
                try:
                    rows, used = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[{symbol}] 补数任务异常：{exc}")
                    rows, used = [], ""

                done += 1
                if rows:
                    if self.bars:
                        rows = rows[-self.bars :]
                    stats["ok"] += 1
                    stats[used] = stats.get(used, 0) + 1
                    stats["rows"] += self._write(rows)
                else:
                    # 分不清「真无数据」和「网络失败」，统一计入 empty，
                    # 重跑时会自然重试（pending 仍会包含它）
                    stats["empty"] += 1

                if done % 500 == 0:
                    logger.info(
                        f"进度 {done}/{len(todo)}，已写入 {stats['rows']} 行"
                        f"（em {stats['em']} / qq {stats['qq']}）"
                    )

        logger.info(
            f"补数完成 — 成功 {stats['ok']} 只（em {stats['em']} / qq {stats['qq']}）"
            f" / 无数据 {stats['empty']} 只，共写入 {stats['rows']} 行"
        )
        return stats

    # ── 内部 ──

    def _resolve_auto_source(self) -> str:
        """auto 模式选源：拿一只样本票试东财，能出数据就用东财，否则全程腾讯。

        东财的成交额是接口原值（精确），腾讯是估算，所以优先东财；
        但东财 K 线路径在部分网络环境会被整体拦掉，那时必须及早识别，
        否则每只票都要白等一次连接超时。
        """
        probe_start = (date.fromisoformat(self.end_date)
                       - timedelta(days=10)).strftime("%Y-%m-%d")
        try:
            if fetch_em_kline("600000", probe_start, self.end_date):
                logger.info("数据源探测：东财可用，使用东财（成交额为接口原值）")
                return "em"
        except Exception as exc:  # noqa: BLE001 - 探测失败就走兜底
            logger.info(f"数据源探测：东财不可用（{type(exc).__name__}: {exc}）")
        logger.info("数据源探测：东财不可用，全程改用腾讯（成交额由不复权均价估算）")
        return "qq"

    def _write(self, rows: list[list]) -> int:
        df = pd.DataFrame(
            rows,
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
        )
        return self.engine._upsert_daily(df)

    def _fetch(self, symbol: str) -> tuple[list[list], str]:
        """带重试地抓一只票（供测试注入）。"""
        for attempt in range(_MAX_RETRIES):
            try:
                rows, used = fetch_kline(
                    symbol, self.start_date, self.end_date, source=self.source
                )
                if rows or attempt == _MAX_RETRIES - 1:
                    return rows, used
            except Exception as exc:  # noqa: BLE001
                if attempt == _MAX_RETRIES - 1:
                    logger.warning(f"[{symbol}] 补数失败（已重试 {_MAX_RETRIES} 次）：{exc}")
                    return [], ""
            time.sleep(2 ** (attempt + 1))
        return [], ""
