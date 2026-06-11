"""SQLite storage on the SSD — the local dataset.

One row per listing we observe, with its score/mode/reasons (gems and rejects alike).
This is both the dedupe ledger AND the seed of the comps/observations dataset that
Phase 3 (auction tracking) and the forecaster will grow into.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id          TEXT PRIMARY KEY,
    search_name      TEXT,
    title            TEXT,
    price            REAL,
    currency         TEXT,
    buying_option    TEXT,
    bid_count        INTEGER,
    seller_username  TEXT,
    seller_pct       REAL,
    seller_score     INTEGER,
    condition        TEXT,
    url              TEXT,
    image_url        TEXT,
    score            REAL,
    stream           TEXT,
    mode             TEXT,
    reasons          TEXT,
    rejected         INTEGER DEFAULT 0,
    reject_reason    TEXT,
    hidden           INTEGER DEFAULT 0,
    saved            INTEGER DEFAULT 0,
    first_seen       REAL,
    last_seen        REAL
);
CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(score);
CREATE INDEX IF NOT EXISTS idx_listings_rejected ON listings(rejected);

CREATE TABLE IF NOT EXISTS comps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT,      -- my-purchase | my-sale | bid-history | insights
    brand         TEXT,
    model         TEXT,
    reference     TEXT,
    caliber       TEXT,
    title         TEXT,
    condition     TEXT,      -- e.g. "broken for repair", "used", "NOS"
    sale_date     TEXT,      -- ISO date when known
    price         REAL,      -- final/transaction price
    currency      TEXT DEFAULT 'USD',
    bid_count     INTEGER,
    seller        TEXT,
    url           TEXT,
    notes         TEXT,
    UNIQUE(source, title, sale_date, price)
);
"""


class Storage:
    def __init__(self, db_path: str | Path = "gemhunter.db"):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")   # safe for 24/7 read+write
        self._conn.executescript(SCHEMA)
        self._migrate()
        # Indexes on migrated columns must come AFTER the migration adds them.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_stream ON listings(stream)")
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns to a pre-existing db (e.g. the Pi's) without losing data."""
        for col, decl in [
            ("stream", "TEXT"),
            ("hidden", "INTEGER DEFAULT 0"),
            ("saved", "INTEGER DEFAULT 0"),
        ]:
            try:
                self._conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists

    def is_new(self, item_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM listings WHERE item_id = ?", (item_id,))
        return cur.fetchone() is None

    def record_result(self, result) -> None:
        """Upsert a scored listing (result has .listing, .score, .mode, .reasons, …)."""
        l = result.listing
        now = time.time()
        self._conn.execute(
            """INSERT OR REPLACE INTO listings
               (item_id, search_name, title, price, currency, buying_option, bid_count,
                seller_username, seller_pct, seller_score, condition, url, image_url,
                score, stream, mode, reasons, rejected, reject_reason, hidden, saved,
                first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   COALESCE((SELECT hidden FROM listings WHERE item_id = ?), 0),
                   COALESCE((SELECT saved FROM listings WHERE item_id = ?), 0),
                   COALESCE((SELECT first_seen FROM listings WHERE item_id = ?), ?), ?)""",
            (l.item_id, l.search_name, l.title, l.price, l.currency, l.buying_option,
             l.bid_count, l.seller_username, l.seller_feedback_pct, l.seller_feedback_score,
             l.condition, l.url, l.image_url, result.score, result.stream, result.mode,
             ", ".join(result.reasons), int(result.rejected), result.reject_reason,
             l.item_id, l.item_id, l.item_id, now, now),
        )
        self._conn.commit()

    def top_gems(self, min_score: float = 0.0, limit: int = 200,
                 stream: str | None = None, include_hidden: bool = False) -> list[dict]:
        hidden_clause = "" if include_hidden else " AND hidden = 0"
        if stream:
            cur = self._conn.execute(
                f"""SELECT * FROM listings WHERE rejected = 0 AND score >= ? AND stream = ?{hidden_clause}
                   ORDER BY score DESC, last_seen DESC LIMIT ?""",
                (min_score, stream, limit))
        else:
            cur = self._conn.execute(
                f"""SELECT * FROM listings WHERE rejected = 0 AND score >= ?{hidden_clause}
                   ORDER BY score DESC, last_seen DESC LIMIT ?""",
                (min_score, limit))
        return [dict(r) for r in cur.fetchall()]

    def saved_gems(self, limit: int = 300) -> list[dict]:
        cur = self._conn.execute(
            """SELECT * FROM listings
               WHERE rejected = 0 AND saved = 1 AND hidden = 0
               ORDER BY last_seen DESC LIMIT ?""",
            (limit,))
        return [dict(r) for r in cur.fetchall()]

    def feedback_rows(self, limit: int = 500) -> list[dict]:
        cur = self._conn.execute(
            """SELECT item_id, title, search_name, stream, reasons, saved, hidden
               FROM listings
               WHERE rejected = 0 AND (saved = 1 OR hidden = 1)
               ORDER BY last_seen DESC LIMIT ?""",
            (limit,))
        return [dict(r) for r in cur.fetchall()]

    def set_saved(self, item_id: str, saved: bool) -> bool:
        cur = self._conn.execute(
            "UPDATE listings SET saved = ? WHERE item_id = ?", (int(saved), item_id))
        self._conn.commit()
        return cur.rowcount > 0

    def set_hidden(self, item_id: str, hidden: bool) -> bool:
        cur = self._conn.execute(
            "UPDATE listings SET hidden = ? WHERE item_id = ?", (int(hidden), item_id))
        self._conn.commit()
        return cur.rowcount > 0

    def get_listing(self, item_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM listings WHERE item_id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def add_comp(self, **kw) -> bool:
        """Insert a comp row; returns False if it was a duplicate."""
        cols = ["source", "brand", "model", "reference", "caliber", "title",
                "condition", "sale_date", "price", "currency", "bid_count",
                "seller", "url", "notes"]
        vals = [kw.get(c) for c in cols]
        try:
            self._conn.execute(
                f"INSERT INTO comps ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                vals)
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def comps(self, brand: str | None = None) -> list[dict]:
        q = "SELECT * FROM comps"
        args: tuple = ()
        if brand:
            q += " WHERE brand LIKE ?"
            args = (f"%{brand}%",)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY sale_date", args)]

    def stats(self) -> dict:
        row = self._conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN rejected=1 THEN 1 ELSE 0 END) AS rejected,
                      SUM(CASE WHEN rejected=0 THEN 1 ELSE 0 END) AS gems
               FROM listings""").fetchone()
        return dict(row)

    def close(self) -> None:
        self._conn.close()
