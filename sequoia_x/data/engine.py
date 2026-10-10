"""数据引擎模块：负责 SQLite 行情数据存储与 akshare 增量同步。

行情与股票清单都只有一个数据源：**akshare（东方财富源）**。
早期还有一条 baostock 通道，因为它的免费服务长期不稳定（同一天可能通、
也可能不通，`query_history_k_data_plus` 还会卡死不返回），已整体移除 ——
少一条通道就少一层「两条通道复权基准不同」的对齐负担。
"""

import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""

_CREATE_NAME_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_name (
    symbol TEXT PRIMARY KEY,
    name   TEXT NOT NULL
);
"""

# 个股所属行业缓存（东财 F10 公司概况的 EM2016 分类）。
# updated_at 为写入日期，用于 TTL 过期判断。
_CREATE_BOARD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_board (
    symbol     TEXT PRIMARY KEY,
    board      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# 早期版本拿东财 F10「核心题材」当板块，口径不可靠：
# 那套数据会把「一带一路」「央国企改革」「沪股通」这类泛主题排在最前，
# 实测招商南油被标成「一带一路」，而它的真实行业是「港口航运」。
# 换成 EM2016 行业分类后，旧的 stock_concept 表整张作废，这里顺手清掉，
# 免得库里的错误数据以后被误读（幂等，只在下一次初始化时真正执行）。
_DROP_LEGACY_CONCEPT_TABLE_SQL = "DROP TABLE IF EXISTS stock_concept;"

# 行业缓存有效期（天）。行业分类几乎不变，缓存久一点，
# 避免每轮选股都去反查已经不常变的信息。
_BOARD_TTL_DAYS = 30

# 并发拉取时的线程数（akshare 是 HTTP 任务，用线程比进程省）
_AK_CONCURRENCY = 16

# 单次 HTTP 请求的超时（秒）。akshare 的接口内部不暴露 timeout，
# 而东财在部分网络下会「连上但不给响应」—— 一个这样的请求就足以把整轮拖住。
_AK_HTTP_TIMEOUT = 20

# 进度日志间隔：每完成这么多只打一行，避免长跑时日志看起来像卡死
_AK_PROGRESS_EVERY = 500

# 一次加载给策略用的 K 线根数（每个代码只保留最近 N 根）。
#
# 这个值是**硬下限的推导结果**，不是随手取的：最长的回看来自 RPS 策略 ——
#   `shift(120)` 要取到 120 根之前那一天 → 至少 121 根
#   `rolling(120)` 在末行要吃到前 120 根 → 也是 120 根
# 其余策略最多只要 61 根（20/60 日均线）。所以 121 是下限，这里取 130 留一点余量。
#
# 🔴 截断长度**必须 ≥ 任何策略的 `_MIN_BARS`**，否则窗口算不满 → 条件恒不成立
#    → 静默少选票（最难查的那类 bug）。tests/test_strategy.py 里有测试守着这个不变量，
#    加新策略（回看更长）时会直接失败提醒你调大这个值。
_PANEL_BARS = 130

# 快照里带的列（不含 id）。symbol 不进列 —— 它是 dict 的键，重复存一遍要多占内存。
_PANEL_COLUMNS = "date,open,high,low,close,volume,turnover"

_REQUESTS_TIMEOUT_PATCHED = False


def _ensure_request_timeout() -> None:
    """给进程内的 requests 请求补一个默认超时（幂等）。

    akshare 内部走 `requests` 且不传 timeout，等于**无限等待**。一旦某个请求
    卡在「连接已建立但服务器不响应」的状态，线程池里那个 worker 就再也不会返回。
    因为主流程是用 `as_completed` 收集全部结果后才继续的，表现就是日志停在
    「并发拉取中...」纹丝不动，直到 job 超时。

    这里用 `setdefault` 打补丁：显式传了 timeout 的调用（例如东财 F10 接口传的 15）
    不受影响，只有没传的那些才被兜上默认值。
    """
    global _REQUESTS_TIMEOUT_PATCHED
    if _REQUESTS_TIMEOUT_PATCHED:
        return
    try:
        import requests

        _orig_request = requests.Session.request

        def _request_with_timeout(self, method, url, **kwargs):  # noqa: ANN001
            kwargs.setdefault("timeout", _AK_HTTP_TIMEOUT)
            return _orig_request(self, method, url, **kwargs)

        requests.Session.request = _request_with_timeout
        _REQUESTS_TIMEOUT_PATCHED = True
        logger.debug(f"已为 requests 补上默认超时 {_AK_HTTP_TIMEOUT}s")
    except Exception as exc:  # noqa: BLE001 - 打不上补丁也不能影响主流程
        logger.warning(f"为 requests 补默认超时失败（不影响主流程）：{exc}")

# 东财 F10 公司概况接口（含 EM2016 行业分类）与批量大小。
# 接口支持 `SECUCODE in (...)`，90 只股票只要 2 次请求，
# 比早期「一只一发」省下大量往返。
_EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_ORGINFO_REPORT = "RPT_F10_BASIC_ORGINFO"
_EM_BATCH_SIZE = 45


def _ak_fetch_one(task: tuple[str, str, str]) -> list[list]:
    """线程 worker：用 akshare 拉单只股票的前复权日线。

    用前复权（qfq）而不是不复权：前复权序列的日涨跌已经过除权调整，
    换算到库中的后复权序列后，除权日也不会算错收益率。

    Args:
        task: (symbol, start_date, end_date)，日期格式 YYYY-MM-DD。

    Returns:
        [[date, open, close, high, low, volume(手), turnover(元)], ...]；
        任何异常或空结果都返回 []，由调用方计入失败数。
    """
    symbol, start, end = task
    try:
        import akshare as ak

        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust="qfq",
        )
    except Exception:  # noqa: BLE001 - 单只失败不影响整体
        return []

    if df is None or len(df) == 0:
        return []

    # akshare 的列名是中文，缺失列直接放弃该只，避免位置索引错位
    need = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
    if not all(c in df.columns for c in need):
        return []

    rows: list[list] = []
    for rec in df.to_dict("records"):
        try:
            rows.append([
                str(rec["日期"])[:10],
                float(rec["开盘"]),
                float(rec["收盘"]),
                float(rec["最高"]),
                float(rec["最低"]),
                float(rec["成交量"]),
                float(rec["成交额"]),
            ])
        except (TypeError, ValueError):
            continue
    return rows


def _to_em_secucode(symbol: str) -> str:
    """纯数字代码 → 东财 SECUCODE：6/9 → .SH，4/8 → .BJ，其余 → .SZ。"""
    if symbol.startswith(("6", "9")):
        return f"{symbol}.SH"
    if symbol.startswith(("4", "8")):
        return f"{symbol}.BJ"
    return f"{symbol}.SZ"


def _industry_from_em2016(em2016: str) -> str:
    """从东财 EM2016 三级行业分类里取**第二级**作为行业板块名。

    东财官方的行业分类是三级的，形如 `交通运输-港口航运-航运`
    （门类-行业-细分）。第二级正好是行情软件里那个「行业板块」粒度：
    既不会像门类（`交通运输`）那样把港口和高速混在一起，
    也不会像细分（`航运`）那样碎到只剩一两只票。

    实测招商南油 = `交通运输-港口航运-航运` → `港口航运`。

    Returns:
        行业名；分级数不足（极少数冷门票只有一级）时返回空串，
        由调用方按「无板块」处理。
    """
    parts = [p.strip() for p in (em2016 or "").split("-") if p.strip()]
    return parts[1] if len(parts) >= 2 else ""


def _em_fetch_boards(symbols: list[str]) -> dict[str, str]:
    """批量反查多只股票的所属行业，返回 {代码: 行业名}。

    数据来自东财 F10 公司概况接口的 `EM2016` 字段（见 `_industry_from_em2016`）。

    为什么不再用「核心题材」：实测那套口径对绝大多数票都给不出行业 ——
    招商南油的「核心题材」是 `一带一路` / `央国企改革`，正确行业 `港口航运`
    的 IS_PRECISE 反而是 0。全量比对 90 只票，EM2016 行业与旧口径**无一只相同**，
    且分组更聚合（42 个板块 / 22 个单只 vs 旧口径 49 个板块 / 31 个单只）。

    北交所（4/8 开头）在东财 F10 里没有数据，直接跳过，省下无谓请求。

    Args:
        symbols: 股票代码列表，允许重复。

    Returns:
        {代码: 行业名}；查不到或该批请求失败的股票不会出现在结果里
        （调用方据此判断「无板块」，且失败不写缓存、下轮自然重试）。
    """
    codes = [s for s in dict.fromkeys(symbols) if not s.startswith(("4", "8"))]
    if not codes:
        return {}

    import requests

    found: dict[str, str] = {}
    for i in range(0, len(codes), _EM_BATCH_SIZE):
        chunk = codes[i : i + _EM_BATCH_SIZE]
        expr = "(" + ",".join(f'"{_to_em_secucode(s)}"' for s in chunk) + ")"
        params = {
            "reportName": _EM_ORGINFO_REPORT,
            "columns": "SECUCODE,EM2016",
            "filter": f"(SECUCODE in {expr})",
            "pageNumber": 1,
            "pageSize": len(chunk),
            "source": "HSF10",
            "client": "PC",
        }
        try:
            resp = requests.get(_EM_URL, params=params, timeout=15)
            resp.raise_for_status()
            rows = (resp.json().get("result") or {}).get("data") or []
        except Exception as exc:  # noqa: BLE001 - 单批失败不影响其它批
            logger.warning(f"行业批量反查失败（本批 {len(chunk)} 只）：{exc}")
            continue

        for row in rows:
            symbol = str(row.get("SECUCODE") or "")[:6]
            board = _industry_from_em2016(str(row.get("EM2016") or ""))
            if symbol and board:
                found[symbol] = board

    return found


def _ak_fetch_symbol_names() -> list[tuple[str, str]]:
    """拉全市场 A 股代码与名称，返回 [(代码, 名称), ...]。

    用 akshare 的 `stock_info_a_code_name()`（一次请求拿全市场），
    而不是逐只查询 —— 逐只在请求量上来后会开始返回空结果（CI 环境尤其明显），
    导致大量股票只能显示代码。

    列名做**防御性识别**：优先按 `code`/`name` 取，取不到就退化为
    「按列位置取前两列」。上游改列名时返回空列表而不是抛异常，
    由调用方沿用已有数据。

    Returns:
        [(代码, 名称), ...]；失败或结果为空时返回 []。
    """
    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
    except Exception as exc:  # noqa: BLE001 - 取不到就沿用已有数据
        logger.warning(f"获取股票清单失败：{type(exc).__name__}: {exc}")
        return []

    if df is None or len(df) == 0:
        logger.warning("股票清单为空，沿用已有数据")
        return []

    # 列名可能是 code/name（小写）、证券代码/证券简称等，按小写名匹配
    lower = {str(c).strip().lower(): c for c in df.columns}
    code_col = next(
        (lower[k] for k in ("code", "symbol", "证券代码", "股票代码") if k in lower), None
    )
    name_col = next(
        (lower[k] for k in ("name", "证券简称", "股票简称", "名称") if k in lower), None
    )
    if code_col is None or name_col is None:
        if len(df.columns) < 2:
            logger.warning(f"股票清单列名无法识别（现有列：{list(df.columns)}）")
            return []
        code_col, name_col = df.columns[0], df.columns[1]

    out: list[tuple[str, str]] = []
    for code, name in zip(df[code_col].astype(str), df[name_col].astype(str)):
        # 上游可能带 sh/sz/bj 前缀，统一剥成 6 位纯数字
        digits = "".join(ch for ch in code if ch.isdigit())
        symbol = digits[-6:] if len(digits) >= 6 else ""
        clean = name.strip()
        if symbol and clean and clean.lower() != "nan":
            out.append((symbol, clean))
    return out


class DataEngine:
    """行情数据引擎：SQLite 存储 + akshare 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        # 全市场 K 线快照缓存（惰性构建）。见 market_panel()。
        self._panel: dict[str, pd.DataFrame] | None = None
        self._panel_bars: int = _PANEL_BARS
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_NAME_TABLE_SQL)
            conn.execute(_CREATE_BOARD_TABLE_SQL)
            conn.execute(_DROP_LEGACY_CONCEPT_TABLE_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def get_latest_date(self) -> str | None:
        """返回 stock_daily 中最新的一天，即当前数据的截止日期。"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        """读取单只股票的**全部**历史 K 线（含 id 列，按日期升序）。

        想批量算策略请用 `market_panel()` —— 它一次加载、多策略共享；
        这个方法是给「取单只票看细节」用的，逐只调用会退化成 5000+ 次查询。
        """
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    def market_panel(self, limit_bars: int = _PANEL_BARS) -> dict[str, pd.DataFrame]:
        """一次性把全市场 K 线读进内存，返回 {代码: K 线}（进程内缓存）。

        **为什么需要这个方法**：6 个 K 线策略原本各自「取全市场代码 → 逐只
        `get_ohlcv`」，等于每轮把 5000+ 只票从 SQLite 捞 6 遍，光取数就 100 秒往上。
        改成一次加载、多策略共享后，取数只付一次。

        两个实现要点：

        - **复用同一个 sqlite 连接**。逐只查询时每次 `connect()` 的固定开销
          比查询本身还大（实测 3.4ms/只 vs 0.9ms/只）。
        - **只取最近 `limit_bars` 根**（`ORDER BY date DESC LIMIT ?` 再反转）。
          策略的回看窗口最长 120 根，全历史既慢又占内存。

        只包含**全库最新交易日**当天有行情的代码（与 `get_local_symbols()` 同口径）：
        停牌、退市或某轮同步漏掉的票最后一行早于大盘，不应参与当轮选股。

        Args:
            limit_bars: 每个代码保留的最近 K 线根数。

        Returns:
            {代码: DataFrame}，DataFrame 按日期升序、索引重置，
            列为 `date/open/high/low/close/volume/turnover`。空库返回 {}。
        """
        if self._panel is not None and self._panel_bars == limit_bars:
            return self._panel

        started = time.perf_counter()
        panel: dict[str, pd.DataFrame] = {}
        conn = sqlite3.connect(self.db_path)
        try:
            symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT symbol FROM stock_daily "
                    "WHERE date = (SELECT MAX(date) FROM stock_daily) ORDER BY symbol"
                )
            ]
            sql = (
                f"SELECT {_PANEL_COLUMNS} FROM stock_daily "
                "WHERE symbol = ? ORDER BY date DESC LIMIT ?"
            )
            for symbol in symbols:
                df = pd.read_sql(sql, conn, params=(symbol, limit_bars))
                if len(df):
                    # 取到的是「最近 N 根」的倒序，反转回来才是按日期升序
                    panel[symbol] = df.iloc[::-1].reset_index(drop=True)
        finally:
            conn.close()

        self._panel = panel
        self._panel_bars = limit_bars
        logger.info(
            f"已加载全市场 K 线快照：{len(panel)} 只 / "
            f"{sum(len(v) for v in panel.values())} 行 / 每只最近 {limit_bars} 根"
            f"（{time.perf_counter() - started:.1f}s）"
        )
        return panel

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """增量同步行情数据并写入 SQLite，返回写入行数。

        数据源只有一个：akshare（东方财富源）。方法名保留「bulk」是为了
        与调用方（`main.py`）保持稳定，实际逻辑全在 `sync_via_akshare()` 里 ——
        它负责把东财的复权基准换算到库中的后复权基准。
        """
        return self.sync_via_akshare()

    # ── akshare 增量同步 ──

    def _load_anchor(self) -> dict[str, tuple[str, float]]:
        """取每只股票在库中的最后交易日与该日的后复权收盘价。

        这是跨源换算的锚点：不同数据源复权基准不同，
        必须用库中已有的后复权价 + 新数据源的相对涨跌来推导，不能直接写入。

        Returns:
            {symbol: (最后交易日, 该日后复权收盘价)}
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT d.symbol, d.date, d.close
                FROM stock_daily d
                JOIN (
                    SELECT symbol, MAX(date) AS md
                    FROM stock_daily GROUP BY symbol
                ) m ON d.symbol = m.symbol AND d.date = m.md
                """
            ).fetchall()
        return {s: (dt, float(c)) for s, dt, c in rows if c}

    def sync_via_akshare(self, today: str | None = None) -> int:
        """通过 akshare（东财）拉增量行情，换算到库中的后复权基准后写入。

        这是**唯一**的行情更新通道。注意库里已有的历史序列是早期用 baostock
        拉的后复权数据，而东财的复权基准与它不同，所以新数据不能直接写入。

        为什么不把 akshare 的行情直接写进库：**不同数据源的复权基准不同**。
        实测同一只票同一日（sh600000 / 2026-10-09）：

        - baostock 后复权收盘 = 127.52（库里历史序列的基准）
        - 东财后复权收盘       = 102.23

        而且两者的日收益率也不一致。直接拼接会让价格序列出现断层，
        均线、RPS 排名等全部失真。

        这里的做法是只借用 akshare 前复权序列的**相对涨跌**，用库中已有的
        后复权收盘做锚点换算：

            k = 库中最后一日后复权收盘 ÷ 该日的前复权收盘
            今日后复权价 = 今日前复权价 × k

        用前复权而不是不复权，是为了在除权日也能得到正确的复权收益率。
        （实测无除权窗口下，东财不复权序列与 baostock 后复权完全同比，
          单日内比例恒定 ≈13.367，说明该换算精确成立。）

        Args:
            today: 目标截止日期（YYYY-MM-DD），默认取本机今天。

        Returns:
            实际写入的行数；失败为 0。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import date, datetime

        if today is None:
            today = date.today().strftime("%Y-%m-%d")

        # A 股周末不开盘。周末手动触发时，全市场 5000+ 只会一只都拿不到新数据，
        # 白拉十几分钟。定时任务 cron 是 1-5、本来落不到周末，这里防的是手动运行。
        # （节假日无法用日历简单判断，那种情况靠拉取结果自然收敛，不会出错。）
        try:
            if datetime.strptime(today, "%Y-%m-%d").weekday() >= 5:
                logger.info(f"akshare：{today} 是周末，A 股不开盘，跳过增量更新")
                return 0
        except ValueError:
            pass  # today 格式异常就不拦，交给下游逻辑处理

        anchors = self._load_anchor()
        if not anchors:
            logger.warning("akshare：本地无任何行情数据，无法确定换算锚点，跳过")
            return 0

        tasks = [
            (symbol, last_date, today)
            for symbol, (last_date, _) in anchors.items()
            if last_date < today
        ]
        if not tasks:
            logger.info("akshare：所有股票已是最新，无需更新")
            return 0

        logger.info(
            f"akshare：需要更新 {len(tasks)} 只股票，{_AK_CONCURRENCY} 线程并发拉取中"
            f"（每 {_AK_PROGRESS_EVERY} 只报告一次进度）..."
        )

        # 先给 requests 补上默认超时，避免个别请求「连上但无响应」把整轮拖死
        _ensure_request_timeout()

        records: list[list] = []
        failed = 0
        try:
            # 用 as_completed 而不是 pool.map：map 按**提交顺序**产出结果，
            # 前面某只慢就会挡住后面已经完成的结果，日志表现为长时间纹丝不动
            # （曾因此在周末白跑二十分钟且看不到任何进展）。
            # 这里按完成顺序收集，并周期性汇报进度。
            fetched: list[list] = [[] for _ in tasks]
            done = 0
            total = len(tasks)
            with ThreadPoolExecutor(max_workers=_AK_CONCURRENCY) as pool:
                futures = {pool.submit(_ak_fetch_one, t): i for i, t in enumerate(tasks)}
                for fut in as_completed(futures):
                    idx = futures[fut]
                    try:
                        fetched[idx] = fut.result() or []
                    except Exception as exc:  # noqa: BLE001 - 单只失败不影响其它
                        logger.debug(f"akshare：{tasks[idx][0]} 拉取异常：{exc}")
                    done += 1
                    if done % _AK_PROGRESS_EVERY == 0 or done == total:
                        logger.info(f"akshare：已拉取 {done}/{total}")
        except Exception as exc:  # noqa: BLE001 - 数据更新失败不应中断主流程
            logger.warning(f"akshare：并发拉取异常，放弃本次更新：{exc}")
            return 0

        for (symbol, last_date, _end), rows in zip(tasks, fetched):
            if not rows:
                failed += 1
                continue
            # 锚点日的前复权收盘必须取自同一次请求，保证与后续行同一复权基准
            base = next((r for r in rows if r[0] == last_date), None)
            if base is None or not base[2]:
                failed += 1
                continue
            k = anchors[symbol][1] / base[2]
            for d, o, c, h, low, volume, turnover in rows:
                if d <= last_date:
                    continue
                records.append(
                    # akshare 成交量单位是「手」，库里存的是「股」，×100 对齐
                    [symbol, d, o * k, h * k, low * k, c * k, volume * 100, turnover]
                )

        if not records:
            logger.warning(
                f"akshare：未取得任何新数据（{failed}/{len(tasks)} 只拉取失败）。"
                f"数据库数据截止 {self.get_latest_date()}，"
                f"本次选股将基于该日期及之前的数据。"
                f"（非交易日、或当日行情尚未生成时属正常现象。）"
            )
            return 0

        df = pd.DataFrame(
            records,
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
        )
        count = self._upsert_daily(df)
        logger.info(f"akshare：写入 {count} 条数据（{failed} 只未取到）")
        return count

    def _upsert_daily(self, df: pd.DataFrame) -> int:
        """把行情 DataFrame 覆盖写入 stock_daily，返回写入行数。

        幂等：只删除本次真正写入的 (symbol, date) 组合。
        不能按 date 整日删除 —— 多进程/多线程抓取时部分 worker 可能掉线，
        单轮结果往往不完整，整日删除会把已写入的其它股票数据一并抹掉，
        导致同一天的数据越补越少。
        """
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]
        if df.empty:
            return 0

        count = len(df)
        with sqlite3.connect(self.db_path) as conn:
            pairs = {
                (row[0], row[1])
                for row in df[["symbol", "date"]].itertuples(index=False, name=None)
            }
            conn.executemany(
                "DELETE FROM stock_daily WHERE symbol = ? AND date = ?", pairs
            )
            df.to_sql(
                "stock_daily", conn, if_exists="append",
                index=False, method="multi", chunksize=500,
            )
            conn.commit()
        # 库里数据变了，之前构建的快照作废，下次 market_panel() 重新加载
        self._panel = None
        return count

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """获取全市场 A 股代码列表（akshare）。

        已剔除北交所（4/8 开头）：东财 F10 对北交所没有数据，本库的行情与
        行业板块也都只覆盖沪深两市，纳进来只会多出无谓的失败请求。

        Returns:
            纯数字代码列表；取不到时返回 []（由调用方决定降级策略）。
        """
        pairs = _ak_fetch_symbol_names()
        if not pairs:
            return []
        symbols = [s for s, _ in pairs if not s.startswith(("4", "8"))]
        logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
        return symbols

    def get_local_symbols(self, latest_only: bool = True) -> list[str]:
        """返回本地有行情数据的股票代码。

        Args:
            latest_only: 只返回**全库最新交易日**当天有行情的股票（默认）。

                停牌、退市或某轮增量同步漏掉的票，其最后一行会早于大盘，
                若拿它自己的「最后一天」当作「今日」，就是在用几天前的旧数据参与
                当轮选股（实测库里有落后 25 天的票）。按最新交易日取行可以
                自然过滤掉这些票 —— 停牌股当天本来就没有 K 线。

                传 False 则返回库里出现过的全部代码（回填/排查用）。
        """
        with sqlite3.connect(self.db_path) as conn:
            if latest_only:
                rows = conn.execute(
                    "SELECT DISTINCT symbol FROM stock_daily "
                    "WHERE date = (SELECT MAX(date) FROM stock_daily)"
                ).fetchall()
            else:
                rows = conn.execute("SELECT DISTINCT symbol FROM stock_daily").fetchall()
        return [row[0] for row in rows]

    # ── 股票名称 ──

    def refresh_stock_names(self) -> int:
        """批量刷新全市场股票名称到本地 stock_name 表，返回写入条数。

        数据源：akshare `stock_info_a_code_name()`，**一次请求拿全市场**。
        为什么不用逐只查询：请求量上来后逐只接口会开始返回空结果
        （CI 环境尤其明显），导致大量股票只能显示代码。
        名称落库还有一层好处：它能跟随数据库缓存持久化 ——
        即使某次刷新失败，也能沿用上一次的名称，不影响推送显示。

        只会写入沪深两市（`_ak_fetch_symbol_names` 已剥掉前缀），
        指数（如上证综指 `000001`）不会进来，避免与平安银行同号串名。

        Returns:
            成功写入的名称条数；失败或未取到数据时返回 0（沿用已有数据）。
        """
        rows = _ak_fetch_symbol_names()
        if not rows:
            logger.warning("未获取到股票名称，沿用已有数据")
            return 0

        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO stock_name (symbol, name) VALUES (?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET name = excluded.name",
                rows,
            )
            conn.commit()

        logger.info(f"股票名称已刷新 {len(rows)} 条")
        return len(rows)

    def get_stock_names(self) -> dict[str, str]:
        """读取本地股票名称表，返回 {代码: 名称}。"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT symbol, name FROM stock_name").fetchall()
        return {symbol: name for symbol, name in rows}

    def name_coverage(self) -> float:
        """stock_name 表对 stock_daily 中股票的覆盖率（0~1）。

        用于决定是否需要刷新名称：名称表随数据库缓存持久化，
        正常跑几轮后就是全的。覆盖率已经很高还去刷新，只是白打一次网络请求并
        阻塞启动流程 —— 名称表够用就直接跳过。
        """
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM stock_daily"
            ).fetchone()[0]
            if not total:
                return 1.0
            named = conn.execute(
                "SELECT COUNT(*) FROM stock_name "
                "WHERE symbol IN (SELECT DISTINCT symbol FROM stock_daily)"
            ).fetchone()[0]
        return named / total

    # ── 个股所属行业（推送里的「板块」）──

    def get_boards(
        self, symbols: list[str], ttl_days: int = _BOARD_TTL_DAYS
    ) -> dict[str, str]:
        """返回 {代码: 所属行业}，只包含查得到行业的股票。

        优先读本地 `stock_board` 表（TTL 内命中即用），表里没有的**一次性批量**反查
        并写回缓存。反查失败**不写库**，下一轮会自然重试。

        北交所（4/8 开头）在东财 F10 里没有数据，直接跳过，省下无谓请求。

        Args:
            symbols: 股票代码列表，允许重复。
            ttl_days: 缓存有效期（天）。

        Returns:
            {代码: 行业名}；查不到的股票不会出现在结果里
            （推送侧按「无板块」处理）。
        """
        unique = list(dict.fromkeys(symbols))
        if not unique:
            return {}

        cutoff = (date.today() - timedelta(days=ttl_days)).strftime("%Y-%m-%d")
        placeholders = ",".join("?" * len(unique))

        cached: dict[str, str] = {}
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT symbol, board FROM stock_board "
                f"WHERE updated_at >= ? AND symbol IN ({placeholders})",
                [cutoff, *unique],
            ).fetchall()
        for symbol, board in rows:
            if board:
                cached[symbol] = board

        missing = [
            s for s in unique
            if s not in cached and not s.startswith(("4", "8"))
        ]
        if not missing:
            return cached

        logger.info(f"反查 {len(missing)} 只股票的所属行业...")
        try:
            fetched = _em_fetch_boards(missing)
        except Exception as exc:  # noqa: BLE001 - 行业只是锦上添花，失败不影响推送
            logger.warning(f"行业反查失败，本次推送不含板块：{exc}")
            return cached

        today = date.today().strftime("%Y-%m-%d")
        if fetched:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT INTO stock_board (symbol, board, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(symbol) DO UPDATE SET "
                    "board = excluded.board, updated_at = excluded.updated_at",
                    [(s, b, today) for s, b in fetched.items()],
                )
                conn.commit()
            cached.update(fetched)

        logger.info(f"行业缓存命中 {len(cached)}/{len(unique)} 只")
        return cached

