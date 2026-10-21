"""SQLite storage for National Rarities: which lots have been seen, and the votes on them."""

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
"""


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

    def close(self) -> None:
        self._conn.close()
