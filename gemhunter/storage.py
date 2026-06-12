"""SQLite storage on the SSD — the local dataset.

One row per listing we observe, with its score/mode/reasons (gems and rejects alike).
This is both the dedupe ledger AND the seed of the comps/observations dataset that
Phase 3 (auction tracking) and the forecaster will grow into.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path


FINGERPRINT_STOP = {
    "watch", "watches", "mens", "men", "vintage", "rare", "used", "pre", "owned",
    "with", "and", "the", "for", "from", "box", "papers", "paper", "full", "set",
    "stainless", "steel", "gold", "automatic", "manual", "working", "running",
}


def _fingerprint_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    useful = [t for t in tokens if len(t) > 2 and t not in FINGERPRINT_STOP]
    return sorted(useful)[:16]


def listing_fingerprint(listing) -> str:
    """Stable-ish relist key: same seller + canonical title tokens."""
    seller = (getattr(listing, "seller_username", "") or "").strip().lower()
    title_tokens = _fingerprint_tokens(getattr(listing, "title", ""))
    base = f"{seller}|{' '.join(title_tokens)}"
    if not title_tokens:
        base = f"{seller}|{getattr(listing, 'title', '')}".lower()
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:16]

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id          TEXT PRIMARY KEY,
    search_name      TEXT,
    title            TEXT,
    price            REAL,
    shipping_cost    REAL DEFAULT 0,
    shipping_known   INTEGER DEFAULT 0,
    import_charges   REAL DEFAULT 0,
    import_charges_known INTEGER DEFAULT 0,
    currency         TEXT,
    buying_option    TEXT,
    bid_count        INTEGER,
    seller_username  TEXT,
    seller_pct       REAL,
    seller_score     INTEGER,
    condition        TEXT,
    country          TEXT,
    url              TEXT,
    image_url        TEXT,
    score            REAL,
    opportunity      REAL DEFAULT 0,
    confidence       REAL DEFAULT 0,
    stream           TEXT,
    mode             TEXT,
    reasons          TEXT,
    risk_tags        TEXT,
    action_note      TEXT,
    rejected         INTEGER DEFAULT 0,
    reject_reason    TEXT,
    hidden           INTEGER DEFAULT 0,
    saved            INTEGER DEFAULT 0,
    feedback_reason  TEXT,
    fingerprint      TEXT,
    first_seen       REAL,
    last_seen        REAL
);
CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(score);
CREATE INDEX IF NOT EXISTS idx_listings_rejected ON listings(rejected);

CREATE TABLE IF NOT EXISTS listing_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT,
    observed_at     REAL,
    search_name     TEXT,
    price           REAL,
    shipping_cost   REAL DEFAULT 0,
    shipping_known  INTEGER DEFAULT 0,
    import_charges  REAL DEFAULT 0,
    import_charges_known INTEGER DEFAULT 0,
    currency        TEXT,
    buying_option   TEXT,
    bid_count       INTEGER,
    score           REAL,
    stream          TEXT,
    country         TEXT,
    fingerprint     TEXT
);
CREATE INDEX IF NOT EXISTS idx_observations_item ON listing_observations(item_id, observed_at);

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
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint)")
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns to a pre-existing db (e.g. the Pi's) without losing data."""
        for col, decl in [
            ("stream", "TEXT"),
            ("hidden", "INTEGER DEFAULT 0"),
            ("saved", "INTEGER DEFAULT 0"),
            ("opportunity", "REAL DEFAULT 0"),
            ("confidence", "REAL DEFAULT 0"),
            ("risk_tags", "TEXT"),
            ("action_note", "TEXT"),
            ("feedback_reason", "TEXT"),
            ("fingerprint", "TEXT"),
            ("country", "TEXT"),
            ("shipping_cost", "REAL DEFAULT 0"),
            ("shipping_known", "INTEGER DEFAULT 0"),
            ("import_charges", "REAL DEFAULT 0"),
            ("import_charges_known", "INTEGER DEFAULT 0"),
        ]:
            try:
                self._conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        try:
            self._conn.execute("ALTER TABLE listing_observations ADD COLUMN fingerprint TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE listing_observations ADD COLUMN country TEXT")
        except sqlite3.OperationalError:
            pass
        for col, decl in [
            ("shipping_cost", "REAL DEFAULT 0"),
            ("shipping_known", "INTEGER DEFAULT 0"),
            ("import_charges", "REAL DEFAULT 0"),
            ("import_charges_known", "INTEGER DEFAULT 0"),
        ]:
            try:
                self._conn.execute(f"ALTER TABLE listing_observations ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        self._backfill_fingerprints()

    def _backfill_fingerprints(self) -> None:
        rows = self._conn.execute(
            """SELECT item_id, title, seller_username
               FROM listings
               WHERE fingerprint IS NULL OR fingerprint = ''"""
        ).fetchall()
        for row in rows:
            listing = type("FingerprintListing", (), {
                "title": row["title"] or "",
                "seller_username": row["seller_username"] or "",
            })()
            self._conn.execute(
                "UPDATE listings SET fingerprint = ? WHERE item_id = ?",
                (listing_fingerprint(listing), row["item_id"]),
            )

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
                shipping_cost, shipping_known, import_charges, import_charges_known,
                seller_username, seller_pct, seller_score,
                condition, country, url, image_url,
                score, opportunity, confidence, stream, mode, reasons, risk_tags,
                action_note, rejected, reject_reason, fingerprint, hidden, saved,
                feedback_reason, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   COALESCE((SELECT hidden FROM listings WHERE item_id = ?), 0),
                   COALESCE((SELECT saved FROM listings WHERE item_id = ?), 0),
                   COALESCE((SELECT feedback_reason FROM listings WHERE item_id = ?), ''),
                   COALESCE((SELECT first_seen FROM listings WHERE item_id = ?), ?), ?)""",
            (l.item_id, l.search_name, l.title, l.price, l.currency, l.buying_option,
             l.bid_count, getattr(l, "shipping_cost", 0.0), int(getattr(l, "shipping_known", False)),
             getattr(l, "import_charges", 0.0), int(getattr(l, "import_charges_known", False)),
             l.seller_username, l.seller_feedback_pct, l.seller_feedback_score,
             l.condition, l.country, l.url, l.image_url, result.score,
             getattr(result, "opportunity", 0.0), getattr(result, "confidence", 0.0),
             result.stream, result.mode, ", ".join(result.reasons),
             ", ".join(getattr(result, "risk_tags", [])), getattr(result, "action_note", ""),
             int(result.rejected), result.reject_reason,
             listing_fingerprint(l),
             l.item_id, l.item_id, l.item_id, l.item_id, now, now),
        )
        self.record_observation(l, result)
        self._conn.commit()

    def record_observation(self, listing, result=None) -> None:
        now = time.time()
        self._conn.execute(
            """INSERT INTO listing_observations
               (item_id, observed_at, search_name, price, currency, buying_option,
                shipping_cost, shipping_known, import_charges, import_charges_known,
                bid_count, score, stream, country, fingerprint)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (listing.item_id, now, listing.search_name, listing.price, listing.currency,
             listing.buying_option, getattr(listing, "shipping_cost", 0.0),
             int(getattr(listing, "shipping_known", False)),
             getattr(listing, "import_charges", 0.0),
             int(getattr(listing, "import_charges_known", False)), listing.bid_count,
             getattr(result, "score", None), getattr(result, "stream", None),
             getattr(listing, "country", ""),
             listing_fingerprint(listing)),
        )
        self._conn.execute(
            """UPDATE listings
               SET price = ?,
                   shipping_cost = CASE WHEN ? THEN ? ELSE shipping_cost END,
                   shipping_known = CASE WHEN ? THEN 1 ELSE shipping_known END,
                   import_charges = CASE WHEN ? THEN ? ELSE import_charges END,
                   import_charges_known = CASE WHEN ? THEN 1 ELSE import_charges_known END,
                   bid_count = ?,
                   country = COALESCE(NULLIF(?, ''), country),
                   last_seen = ?
               WHERE item_id = ?""",
            (listing.price,
             int(getattr(listing, "shipping_known", False)), getattr(listing, "shipping_cost", 0.0),
             int(getattr(listing, "shipping_known", False)),
             int(getattr(listing, "import_charges_known", False)), getattr(listing, "import_charges", 0.0),
             int(getattr(listing, "import_charges_known", False)),
             listing.bid_count, getattr(listing, "country", ""), now, listing.item_id),
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
        return self._with_relist_groups([dict(r) for r in cur.fetchall()], collapse=True)

    def saved_gems(self, limit: int = 300) -> list[dict]:
        cur = self._conn.execute(
            """SELECT * FROM listings
               WHERE rejected = 0 AND saved = 1 AND hidden = 0
               ORDER BY last_seen DESC LIMIT ?""",
            (limit,))
        return self._with_relist_groups([dict(r) for r in cur.fetchall()], collapse=False)

    def _with_relist_groups(self, rows: list[dict], collapse: bool = False) -> list[dict]:
        decorated = [self._decorate_relist_row(dict(row)) for row in rows]
        if not collapse:
            return decorated
        groups: dict[str, dict] = {}
        order: list[str] = []
        for row in decorated:
            key = row.get("fingerprint") or f"item:{row.get('item_id')}"
            if key not in groups:
                groups[key] = row
                order.append(key)
            elif self._group_rank(row) > self._group_rank(groups[key]):
                groups[key] = row
        return [groups[k] for k in order]

    def _group_rank(self, row: dict) -> tuple:
        score_bucket = int(float(row.get("score") or 0) // 3)
        return (
            int(row.get("saved") or 0),
            score_bucket,
            -float(row.get("price") or 0),
            float(row.get("opportunity") or 0),
            float(row.get("confidence") or 0),
            float(row.get("score") or 0),
            float(row.get("last_seen") or 0),
        )

    def _decorate_relist_row(self, row: dict) -> dict:
        row["relist_count"] = 1
        row["relist_min_price"] = row.get("price") or 0
        row["relist_max_price"] = row.get("price") or 0
        row["relist_best_confidence"] = row.get("confidence") or 0
        row["relist_latest_seen"] = row.get("last_seen") or 0
        row["relist_group_summary"] = ""
        row["relist_group_item_ids"] = row.get("item_id") or ""
        row["is_group_representative"] = 0
        fp = row.get("fingerprint")
        if not fp:
            return row
        summary = self._conn.execute(
            """SELECT COUNT(DISTINCT item_id) AS n,
                      MIN(CASE WHEN price > 0 THEN price END) AS min_price,
                      MAX(price) AS max_price,
                      MAX(confidence) AS best_confidence,
                      MAX(last_seen) AS latest_seen,
                      GROUP_CONCAT(item_id) AS item_ids
               FROM listings
               WHERE rejected = 0 AND hidden = 0 AND fingerprint = ?""",
            (fp,),
        ).fetchone()
        n = int(summary["n"] or 1)
        row["relist_count"] = n
        row["relist_min_price"] = summary["min_price"] or row.get("price") or 0
        row["relist_max_price"] = summary["max_price"] or row.get("price") or 0
        row["relist_best_confidence"] = summary["best_confidence"] or row.get("confidence") or 0
        row["relist_latest_seen"] = summary["latest_seen"] or row.get("last_seen") or 0
        row["relist_group_item_ids"] = summary["item_ids"] or row.get("item_id") or ""
        row["is_group_representative"] = 1 if n > 1 else 0
        if n > 1:
            row["relist_group_summary"] = (
                f"{n} similar listings · cheapest ${row['relist_min_price']:,.0f} · "
                f"best confidence {row['relist_best_confidence']:.0f}"
            )
        return row

    def _with_relist_counts(self, rows: list[dict]) -> list[dict]:
        """Backward-compatible alias for older call sites."""
        return self._with_relist_groups(rows, collapse=False)

    def feedback_rows(self, limit: int = 500) -> list[dict]:
        cur = self._conn.execute(
            """SELECT item_id, title, search_name, stream, reasons, feedback_reason, saved, hidden
               FROM listings
               WHERE rejected = 0 AND (saved = 1 OR hidden = 1)
               ORDER BY last_seen DESC LIMIT ?""",
            (limit,))
        return [dict(r) for r in cur.fetchall()]

    def inspect_now(self, per_section: int = 3) -> list[dict]:
        sections = [
            ("rare", "Rare radar", "stream = 'rare'", "opportunity DESC, confidence DESC"),
            ("repair", "Best repair projects", "stream = 'repair'", "opportunity DESC, score DESC"),
            ("safe", "Safe collector buys",
             "stream IN ('rolex','patek','iwc','taste') AND confidence >= 70",
             "opportunity DESC, confidence DESC"),
            ("chrono", "Chronos worth inspecting", "stream = 'chrono'", "opportunity DESC, score DESC"),
            ("relist", "Possible relists",
             "fingerprint IS NOT NULL AND fingerprint IN (SELECT fingerprint FROM listings WHERE fingerprint IS NOT NULL GROUP BY fingerprint HAVING COUNT(DISTINCT item_id) > 1)",
             "last_seen DESC"),
        ]
        out = []
        for section_id, label, where, ordering in sections:
            rows = self._conn.execute(
                f"""SELECT * FROM listings
                    WHERE rejected = 0 AND hidden = 0 AND {where}
                    ORDER BY {ordering}
                    LIMIT ?""",
                (per_section,),
            ).fetchall()
            rows = self._with_relist_groups([dict(r) for r in rows], collapse=True)
            if rows:
                out.append({"id": section_id, "label": label, "items": rows})
        return out

    def set_saved(self, item_id: str, saved: bool) -> bool:
        cur = self._conn.execute(
            "UPDATE listings SET saved = ? WHERE item_id = ?", (int(saved), item_id))
        self._conn.commit()
        return cur.rowcount > 0

    def set_hidden(self, item_id: str, hidden: bool, reason: str = "") -> bool:
        cur = self._conn.execute(
            "UPDATE listings SET hidden = ?, feedback_reason = ? WHERE item_id = ?",
            (int(hidden), reason, item_id))
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
