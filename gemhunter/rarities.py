"""National Rarities — one consignment seller's weekly auction drop.

turnaroundrarities (`nationalrarities`) consigns a few hundred watches a week,
nearly all of them ending Sunday evening. Their Rolex sells for full money; the
90s-onward IWC and Breitling is where the value hides, which is what the taste
ordering here encodes.

Two things use this module: the /rarities tab in the web app, which shows the
whole week ranked, and the new-drop digest below, which pushes one notification
when fresh lots appear.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone

from .config import load_config
from .ebay import EbayClient
from .knowledge import (CHRONO_KEYWORDS, COLLECTOR_TARGETS, IWC_TARGETS,
                        PROJECT_KEYWORDS, QUARTZ_MODELS, TASTE_BRANDS,
                        VALUED_CALIBERS)
from .models import Listing
from .notify import Notifier
from .storage import Storage

try:
    from zoneinfo import ZoneInfo
    MOUNTAIN = ZoneInfo("America/Denver")
except Exception:
    MOUNTAIN = None

SELLER = "nationalrarities"
STORE_URL = "https://www.ebay.com/str/turnaroundrarities"

# Their Rolex goes for full money; the 90s–2020s IWC and Breitling is where the
# value hides, so those two brands outrank everything — Rolex included.
BREITLING_TARGETS = ["navitimer", "chronomat", "cosmonaute", "aerospace",
                     "superocean", "avenger", "emergency", "montbrillant",
                     "a23322", "b01", "top time", "premier"]
DOWNRANK = ["quartz", "ladies", "lady's", "ladys", "women", "girls",
            "pocket watch", "smartwatch", "smart watch", "apple watch"]
# They also auction loose bracelets, straps, and bezels under the same brands.
# Those never say "watch" in the title, so an accessory word without it means
# the picture is a strap — sort it under every actual watch.
ACCESSORY_WORDS = ["bracelet", "strap", "band", "buckle", "clasp", "bezel",
                   "links", "case back"]

# The digest leads with these two and summarises the rest: IWC in any form, and
# the Navitimer family specifically rather than all Breitling.
NAVITIMER_WORDS = ["navitimer", "cosmonaute", "montbrillant"]


def _is_iwc(t: str) -> bool:
    return "iwc" in t or "schaffhausen" in t


def taste(title: str) -> float:
    t = " " + (title or "").lower() + " "
    score = 0.0
    if _is_iwc(t):
        score += 40
        if any(k in t for k in IWC_TARGETS):
            score += 10
    elif "breitling" in t:
        score += 36
        if any(k in t for k in BREITLING_TARGETS):
            score += 8
    elif "rolex" in t or "tudor" in t:
        score += 12
    elif any(b in t for b in TASTE_BRANDS):
        score += 20
    if any(k in t for k in CHRONO_KEYWORDS):
        score += 6
    for cal, (pts, column) in VALUED_CALIBERS.items():
        if cal in t:
            score += pts * 2 + (4 if column else 0)
            break
    if any(k in t for k in COLLECTOR_TARGETS):
        score += 4
    # "Needs a little work" is the whole reason to shop this seller.
    if any(k in t for k in PROJECT_KEYWORDS):
        score += 4
    if any(k in t for k in DOWNRANK) or any(k in t for k in QUARTZ_MODELS):
        score -= 30
    if "watch" not in t and any(k in t for k in ACCESSORY_WORDS):
        score -= 40
    return score


def priority_tag(title: str):
    """'IWC' / 'Navitimer' for the lots that lead the digest, else None."""
    t = " " + (title or "").lower() + " "
    if any(k in t for k in DOWNRANK) or any(k in t for k in QUARTZ_MODELS):
        return None
    if "watch" not in t and any(k in t for k in ACCESSORY_WORDS):
        return None
    if _is_iwc(t):
        return "IWC"
    if "breitling" in t and any(k in t for k in NAVITIMER_WORDS):
        return "Navitimer"
    return None


def item_dict(listing: Listing) -> dict:
    return {
        "id": listing.item_id,
        "title": listing.title,
        "url": listing.url,
        # Search hands back a thumbnail URL; the size lives in the filename,
        # so rewrite it for a picture-first scroll (s-l800 ≈ 60–120 KB).
        "image": re.sub(r"s-l\d+", "s-l800", listing.image_url)
                 if listing.image_url else "",
        "bid": listing.price,
        "bids": listing.bid_count,
        "ends": listing.item_end_date,
        "for_parts": "parts" in (listing.condition or "").lower(),
        "taste": taste(listing.title),
    }


def fetch(client: EbayClient) -> list[dict]:
    """Every auction they have running now, best-first."""
    items = [item_dict(l) for l in client.seller_auctions(SELLER) if l.active]
    items.sort(key=lambda r: (-r["taste"], r["ends"] or "9999"))
    return items


# ---------------------------------------------------------------------------
# The new-drop digest. Run it on a timer; it only pushes when lots it has never
# seen turn up, so a run mid-week that finds nothing new sends nothing at all.
# ---------------------------------------------------------------------------

def _money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "$?"


def _ends_label(iso: str) -> str:
    """'Sun 7:06 PM' local — the day is the fact that matters here.

    eBay hands back UTC, where a Sunday-evening close reads as Monday. Falls
    back to the machine's own zone when tzdata is missing (Windows dev boxes),
    which is still right at home and never leaves the time in UTC.
    """
    if not iso:
        return ""
    try:
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S") \
                     .replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    dt = dt.astimezone(MOUNTAIN) if MOUNTAIN else dt.astimezone()
    hour12 = dt.hour % 12 or 12
    return f"{dt.strftime('%a')} {hour12}:{dt.minute:02d} " \
           f"{'AM' if dt.hour < 12 else 'PM'}"


def _brand_of(title: str) -> str:
    t = " " + (title or "").lower() + " "
    if _is_iwc(t):
        return "IWC"
    for name in ("breitling", "rolex", "omega", "tudor", "heuer", "longines",
                 "cartier", "seiko", "movado", "zenith", "universal"):
        if name in t:
            return name.title()
    return "other"


def _line(item: dict) -> str:
    bids = f"{item['bids']} bid{'s' if item['bids'] != 1 else ''}" \
        if item.get("bids") else "no bids"
    ends = _ends_label(item.get("ends", ""))
    return f"{item['title'][:70]}\n  {_money(item['bid'])} · {bids}" \
           + (f" · ends {ends}" if ends else "")


def build_digest(new_items: list[dict], lead: int = 6) -> tuple[str, str]:
    """(title, message) for Pushover. Leads with IWC and Navitimer."""
    leads = [i for i in new_items if priority_tag(i["title"])]
    leads.sort(key=lambda r: -r["taste"])
    rest = [i for i in new_items if not priority_tag(i["title"])]

    counts: dict[str, int] = {}
    for item in leads:
        tag = priority_tag(item["title"])
        counts[tag] = counts.get(tag, 0) + 1
    headline = " · ".join(f"{n} {tag}" for tag, n in sorted(counts.items())) \
        or "nothing in your lanes"
    title = f"National Rarities: {len(new_items)} new · {headline}"

    body = [_line(i) for i in leads[:lead]]
    if len(leads) > lead:
        body.append(f"+{len(leads) - lead} more IWC/Navitimer in the app")
    if rest:
        # The rest is a headcount, not a list — 400 lots will not fit a phone.
        by_brand: dict[str, int] = {}
        for item in rest:
            by_brand[_brand_of(item["title"])] = \
                by_brand.get(_brand_of(item["title"]), 0) + 1
        top = sorted(by_brand.items(), key=lambda kv: -kv[1])[:5]
        body.append("\nAlso new: " + ", ".join(f"{n} {b}" for b, n in top))
    return title, "\n".join(body)


def run_digest(db_path: str, dry_run: bool = False, lead: int = 6) -> int:
    """Fetch, diff against what's been seen, push a digest. Returns new count."""
    cfg = load_config()
    if not cfg.has_ebay_keys:
        raise RuntimeError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set")
    client = EbayClient(cfg.ebay_client_id, cfg.ebay_client_secret,
                        cfg.marketplace, cfg.buyer_country, cfg.buyer_postal_code)
    items = fetch(client)
    storage = Storage(db_path)
    try:
        # First ever run sees several hundred "new" lots, which is not news —
        # seed the table quietly and let the next run report real arrivals.
        seeding = not storage.has_rarities_history()
        new_items = storage.record_rarities(items)
    finally:
        storage.close()

    if seeding:
        print(f"[rarities] seeded {len(items)} current lots — "
              f"the next run reports what's new since.")
        return 0
    if not new_items:
        print(f"[rarities] {len(items)} lots live, none new since last run.")
        return 0

    title, message = build_digest(new_items, lead)
    if dry_run:
        print(f"[rarities] {title}\n{message}")
        return len(new_items)
    notifier = Notifier(cfg.pushover_user_key, cfg.pushover_api_token)
    notifier.send_digest(title, message, STORE_URL)
    return len(new_items)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="National Rarities new-drop digest")
    parser.add_argument("--db", default="gemhunter.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the digest instead of pushing it")
    parser.add_argument("--lead", type=int, default=6,
                        help="how many IWC/Navitimer lots to name (default 6)")
    args = parser.parse_args()
    count = run_digest(args.db, args.dry_run, args.lead)
    print(f"[rarities] {count} new lot(s).")


if __name__ == "__main__":
    main()
