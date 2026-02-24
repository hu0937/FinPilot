import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Optional
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_PATH

_local = threading.local()

DDL = """
CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    market      TEXT NOT NULL,
    name        TEXT,
    added_by    INTEGER,
    added_at    DATETIME DEFAULT (datetime('now', 'localtime')),
    is_active   INTEGER DEFAULT 1,
    note        TEXT,
    UNIQUE(symbol, market)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_market ON watchlist(market, is_active);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    market      TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    open_price  REAL,
    high_price  REAL,
    low_price   REAL,
    close_price REAL NOT NULL,
    volume      INTEGER,
    ma5         REAL,
    ma20        REAL,
    vol_ma20    REAL,
    n_day_high  REAL,
    created_at  DATETIME DEFAULT (datetime('now', 'localtime')),
    UNIQUE(symbol, market, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_price_symbol_date ON price_history(symbol, market, trade_date DESC);

CREATE TABLE IF NOT EXISTS position_lots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    market     TEXT NOT NULL,
    trade_date DATE NOT NULL,
    quantity   REAL NOT NULL,
    remaining  REAL NOT NULL,
    unit_cost  REAL NOT NULL,
    created_at DATETIME DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_lots_symbol ON position_lots(symbol, market, trade_date ASC);

CREATE TABLE IF NOT EXISTS discovered_strategies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id     TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description     TEXT,
    cagr            REAL,
    sharpe          REAL,
    mdd             REAL,
    win_ratio       REAL,
    passed          INTEGER DEFAULT 0,
    code            TEXT,
    file_path       TEXT,
    tried_at        DATETIME DEFAULT (datetime('now','localtime')),
    notified        INTEGER DEFAULT 0,
    calmar_ratio    REAL,
    volatility      REAL,
    factor_list     TEXT,
    ranking_factor  TEXT,
    rebalance_freq  TEXT,
    position_limit  REAL
);
"""


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


@contextmanager
def transaction():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    with transaction() as conn:
        conn.executescript(DDL)
    print(f"DB initialized: {DB_PATH}")


# ── Watchlist ────────────────────────────────────────────────

def add_to_watchlist(symbol: str, market: str, added_by: int = 0) -> bool:
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watchlist(symbol, market, added_by) VALUES(?,?,?)",
                (symbol.upper(), market.upper(), added_by)
            )
            return conn.execute(
                "SELECT changes()"
            ).fetchone()[0] > 0
    except Exception:
        return False


def remove_from_watchlist(symbol: str, market: str) -> bool:
    with transaction() as conn:
        conn.execute(
            "UPDATE watchlist SET is_active=0 WHERE symbol=? AND market=?",
            (symbol.upper(), market.upper())
        )
        return conn.execute("SELECT changes()").fetchone()[0] > 0


