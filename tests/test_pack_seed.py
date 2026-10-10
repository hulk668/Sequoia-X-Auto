"""种子打包脚本测试（`scripts/pack_seed.py`）。

重点验证三件事：
  1. 裁剪按 symbol 独立生效，且保留的是**最近**的 N 根（不是最早的）；
  2. 数据量异常时拒绝出包 —— 一个空种子发上 Release 会把冷启动链路彻底弄坏；
  3. 打包可复现 —— 同一个库打两次 sha256 必须完全一致（gzip 头的 mtime 已固定为 0）。
"""

import gzip
import importlib.util
import sqlite3
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("pack_seed", _ROOT / "scripts" / "pack_seed.py")
assert _spec and _spec.loader
pack_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pack_seed)


def _make_db(path: Path, symbols: dict[str, int]) -> None:
    """造一个最小数据库：symbols = {代码: 根数}，日期从 2024-01-01 起逐日递增。"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE stock_daily ("
        "symbol TEXT, date TEXT, open REAL, high REAL, low REAL, "
        "close REAL, volume REAL, turnover REAL, PRIMARY KEY (symbol, date))"
    )
    conn.execute("CREATE TABLE stock_name (symbol TEXT PRIMARY KEY, name TEXT)")
    for symbol, bars in symbols.items():
        for i in range(bars):
            day = f"2024-01-{i + 1:02d}"
            conn.execute(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?)",
                (symbol, day, 10.0, 11.0, 9.0, 10.5, 1000.0, 10500.0),
            )
        conn.execute("INSERT INTO stock_name VALUES (?,?)", (symbol, f"名称{symbol}"))
    conn.commit()
    conn.close()


def _rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture()
def small_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """3 只票 × 10 根，并把安全阈值调到能通过。"""
    monkeypatch.setattr(pack_seed, "_MIN_SYMBOLS", 1)
    monkeypatch.setattr(pack_seed, "_MIN_ROWS", 1)
    db = tmp_path / "test.db"
    _make_db(db, {"600000": 10, "000001": 10, "300750": 6})
    return db


# ── 裁剪 ──


def test_pack_trims_each_symbol_independently(small_db: Path, tmp_path: Path) -> None:
    """每只票各留最近 4 根：3 只 × 4 = 12 行（不足 4 根的按原样保留）。"""
    out = tmp_path / "seed.db.gz"
    stats = pack_seed.pack(small_db, out, bars=4, verbose=False)

    assert stats["rows"] == 12
    assert stats["symbols"] == 3

    # 解出来验一验：留下的必须是最新的 4 天，不能是最早的
    unpacked = tmp_path / "unpacked.db"
    with gzip.open(out, "rb") as fi, unpacked.open("wb") as fo:
        fo.write(fi.read())
    conn = sqlite3.connect(unpacked)
    try:
        dates = [r[0] for r in conn.execute(
            "SELECT date FROM stock_daily WHERE symbol='600000' ORDER BY date"
        )]
    finally:
        conn.close()
    assert dates == ["2024-01-07", "2024-01-08", "2024-01-09", "2024-01-10"]


def test_pack_with_zero_bars_keeps_everything(small_db: Path, tmp_path: Path) -> None:
    """bars=0 表示不裁剪（排查用），26 行应原样保留。"""
    out = tmp_path / "seed.db.gz"
    stats = pack_seed.pack(small_db, out, bars=0, verbose=False)
    assert stats["rows"] == 10 + 10 + 6
    assert stats["symbols"] == 3


def test_pack_does_not_touch_source_db(small_db: Path, tmp_path: Path) -> None:
    """打包是在副本上做的，源库必须一行不少（本机可能还有进程在读它）。"""
    before = _rows(small_db)
    pack_seed.pack(small_db, tmp_path / "seed.db.gz", bars=2, verbose=False)
    assert _rows(small_db) == before


# ── 安全阈值 ──


def test_pack_rejects_suspiciously_small_db(tmp_path: Path) -> None:
    """阈值内建：股票数或行数不够就拒绝出包，避免把空种子发上 Release。"""
    db = tmp_path / "tiny.db"
    _make_db(db, {"600000": 5})  # 1 只 / 5 行，远低于默认阈值

    with pytest.raises(RuntimeError, match="数据量异常"):
        pack_seed.pack(db, tmp_path / "seed.db.gz", bars=0, verbose=False)


def test_pack_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        pack_seed.pack(tmp_path / "nope.db", tmp_path / "seed.db.gz", verbose=False)


# ── 可复现性 ──


def test_pack_is_reproducible(small_db: Path, tmp_path: Path) -> None:
    """gzip 头的 mtime 固定为 0 → 同一个库打两次 sha256 必须一致。

    否则 Release 说明里记的 sha256 下次重新打包就对不上了。
    """
    a = pack_seed.pack(small_db, tmp_path / "a.db.gz", bars=4, verbose=False)
    b = pack_seed.pack(small_db, tmp_path / "b.db.gz", bars=4, verbose=False)

    assert a["sha256"] == b["sha256"]
    assert a["packed_bytes"] == b["packed_bytes"]
    assert (tmp_path / "a.db.gz").read_bytes() == (tmp_path / "b.db.gz").read_bytes()


def test_pack_sha256_matches_file(small_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "seed.db.gz"
    stats = pack_seed.pack(small_db, out, bars=4, verbose=False)
    assert stats["sha256"] == pack_seed._sha256(out)
    assert stats["packed_bytes"] == out.stat().st_size


def test_pack_output_is_valid_gzip_sqlite(small_db: Path, tmp_path: Path) -> None:
    """产物必须是能 gunzip、能打开、能过 quick_check 的真 SQLite。"""
    out = tmp_path / "seed.db.gz"
    pack_seed.pack(small_db, out, bars=4, verbose=False)

    unpacked = tmp_path / "unpacked.db"
    with gzip.open(out, "rb") as fi, unpacked.open("wb") as fo:
        fo.write(fi.read())

    conn = sqlite3.connect(unpacked)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM stock_name").fetchone()[0] == 3
    finally:
        conn.close()


# ── 输出格式 ──


def test_human_formats_units() -> None:
    assert pack_seed._human(512) == "512.0 B"
    assert pack_seed._human(2048) == "2.0 KB"
    assert pack_seed._human(5 * 1024 * 1024) == "5.0 MB"
    assert pack_seed._human(3 * 1024**3) == "3.0 GB"
