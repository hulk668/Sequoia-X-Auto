"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
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


def _bs_fetch_batch(tasks: list) -> list:
    """多进程 worker：独立 login，批量拉取 baostock 数据。"""
    import baostock as bs
    bs.login()
    results = []
    for symbol, bs_code, start, end in tasks:
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="1",  # 后复权
        )
        if rs.error_code != "0":
            continue
        while rs.next():
            results.append([symbol] + rs.get_row_data())
    bs.logout()
    return results


def _probe_baostock() -> tuple[bool, str]:
    """探测 baostock 行情接口是否真的可用（拿一只样本股做最小查询）。

    为什么需要探测：baostock 内部用裸 print() 直接往 stdout 吐错误
    （见 baostock/util/socketutil.py 的"服务器连接失败/接收数据异常"），
    不走 logging，没法用日志级别屏蔽。一旦接口在 CI 环境不可达，
    8 个 worker 会刷出上百行噪音，还白等一两分钟。

    这里先在主进程用一次最小查询探路，不可用就直接跳过同步 ——
    既不刷屏，也省掉拉起进程池的开销。

    Returns:
        (是否可用, 失败原因)。可用时原因为空字符串。
    """
    import contextlib
    import io
    from datetime import date, timedelta

    import baostock as bs

    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")

    buf = io.StringIO()
    reason = ""
    try:
        # 把 baostock 的 stdout 收进来，稍后压成一行诊断信息
        with contextlib.redirect_stdout(buf):
            lg = bs.login()
            if lg.error_code != "0":
                reason = f"登录失败：{lg.error_msg}"
            else:
                try:
                    rs = bs.query_history_k_data_plus(
                        "sh.600000",
                        "date,close",
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="1",
                    )
                    if rs.error_code != "0":
                        reason = f"查询返回错误：{rs.error_msg}"
                    else:
                        rs.next()  # 必须真正取一次，才会触发实际的数据传输
                finally:
                    bs.logout()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"

    if not reason:
        return True, ""

    noise = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
    detail = next((ln for ln in noise if "success" not in ln.lower()), "")
    if detail:
        reason = f"{reason}｜baostock 输出：{detail}"
    return False, reason


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_NAME_TABLE_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_latest_date(self) -> str | None:
        """返回 stock_daily 中最新的一天，即当前数据的截止日期。"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。"""
        from datetime import date, timedelta
        from multiprocessing import Pool

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        logger.info(f"需要更新 {len(tasks)} 只股票，启动多进程并行拉取...")

        # 先探路：接口不可达就直接跳过，不拉起进程池刷屏
        reachable, reason = _probe_baostock()
        if not reachable:
            logger.warning(
                f"baostock 数据接口不可用（{reason}），跳过本次增量同步。"
                f"数据库数据截止 {self.get_latest_date()}，"
                f"本次选股将基于该日期及之前的数据。"
            )
            return 0

        n_workers = min(8, len(tasks))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]

        with Pool(n_workers) as pool:
            batch_results = pool.map(_bs_fetch_batch, chunks)

        all_rows = []
        for batch in batch_results:
            all_rows.extend(batch)

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(all_rows, columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"])
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        with sqlite3.connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500)
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    def backfill(self, symbols: list[str]) -> None:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 已入库的自动 skip，中断后可重跑续传
        """
        import time
        from datetime import date, timedelta

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次

        def _login():
            lg = bs.login()
            if lg.error_code != "0":
                logger.error(f"baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return

        success = 0
        skipped = 0
        failed = 0
        since_reconnect = 0

        try:
            for i, symbol in enumerate(symbols):
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    bs.logout()
                    time.sleep(1)
                    if not _login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=today_str,
                            frequency="d",
                            adjustflag="1",  # 后复权
                        )

                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            try:
                                row_data = rs.get_row_data()
                                # 检查是否有乱码数据（非正常数字/日期格式）
                                if any(not isinstance(v, str) or (v and any(ord(c) > 127 for c in v)) for v in row_data):
                                    # 尝试清理乱码字符
                                    cleaned = []
                                    for v in row_data:
                                        if isinstance(v, str):
                                            # 过滤掉非 ASCII 字符
                                            cleaned.append(''.join(c for c in v if ord(c) <= 127))
                                        else:
                                            cleaned.append(v)
                                    row_data = tuple(cleaned)
                                rows.append(row_data)
                            except UnicodeDecodeError:
                                # 单行解码失败，跳过该行
                                logger.warning(f"[{symbol}] 单行数据解码失败，跳过")
                                continue
                        query_ok = True
                        break

                    except UnicodeDecodeError as exc:
                        # 整体解码失败，触发重试
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败：{exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock
                            bs.logout()
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")
                    except Exception as exc:
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败：{exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock
                            bs.logout()
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if not query_ok:
                    failed += 1
                    continue

                if not rows:
                    skipped += 1
                    continue

                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    continue

                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                try:
                    with sqlite3.connect(self.db_path) as conn:
                        df.to_sql(
                            "stock_daily", conn, if_exists="append",
                            index=False, method="multi", chunksize=500,
                        )
                except sqlite3.IntegrityError:
                    pass

                success += 1

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"已处理 {i + 1}/{len(symbols)}，"
                        f"成功 {success} 跳过 {skipped} 失败 {failed}"
                    )

        finally:
            bs.logout()

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """通过 baostock 获取全市场 A 股代码列表。"""
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return []

        try:
            rs = bs.query_stock_basic(code_name="", code="")
            symbols = []
            while rs.next():
                row = rs.get_row_data()
                code = row[0]           # "sh.600000" or "sz.000001"
                status = row[4]         # "1" = 上市
                stock_type = row[5]     # "1" = 股票
                if status == "1" and stock_type == "1":
                    symbols.append(code.split(".")[1])  # 提取纯数字代码
            logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
            return symbols
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
        finally:
            bs.logout()

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]

    # ── 股票名称 ──

    def refresh_stock_names(self) -> int:
        """批量刷新全市场股票名称到本地 stock_name 表，返回写入条数。

        为什么不逐只查询：baostock 单次会话内逐只 query_stock_basic 在请求量大时
        会开始返回空结果（CI 环境尤其明显），导致大量股票只能显示代码。
        这里改成一次性拉全市场（单次请求），并且落库以便跟随数据库缓存持久化 ——
        即使某次刷新失败，也能沿用上一次的名称，不影响推送显示。

        仅采集 type == "1"（股票），避免把指数写进来：
        `sh.000001` 是上证综合指数，与 `sz.000001` 平安银行数字部分相同，
        不过滤会导致同号串名。

        Returns:
            成功写入的名称条数；失败或未取到数据时返回 0（沿用已有数据）。
        """
        import baostock as bs

        try:
            lg = bs.login()
        except Exception as exc:  # 网络异常
            logger.warning(f"baostock 登录异常，沿用已有股票名称：{exc}")
            return 0

        if lg.error_code != "0":
            logger.warning(f"baostock 登录失败，沿用已有股票名称：{lg.error_msg}")
            return 0

        rows: list[tuple[str, str]] = []
        try:
            rs = bs.query_stock_basic(code_name="", code="")
            while rs.next():
                r = rs.get_row_data()
                # 字段顺序：code, code_name, ipoDate, outDate, type, status
                if len(r) < 6 or r[4] != "1":  # type != 1 的是指数等，跳过
                    continue
                symbol = r[0].split(".")[-1]
                name = (r[1] or "").strip()
                if symbol and name:
                    rows.append((symbol, name))
        except Exception as exc:
            logger.warning(f"获取股票名称异常，沿用已有股票名称：{exc}")
            return 0
        finally:
            bs.logout()

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

