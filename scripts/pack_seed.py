#!/usr/bin/env python
"""把 SQLite 数据库打包成 Actions 冷启动用的种子压缩包。

做三件事，缺一不可：

1. **复制一份再操作**，绝不碰正在用的 `data/sequoia_v2.db`。
   直接对活动库 VACUUM 会长时间独占写锁，本机可能还有别的进程在读。
2. **按 symbol 截断**到最近 N 根 K 线。日常增量同步只追加不裁剪，
   库会一直变胖（5224 只 × 每交易日 1 行 ≈ 一年 100MB），种子必须封顶。
   截断放到 VACUUM 之前，这样空间能真正回收掉。
3. **gzip -9**。SQLite 页里大量重复的 symbol/日期前缀，压缩率能到 4x。

用法：
    python scripts/pack_seed.py                     # 默认 400 根
    python scripts/pack_seed.py --bars 300          # 更小的种子
    python scripts/pack_seed.py --bars 0            # 不截断（只有排查时才用）
    python scripts/pack_seed.py --db other.db --out /tmp/seed.db.gz
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

DEFAULT_DB = "data/sequoia_v2.db"
DEFAULT_OUT = "data/seed/sequoia_v2.db.gz"
DEFAULT_BARS = 400

# 种子小到离谱多半是打包错了（比如库里本来就没数据），直接拦掉，
# 免得把一个空种子发上 Release 把冷启动链路彻底弄坏。
_MIN_SYMBOLS = 1000
_MIN_ROWS = 100_000

# 保留最近 N 根 K 线的裁剪语句。用窗口函数按 symbol 分组、日期倒序排名，
# 排名超过 N 的一次性删掉（SQLite 3.25+ 支持窗口函数）。
_TRIM_SQL = """
DELETE FROM stock_daily
 WHERE rowid IN (
       SELECT rowid FROM (
              SELECT rowid,
                     ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM stock_daily
       )
        WHERE rn > ?
 )
"""


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _stats(conn: sqlite3.Connection) -> tuple[int, int, str, str]:
    rows = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
    symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM stock_daily").fetchone()[0]
    lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM stock_daily").fetchone()
    names = conn.execute("SELECT COUNT(*) FROM stock_name").fetchone()[0]
    return rows, symbols, f"{lo} ~ {hi}", names


def pack(
    db_path: Path,
    out_path: Path,
    bars: int = DEFAULT_BARS,
    *,
    verbose: bool = True,
) -> dict[str, object]:
    """把 db_path 打包成 out_path，返回一份统计信息。"""
    log = print if verbose else (lambda *a, **k: None)

    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 临时目录里做，避免在旁边留下几百 MB 的中间文件
    with tempfile.TemporaryDirectory(prefix="pack_seed_") as tmpdir:
        tmp_db = Path(tmpdir) / "seed.db"
        log(f"复制 {db_path}（{_human(db_path.stat().st_size)}）→ 临时目录")
        shutil.copyfile(db_path, tmp_db)

        conn = sqlite3.connect(tmp_db)
        try:
            rows, symbols, span, names = _stats(conn)
            log(f"原始：{rows:,} 行 / {symbols:,} 只 / {span} / 名称 {names:,} 条")

            if bars and bars > 0:
                before = rows
                conn.execute(_TRIM_SQL, (bars,))
                conn.commit()
                rows = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
                log(f"裁剪：每只保留最近 {bars} 根 → 删除 {before - rows:,} 行，剩 {rows:,} 行")
            else:
                log("裁剪：已跳过（--bars 0）")

            # 裁剪会留下大量空洞页，VACUUM 才真正把文件缩下来
            log("VACUUM …（大库需要几十秒）")
            conn.execute("VACUUM")
            conn.commit()

            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"VACUUM 后完整性校验失败：{check}")
            log("完整性校验：ok")
        finally:
            conn.close()

        if symbols < _MIN_SYMBOLS or rows < _MIN_ROWS:
            raise RuntimeError(
                f"数据量异常（{symbols} 只 / {rows} 行），低于安全阈值"
                f"（{_MIN_SYMBOLS} 只 / {_MIN_ROWS} 行），拒绝生成种子。"
                f"请先确认库已补齐：python main.py --backfill"
            )

        # VACUUM 之后的真实大小 —— 压缩率要用它当分母，
        # 拿裁剪前的原始库来算会把比率算虚高。
        slim_bytes = tmp_db.stat().st_size
        log(f"裁剪 + VACUUM 后：{_human(slim_bytes)}")

        # mtime=0：gzip 头默认会写入源文件的修改时间，而源文件是每次新建的临时副本，
        # 时间戳一变整包 sha256 就跟着变。固定成 0 让打包结果**可复现** ——
        # 同样的数据库打出来的 sha256 永远一致，Release 说明里记的值才可信。
        with tmp_db.open("rb") as fi, out_path.open("wb") as raw_out:
            with gzip.GzipFile(
                filename="", fileobj=raw_out, mode="wb", compresslevel=9, mtime=0
            ) as fo:
                shutil.copyfileobj(fi, fo, length=1 << 20)

    raw = db_path.stat().st_size
    packed = out_path.stat().st_size
    digest = _sha256(out_path)

    log(f"完成：{out_path}")
    log(
        f"  {_human(packed)}"
        f"（源库 {_human(raw)} → VACUUM 后 {_human(slim_bytes)}，"
        f"相对 VACUUM 后压缩率 {slim_bytes / packed:.2f}x，sha256 {digest[:16]}…）"
    )

    return {
        "rows": rows,
        "symbols": symbols,
        "names": names,
        "span": span,
        "bars": bars,
        "raw_bytes": raw,
        "slim_bytes": slim_bytes,
        "packed_bytes": packed,
        "sha256": digest,
        "out": str(out_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 Actions 冷启动种子")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"源数据库（默认 {DEFAULT_DB}）")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"输出路径（默认 {DEFAULT_OUT}）")
    parser.add_argument(
        "--bars",
        type=int,
        default=DEFAULT_BARS,
        help=f"每只票保留最近 N 根 K 线（默认 {DEFAULT_BARS}，0 表示不截断）",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出最终结果")
    args = parser.parse_args()

    try:
        stats = pack(Path(args.db), Path(args.out), args.bars, verbose=not args.quiet)
    except Exception as exc:  # noqa: BLE001
        print(f"打包失败：{exc}", file=sys.stderr)
        return 1

    # 给 CI 用：写出 key=value 便于 append 进 $GITHUB_OUTPUT
    print(f"SEED_SHA256={stats['sha256']}")
    print(f"SEED_BYTES={stats['packed_bytes']}")
    print(f"SEED_SLIM_BYTES={stats['slim_bytes']}")
    print(f"SEED_ROWS={stats['rows']}")
    print(f"SEED_SYMBOLS={stats['symbols']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
