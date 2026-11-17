"""SQLite storage: National Rarities lots and votes, and what Wild Cards has found."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


SCHEMA = """
-- Which National Rarities lots have already been reported, and his heart /
-- thumbs-down on each. The question asked of it by the digest is "have I told
-- him about this one yet?"; the tab reads the votes back to rank the week.
CREATE TABLE IF NOT EXISTS rarities_seen (
    item_id     TEXT PRIMARY KEY,
    title       TEXT,
    ends        TEXT,
    taste       REAL,
    first_seen  REAL,
    saved       INTEGER DEFAULT 0,
    saved_at    REAL,
    disliked    INTEGER DEFAULT 0,
    disliked_at REAL
);

-- Wild Cards. Every listing a sweep has read, kept so its page is fetched
-- once: `detail` is what getItem said, `payload` the scored card when it
-- qualified. A row whose last_seen is the latest sweep is live on eBay now.
CREATE TABLE IF NOT EXISTS wildcard_items (
    item_id     TEXT PRIMARY KEY,
    title       TEXT,
    brand       TEXT,
    score       REAL,
    status      TEXT,               -- 'ok' or 'excluded'
    why         TEXT,
    payload     TEXT,
    detail      TEXT,
    first_seen  REAL,
    last_seen   REAL,
    passed      INTEGER DEFAULT 0
);