def get_watchlist(market: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if market:
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE is_active=1 AND market=? ORDER BY market,symbol",
            (market.upper(),)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE is_active=1 ORDER BY market,symbol"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Price History ────────────────────────────────────────────

def upsert_prices(records: list[dict]):
    if not records:
        return
    with transaction() as conn:
        conn.executemany(
            """INSERT INTO price_history
               (symbol,market,trade_date,open_price,high_price,low_price,
                close_price,volume,ma5,ma20,vol_ma20,n_day_high)
               VALUES(:symbol,:market,:trade_date,:open_price,:high_price,:low_price,
                      :close_price,:volume,:ma5,:ma20,:vol_ma20,:n_day_high)
               ON CONFLICT(symbol,market,trade_date) DO UPDATE SET
                 open_price=excluded.open_price,
                 high_price=excluded.high_price,
                 low_price=excluded.low_price,
                 close_price=excluded.close_price,
                 volume=excluded.volume,
                 ma5=excluded.ma5,
                 ma20=excluded.ma20,
                 vol_ma20=excluded.vol_ma20,
                 n_day_high=excluded.n_day_high""",
            records
        )


def get_recent_prices(symbol: str, market: str, days: int = 60) -> list[dict]:
    conn = get_conn()
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT * FROM price_history
           WHERE symbol=? AND market=? AND trade_date>=?
           ORDER BY trade_date ASC""",
        (symbol.upper(), market.upper(), since)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Positions (FIFO) ─────────────────────────────────────────

def buy_position(symbol: str, market: str, quantity: float, unit_cost: float, trade_date: Optional[str] = None) -> bool:
    if trade_date is None:
        trade_date = date.today().isoformat()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO position_lots(symbol, market, trade_date, quantity, remaining, unit_cost) VALUES(?,?,?,?,?,?)",
            (symbol.upper(), market.upper(), trade_date, quantity, quantity, unit_cost)
        )
    return True


def sell_position(symbol: str, market: str, quantity: float) -> dict:
    sym, mkt = symbol.upper(), market.upper()
    conn = get_conn()
    lots = conn.execute(
        "SELECT id, remaining FROM position_lots WHERE symbol=? AND market=? AND remaining>0 ORDER BY trade_date ASC, id ASC",
        (sym, mkt)
    ).fetchall()
    to_sell = quantity
    with transaction() as conn:
        for lot in lots:
            if to_sell <= 0:
                break
            consume = min(to_sell, lot["remaining"])
            conn.execute("UPDATE position_lots SET remaining=? WHERE id=?", (lot["remaining"] - consume, lot["id"]))
            to_sell -= consume
    sold = quantity - to_sell
    return {"sold": sold, "insufficient": to_sell > 0}


def get_positions(market: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if market:
        rows = conn.execute(
            """SELECT symbol, market, SUM(remaining) AS quantity,
                      SUM(remaining * unit_cost) / SUM(remaining) AS avg_cost
               FROM position_lots WHERE remaining>0 AND market=?
               GROUP BY symbol, market ORDER BY market, symbol""",
            (market.upper(),)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT symbol, market, SUM(remaining) AS quantity,
                      SUM(remaining * unit_cost) / SUM(remaining) AS avg_cost
               FROM position_lots WHERE remaining>0
               GROUP BY symbol, market ORDER BY market, symbol"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_position_lots(symbol: str, market: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM position_lots WHERE symbol=? AND market=? ORDER BY trade_date ASC, id ASC",
        (symbol.upper(), market.upper())
    ).fetchall()
    return [dict(r) for r in rows]


# ── Discovered Strategies ────────────────────────────────────

def is_strategy_tried(template_id: str) -> bool:
    row = get_conn().execute(
        "SELECT id FROM discovered_strategies WHERE template_id=?", (template_id,)
    ).fetchone()
    return row is not None


def _migrate_discovered_strategies():
    """idempotent migrations: 新增欄位（已存在時忽略）"""
    conn = get_conn()
    new_cols = [
        ('hypothesis',     'TEXT'),
        ('condition_group','TEXT'),
        ('calmar_ratio',   'REAL'),
        ('volatility',     'REAL'),
        ('factor_list',    'TEXT'),
        ('ranking_factor', 'TEXT'),
        ('rebalance_freq', 'TEXT'),
        ('position_limit', 'REAL'),
    ]
    for col, coldef in new_cols:
        try:
            conn.execute(f"ALTER TABLE discovered_strategies ADD COLUMN {col} {coldef}")
            conn.commit()
        except Exception:
            pass  # 欄位已存在，忽略


def save_discovered_strategy(template_id: str, name: str, description: str,
                              cagr: float, sharpe: float, mdd: float, win_ratio: float,
                              passed: bool, code: str = None, file_path: str = None,
                              hypothesis: str = None, condition_group: str = None,
                              calmar_ratio: float = None, volatility: float = None,
                              factor_list: str = None, ranking_factor: str = None,
                              rebalance_freq: str = None, position_limit: float = None):
    _migrate_discovered_strategies()
    with transaction() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO discovered_strategies
              (template_id, name, description, cagr, sharpe, mdd, win_ratio,
               passed, code, file_path, hypothesis, condition_group,
               calmar_ratio, volatility, factor_list, ranking_factor,
               rebalance_freq, position_limit)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (template_id, name, description, cagr, sharpe, mdd, win_ratio,
              1 if passed else 0, code, file_path, hypothesis, condition_group,
              calmar_ratio, volatility, factor_list, ranking_factor,
              rebalance_freq, position_limit))


def get_passed_strategy_count() -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS n FROM discovered_strategies WHERE passed=1"
    ).fetchone()
    return row["n"] if row else 0


def get_passed_by_condition_group(group_hash: str) -> Optional[dict]:
    """找同條件組（因子鍵相同）中現有的 passed 策略"""
    row = get_conn().execute(
        "SELECT * FROM discovered_strategies WHERE condition_group=? AND passed=1 LIMIT 1",
        (group_hash,)
    ).fetchone()
    return dict(row) if row else None


def supersede_strategy(template_id: str):
    """將舊策略標記為 passed=0（被新版取代）"""
    with transaction() as conn:
        conn.execute(
            "UPDATE discovered_strategies SET passed=0 WHERE template_id=?",
            (template_id,)
        )


def get_all_discovered_strategies() -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM discovered_strategies ORDER BY tried_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


