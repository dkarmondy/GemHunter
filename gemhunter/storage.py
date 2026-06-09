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
    mode             TEXT,
    reasons          TEXT,
    rejected         INTEGER DEFAULT 0,
    reject_reason    TEXT,
    first_seen       REAL,
    last_seen        REAL
);
CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(score);
CREATE INDEX IF NOT EXISTS idx_listings_rejected ON listings(rejected);
"""


class Storage:
    def __init__(self, db_path: str | Path = "gemhunter.db"):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")   # safe for 24/7 read+write
        self._conn.executescript(SCHEMA)
        self._conn.commit()

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
                score, mode, reasons, rejected, reject_reason, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   COALESCE((SELECT first_seen FROM listings WHERE item_id = ?), ?), ?)""",
            (l.item_id, l.search_name, l.title, l.price, l.currency, l.buying_option,
             l.bid_count, l.seller_username, l.seller_feedback_pct, l.seller_feedback_score,
             l.condition, l.url, l.image_url, result.score, result.mode,
             ", ".join(result.reasons), int(result.rejected), result.reject_reason,
             l.item_id, now, now),
        )
        self._conn.commit()

    def top_gems(self, min_score: float = 0.0, limit: int = 200) -> list[dict]:
        cur = self._conn.execute(
            """SELECT * FROM listings WHERE rejected = 0 AND score >= ?
               ORDER BY score DESC, last_seen DESC LIMIT ?""",
            (min_score, limit))
        return [dict(r) for r in cur.fetchall()]

    def stats(self) -> dict:
        row = self._conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN rejected=1 THEN 1 ELSE 0 END) AS rejected,
                      SUM(CASE WHEN rejected=0 THEN 1 ELSE 0 END) AS gems
               FROM listings""").fetchone()
        return dict(row)

    def close(self) -> None:
        self._conn.close()