-- The makers found, and how many live listings eBay had for each when asked.
-- This is the private list of unknowns the tab exists to build.
CREATE TABLE IF NOT EXISTS wildcard_brands (
    brand       TEXT PRIMARY KEY,   -- lower-cased
    name        TEXT,
    live_count  INTEGER,
    checked_at  REAL,
    blocked     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wildcard_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

BLOCKED_MAKER = "maker you blocked"


class Storage:
    def __init__(self, db_path: str | Path = "gemhunter.db"):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # The web app and the digest timer can have the file open at once.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        # rarities_seen predates the heart and thumbs-down on some databases.
        for col, decl in [("saved", "INTEGER DEFAULT 0"), ("saved_at", "REAL"),
                          ("disliked", "INTEGER DEFAULT 0"),
                          ("disliked_at", "REAL")]:
            try:
                self._conn.execute(
                    f"ALTER TABLE rarities_seen ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass

    def has_rarities_history(self) -> bool:
        """False only before the very first sweep, when everything looks new."""
        return bool(self._conn.execute(
            "SELECT 1 FROM rarities_seen LIMIT 1").fetchone())

    def record_rarities(self, items: list[dict]) -> list[dict]:
        """Remember these lots; hand back only the ones never seen before."""
        if not items:
            return []
        known = {r["item_id"] for r in self._conn.execute(
            "SELECT item_id FROM rarities_seen")}
        fresh = [i for i in items if i["id"] not in known]
        now = time.time()
        self._conn.executemany(
            """INSERT OR IGNORE INTO rarities_seen
                   (item_id, title, ends, taste, first_seen)
               VALUES (?, ?, ?, ?, ?)""",
            [(i["id"], i.get("title", ""), i.get("ends", ""),
              i.get("taste", 0), now) for i in fresh],
        )
        self._conn.commit()
        return fresh

    def set_rarities_vote(self, item_id: str, vote: int, title: str = "") -> bool:
        """+1 heart, -1 thumbs-down, 0 clears. The two are exclusive.

        The row is created on demand: a vote comes from browsing the tab, which
        can happen before the digest's first sweep has recorded that lot.
        """
        now = time.time()
        saved, disliked = (1 if vote > 0 else 0), (1 if vote < 0 else 0)
        self._conn.execute(
            """INSERT INTO rarities_seen
                   (item_id, title, first_seen, saved, saved_at,
                    disliked, disliked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET
                   saved = excluded.saved,
                   saved_at = excluded.saved_at,
                   disliked = excluded.disliked,
                   disliked_at = excluded.disliked_at,
                   title = CASE WHEN rarities_seen.title IS NULL
                                  OR rarities_seen.title = ''
                                THEN excluded.title ELSE rarities_seen.title END""",
            (item_id, title, now, saved, now if saved else None,
             disliked, now if disliked else None),
        )
        self._conn.commit()
        return True

    def rarities_voted_ids(self) -> tuple[set, set]:
        """(hearted ids, thumbs-down ids)."""
        saved, disliked = set(), set()
        for row in self._conn.execute(
                "SELECT item_id, saved, disliked FROM rarities_seen "
                "WHERE saved = 1 OR disliked = 1"):
            (saved if row["saved"] else disliked).add(row["item_id"])
        return saved, disliked

    def rarities_liked_titles(self, limit: int = 300) -> list[str]:
        """What he hearted, newest first — the training set for the boost."""
        cur = self._conn.execute(
            """SELECT title FROM rarities_seen
               WHERE saved = 1 AND title IS NOT NULL AND title != ''
               ORDER BY saved_at DESC LIMIT ?""", (limit,))
        return [r["title"] for r in cur.fetchall()]

    def rarities_disliked_titles(self, limit: int = 300) -> list[str]:
        """What he thumbed down — the training set for the demotion."""
        cur = self._conn.execute(
            """SELECT title FROM rarities_seen
               WHERE disliked = 1 AND title IS NOT NULL AND title != ''
               ORDER BY disliked_at DESC LIMIT ?""", (limit,))
        return [r["title"] for r in cur.fetchall()]

    # --- Wild Cards --------------------------------------------------------

    def wildcard_rows(self, item_ids: list[str]) -> dict:
        """What is already known about these listings, by item id."""
        out = {}
        for at in range(0, len(item_ids), 500):
            chunk = item_ids[at:at + 500]
            marks = ",".join("?" * len(chunk))
            for row in self._conn.execute(
                    f"SELECT item_id, status, detail, passed, first_seen "
                    f"FROM wildcard_items WHERE item_id IN ({marks})", chunk):
                out[row["item_id"]] = row
        return out

    def wildcard_save(self, rows: list[dict], now: float) -> None:
        """Record a sweep's verdicts. first_seen and the pass flag survive."""
        self._conn.executemany(
            """INSERT INTO wildcard_items
                   (item_id, title, brand, score, status, why, payload, detail,
                    first_seen, last_seen)
               VALUES (:item_id, :title, :brand, :score, :status, :why,
                       :payload, :detail, :now, :now)
               ON CONFLICT(item_id) DO UPDATE SET
                   title = excluded.title, brand = excluded.brand,
                   score = excluded.score, status = excluded.status,
                   why = excluded.why, payload = excluded.payload,
                   detail = excluded.detail, last_seen = excluded.last_seen""",
            [{**r, "now": now} for r in rows])
        self._conn.commit()

    def wildcard_live(self, sweep_ts: float) -> list:
        """The qualifying listings the given sweep saw, passed ones included."""
        return self._conn.execute(
            """SELECT item_id, payload, first_seen, passed FROM wildcard_items
               WHERE status = 'ok' AND payload != '' AND last_seen >= ?
               ORDER BY score DESC""", (sweep_ts - 1,)).fetchall()

    def wildcard_set_pass(self, item_id: str, passed: bool) -> bool:
        cur = self._conn.execute(
            "UPDATE wildcard_items SET passed = ? WHERE item_id = ?",
            (1 if passed else 0, item_id))
        self._conn.commit()
        return cur.rowcount > 0

    def wildcard_brand(self, key: str):
        return self._conn.execute(
            "SELECT * FROM wildcard_brands WHERE brand = ?", (key,)).fetchone()

    def wildcard_set_brand(self, key: str, name: str, live_count, now: float) -> None:
        self._conn.execute(
            """INSERT INTO wildcard_brands (brand, name, live_count, checked_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(brand) DO UPDATE SET
                   name = excluded.name, live_count = excluded.live_count,
                   checked_at = excluded.checked_at""",
            (key, name, live_count, now))
        self._conn.commit()

    def wildcard_blocked_brands(self) -> set:
        return {r["brand"] for r in self._conn.execute(
            "SELECT brand FROM wildcard_brands WHERE blocked = 1")}

    def wildcard_block_brand(self, key: str, name: str, blocked: bool) -> None:
        """"I know this maker": its listings leave the tab now, not next sweep."""
        self._conn.execute(
            """INSERT INTO wildcard_brands (brand, name, blocked) VALUES (?, ?, ?)
               ON CONFLICT(brand) DO UPDATE SET blocked = excluded.blocked""",
            (key, name, 1 if blocked else 0))
        if blocked:
            self._conn.execute(
                """UPDATE wildcard_items SET status = 'excluded', why = ?
                   WHERE status = 'ok' AND lower(trim(brand)) = ?""",
                (BLOCKED_MAKER, key))
        else:
            self._conn.execute(
                """UPDATE wildcard_items SET status = 'ok', why = ''
                   WHERE why = ? AND payload != '' AND lower(trim(brand)) = ?""",
                (BLOCKED_MAKER, key))
        self._conn.commit()

    def wildcard_makers(self) -> list[dict]:
        """Every maker that has ever put a card on the tab, rarest first."""
        cur = self._conn.execute(
            """SELECT b.brand AS key, COALESCE(b.name, i.brand) AS name,
                      b.live_count, COALESCE(b.blocked, 0) AS blocked,
                      COUNT(*) AS listings, MAX(i.score) AS best,
                      MAX(i.last_seen) AS last_seen
               FROM wildcard_items i
               JOIN wildcard_brands b ON b.brand = lower(trim(i.brand))
               WHERE i.payload != ''
               GROUP BY b.brand
               ORDER BY blocked, b.live_count IS NULL, b.live_count, name""")
        return [dict(r) for r in cur.fetchall()]

    def wildcard_meta(self, key: str):
        row = self._conn.execute(
            "SELECT value FROM wildcard_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def wildcard_set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            """INSERT INTO wildcard_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
