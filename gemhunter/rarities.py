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
import threading
from concurrent.futures import ThreadPoolExecutor
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

# ---------------------------------------------------------------------------
# Hard exclusions — these never reach the page at all. A third of any given
# week is fashion and mid-century American filler (measured 2026-08-06: 61
# Bulova, 41 Seiko, 20 Elgin, 12 Gucci, 9 Tissot, 8 Wittnauer out of 420), and
# scrolling past it is the whole problem this tab exists to solve.
# ---------------------------------------------------------------------------
BLOCKED_BRANDS = ["seiko", "tissot", "bulova", "gucci", "elgin", "wittnauer",
                  "accutron"]
BLOCKED_WORDS = ["quartz", "ladies", "lady's", "ladys", "women's", "womens",
                 "girls", "pocket watch", "smartwatch", "smart watch",
                 "apple watch", "tuning fork"]

# Their Breitling is mostly Navitimer family; those lead, the rest follow.
BREITLING_TARGETS = ["navitimer", "chronomat", "cosmonaute", "aerospace",
                     "superocean", "avenger", "emergency", "montbrillant",
                     "a23322", "b01", "top time", "premier"]
NAVITIMER_WORDS = ["navitimer", "cosmonaute", "montbrillant"]

# The two Breitling complications he watches for. "perpetual" alone is no use
# as a signal — Rolex puts it on every Oyster Perpetual — so the calendar has
# to be named, or Breitling's own "Quantième" spelling.
PERPETUAL_WORDS = ["perpetual calendar", "quantieme", "quantième", "qp chrono"]
RATTRAPANTE_WORDS = ["rattrapante", "split second", "split-second",
                     "splitsecond", "doppelchrono"]
LIMITED_WORDS = ["limited", " ltd", "one of ", "1 of "]
# Solid precious metal only. Plated, filled, and two-tone are the opposite
# signal, so they veto the match rather than merely failing to add to it.
PRECIOUS_WORDS = ["platinum", "18k", "18kt", "18 k", "14k", "14kt",
                  "yellow gold", "rose gold", "white gold", "pink gold",
                  "solid gold"]
NOT_PRECIOUS = ["plated", "gold filled", "gold-filled", " gf ", " gp ",
                "gold tone", "gold-tone", "two-tone", "two tone", "10k gf"]

# Rolex sport/tool references, as opposed to Datejust/Day-Date/Cellini dress.
ROLEX_SPORTS = ["submariner", "gmt-master", "gmt master", "gmt", "daytona",
                "cosmograph", "explorer", "sea-dweller", "sea dweller",
                "yacht-master", "yachtmaster", "milgauss", "turn-o-graph",
                "air king", "air-king"]
# The Oyster Perpetual line straddles it: the tool-watch base, but also every
# vintage dress Rolex (Bubble Back, 6634). Enough to reach the project tier
# when it needs work, not enough to hold a 1940s dress piece near the top.
ROLEX_OYSTER = ["oyster perpetual", "oyster-perpetual", "oyster"]

# They auction loose bracelets, dials, and bezels under the same brand names.
# The word "watch" can't distinguish them — "Rubber Strap Watch" is a watch,
# "Watch Bracelet" is a bracelet — but word order can: whichever noun comes
# last is what is actually being sold.
ACCESSORY_WORDS = ["bracelet", "strap", "band", "buckle", "clasp", "bezel",
                   "links", "case back"]
# Components are named in almost every title as descriptors ("Silver Dial
# Watch"), so they only mean a parts listing when the lot is one — hence the
# "lot of" qualifier rather than the word-order test.
PART_WORDS = ["dial", "hands", "crown", "crystal", "movement", "parts", "case"]
W_ACCESSORY = -70.0        # a part, not a watch: below everything but old Omega

# Model names that date themselves, for the titles that carry no year, no
# decade, and no reference number.
VINTAGE_MODELS = ["bubble back", "bubbleback", "bumper", "ovettone"]

# ---- Tier bases. The order of these is the answer to "what do I want to see
# first", and everything else sorts underneath them. ----
T_BREITLING_GRAIL = 118.0  # Breitling perpetual calendar chrono / rattrapante
T_NAVI_SPECIAL = 112.0     # Navitimer in precious metal, or a limited edition
T_IWC_MODERN = 100.0       # IWC, 1980s onward
T_NAVITIMER = 92.0         # Breitling Navitimer family
T_ROLEX_PROJECT = 85.0     # Rolex sports that needs work — his bench, his edge
T_ROLEX_SPORTS = 45.0      # same models, nothing wrong with them
T_BREITLING = 42.0
T_TUDOR = 38.0
T_TASTE = 30.0             # any other brand he collects
T_ROLEX_DRESS = 25.0       # Datejust, Day-Date, Cellini
T_OMEGA = -40.0            # bottom by request, however nice
W_PRE_1980 = -60.0         # bottom by request, unless it's one of his lanes
W_SIZE_OK = 8.0            # 36mm and up: wearable
# Deep enough that a 34mm project loses to a clean 40mm of the same model:
# an unwearable case is a worse problem than a movement he can fix himself.
W_SIZE_SMALL = -35.0
SIZE_MIN = 36.0
_MM_RE = re.compile(r"(\d{2}(?:\.\d)?)\s*mm", re.I)

# Pre-1980 is rarely stated as a year (2 of 420 titles carried one), so it is
# inferred: the seller's own "vintage", an explicit old year or decade, and —
# for Rolex only — reference length, where 4 digits means pre-80s, 5 means
# 1977 onward, 6 means 2000s. That digit rule is Rolex's alone; IWC's 4-digit
# refs (3706, 3253, 3713) are exactly the 80s-onward pieces he hunts.
_OLD_YEAR_RE = re.compile(r"\b(19[0-7]\d)\b")
_OLD_DECADE_RE = re.compile(r"(?i)\b(?:19)?([2-7]0)'?s\b")
_ROLEX_REF_RE = re.compile(r"(?i)ref\.?\s*#?\s*(\d{4,6})")


def _is_iwc(t: str) -> bool:
    return "iwc" in t or "schaffhausen" in t


def excluded(title: str):
    """Reason this lot should never be shown, or None to keep it."""
    t = " " + (title or "").lower() + " "
    for brand in BLOCKED_BRANDS:
        if brand in t:
            return brand
    for word in BLOCKED_WORDS:
        if word in t:
            return word
    for model in QUARTZ_MODELS:
        if model in t:
            return "quartz model"
    return None


def is_accessory(t: str) -> bool:
    """True when a part, not a watch, is the thing being sold.

    Decided by word order rather than presence: "Rubber Strap Watch" is a
    watch, "Watch Bracelet" is a bracelet, and "Navitimer Bracelet" names no
    watch at all. "Watch Head" stays a watch — a head is the watch itself,
    minus its bracelet, and is exactly what he buys to rebuild.
    """
    last_watch = t.rfind("watch")
    for word in ACCESSORY_WORDS:
        at = t.rfind(word)
        if at >= 0 and at > last_watch:
            return True
    if "lot of" in t and any(w in t for w in PART_WORDS + ACCESSORY_WORDS):
        return True
    return False


def case_mm(t: str):
    """Case diameter off the title — they state it on nearly every lot."""
    for raw in _MM_RE.findall(t):
        try:
            mm = float(raw)
        except ValueError:
            continue
        if 20 <= mm <= 60:          # a plausible wristwatch, not a lug width
            return mm
    return None


def _is_precious(t: str) -> bool:
    if any(k in t for k in NOT_PRECIOUS):
        return False
    return any(k in t for k in PRECIOUS_WORDS)


def looks_pre_1980(t: str) -> bool:
    if _OLD_YEAR_RE.search(t) or _OLD_DECADE_RE.search(t):
        return True
    if any(m in t for m in VINTAGE_MODELS):
        return True
    if "rolex" in t:
        found = _ROLEX_REF_RE.search(t)
        if found and len(found.group(1)) == 4:
            return True
    return "vintage" in t


def taste(title: str) -> float:
    """Tier first, then the usual modifiers. Tier order is the whole point."""
    t = " " + (title or "").lower() + " "
    # A part is never a lot he is shopping for here, whatever brand is on it.
    if is_accessory(t):
        return W_ACCESSORY
    is_rolex = "rolex" in t
    core_sports = is_rolex and any(k in t for k in ROLEX_SPORTS)
    rolex_sports = core_sports or (is_rolex and any(k in t for k in ROLEX_OYSTER))
    project = any(k in t for k in PROJECT_KEYWORDS)

    breitling = "breitling" in t
    navitimer = breitling and any(k in t for k in NAVITIMER_WORDS)
    grail = breitling and (any(k in t for k in PERPETUAL_WORDS)
                           or any(k in t for k in RATTRAPANTE_WORDS))

    if grail:
        score, protected = T_BREITLING_GRAIL, True
    elif navitimer and (_is_precious(t) or any(k in t for k in LIMITED_WORDS)):
        score, protected = T_NAVI_SPECIAL, True
    elif _is_iwc(t) and not looks_pre_1980(t):
        score, protected = T_IWC_MODERN, True
        if any(k in t for k in IWC_TARGETS):
            score += 10
    elif navitimer:
        score, protected = T_NAVITIMER, True
    elif rolex_sports and project:
        score, protected = T_ROLEX_PROJECT, True
    elif rolex_sports:
        # Not a project, but still his lane. Only the named sport models are
        # held above the age rule; a vintage Oyster dress piece sinks.
        score, protected = T_ROLEX_SPORTS, core_sports
    elif "omega" in t:
        score, protected = T_OMEGA, False
    elif breitling:
        score, protected = T_BREITLING, False
        if any(k in t for k in BREITLING_TARGETS):
            score += 6
    elif "tudor" in t:
        score, protected = T_TUDOR, False
    elif "rolex" in t:
        score, protected = T_ROLEX_DRESS, False
    elif _is_iwc(t):                      # pre-80s IWC: wanted, but not a lead
        score, protected = T_TASTE, False
    elif any(b in t for b in TASTE_BRANDS):
        score, protected = T_TASTE, False
    else:
        score, protected = 0.0, False

    if any(k in t for k in CHRONO_KEYWORDS):
        score += 6
    for cal, (pts, column) in VALUED_CALIBERS.items():
        if cal in t:
            score += pts * 2 + (4 if column else 0)
            break
    if any(k in t for k in COLLECTOR_TARGETS):
        score += 4
    # "Needs a little work" is the whole reason to shop this seller.
    if project:
        score += 4
    if not protected and looks_pre_1980(t):
        score += W_PRE_1980
    mm = case_mm(t)
    if mm is not None:
        score += W_SIZE_OK if mm >= SIZE_MIN else W_SIZE_SMALL
    return score


def priority_tag(title: str):
    """The lanes that lead the digest, or None for everything else."""
    if excluded(title):
        return None
    t = " " + (title or "").lower() + " "
    if is_accessory(t):
        return None
    if "breitling" in t and (any(k in t for k in PERPETUAL_WORDS)
                             or any(k in t for k in RATTRAPANTE_WORDS)):
        return "Breitling grail"
    if _is_iwc(t) and not looks_pre_1980(t):
        return "IWC"
    if "breitling" in t and any(k in t for k in NAVITIMER_WORDS):
        return "Navitimer"
    if "rolex" in t and any(k in t for k in PROJECT_KEYWORDS) \
            and any(k in t for k in ROLEX_SPORTS + ROLEX_OYSTER):
        return "Rolex project"
    return None


# ---------------------------------------------------------------------------
# What the Condition Description actually says. eBay's own condition field is
# "Pre-owned - Good" on almost everything they list, including watches their
# own description calls non-running, so the description is the only honest
# read of what you would be bidding on.
# ---------------------------------------------------------------------------

# Every listing ends with the same disclaimer — that it will "likely require a
# service", that cases are "assumed to have been polished", that nothing was
# "tested for accuracy". Flagging boilerplate would mark all 200-odd lots
# identically, so everything from that sentence on is cut before matching.
_BOILERPLATE_RE = re.compile(
    r"(?i)\bas an estate watch\b|\bdue to unknown service history\b")

# (pattern, chip label, severity). Order matters: first match per label wins,
# and the page shows them in this order, so the deal-breakers come first.
CONDITION_FLAGS = [
    (r"currently non-?running|not currently running|\bnot running\b"
     r"|does\s*n[o']?t run", "NOT RUNNING", "bad"),
    (r"sold as-?is for parts|for parts or repair", "FOR PARTS", "bad"),
    (r"functions? (?:have been tested and are|are) non-?working"
     r"|functions? (?:are|is) not working", "FUNCTIONS DEAD", "bad"),
    (r"does\s*n[o']?t wind|will not wind|cannot be wound", "WON'T WIND", "bad"),
    (r"cannot be set|can\s*n[o']?t be set|does\s*n[o']?t set", "WON'T SET", "bad"),
    # On a vintage Rolex this is most of the value gone, so it reads as loudly
    # as a dead movement.
    (r"dial is aftermarket|aftermarket dial|re-?dial|refinished dial",
     "AFTERMARKET DIAL", "bad"),
    (r"lume .{0,40}re-?applied|re-?lumed|lume has been", "RELUMED", "bad"),
    (r"aftermarket|is a replacement|has been replaced", "REPLACED PART", "bad"),
    (r"\bis missing\b|missing its|missing the", "MISSING PART", "bad"),
    (r"movement .{0,30}loose|loose rotor|rattling inside", "LOOSE MOVEMENT", "bad"),
    (r"chronograph .{0,40}not working|pushers? .{0,30}not working"
     r"|pushers? are loose", "CHRONO FAULT", "bad"),
    (r"crystal .{0,20}crack|cracked|chipped", "CRACKED", "bad"),
    (r"\brust\b|corrosion|water damage", "RUST / WATER", "bad"),
    (r"crown is loose|crown is not screwing|crown .{0,20}stripped",
     "CROWN FAULT", "warn"),
    (r"bezel is seized|will not rotate|difficulty rotating"
     r"|difficult to rotate", "BEZEL STUCK", "warn"),
    (r"considerable scratches|heavy scratches|significant wear|deep(?:er)? scratch",
     "HEAVY WEAR", "warn"),
    (r"stretch(?:ed)? bracelet|bracelet .{0,20}stretch", "STRETCHED", "warn"),
    (r"currently running", "RUNNING", "good"),
]
CONDITION_FLAGS = [(re.compile(p, re.I), label, sev)
                   for p, label, sev in CONDITION_FLAGS]


def condition_flags(text: str) -> list[dict]:
    """Chips for the card, worst first, off the seller's own description."""
    if not text:
        return []
    cut = _BOILERPLATE_RE.search(text)
    body = text[:cut.start()] if cut else text
    out, seen = [], set()
    for rx, label, sev in CONDITION_FLAGS:
        if label not in seen and rx.search(body):
            out.append({"label": label, "sev": sev})
            seen.add(label)
    # "Running" next to a fault is noise; the fault is the news.
    if any(f["sev"] == "bad" for f in out):
        out = [f for f in out if f["label"] != "RUNNING"]
    return out


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
        "mm": case_mm(" " + (listing.title or "").lower() + " "),
        "taste": taste(listing.title),
        "flags": None,          # filled in by the detail pass, if it has run
    }


def fetch(client: EbayClient) -> list[dict]:
    """Every auction worth his eye, best-first. Blocked brands never appear."""
    items = [item_dict(l) for l in client.seller_auctions(SELLER)
             if l.active and not excluded(l.title)]
    items.sort(key=lambda r: (-r["taste"], r["ends"] or "9999"))
    return items


# A listing's condition text never changes, so once fetched it is kept for the
# life of the process: a week of browsing costs one call per lot, not one per
# page view. Only the top slice is ever fetched — nobody reads to lot 180.
_details_lock = threading.Lock()
_details: dict[str, list] = {}
_details_running = False


def _load_detail(client: EbayClient, item_id: str) -> list:
    try:
        d = client.get_item(item_id)
    except Exception:
        return []
    return condition_flags(d.get("conditionDescription") or "")


def apply_details(items: list[dict]) -> int:
    """Attach known flags; return how many of these are still unfetched."""
    with _details_lock:
        pending = 0
        for item in items:
            if item["id"] in _details:
                item["flags"] = _details[item["id"]]
            else:
                pending += 1
        return pending


def start_detail_pass(client: EbayClient, items: list[dict], top: int = 60,
                      workers: int = 6) -> None:
    """Fetch condition text for the top lots in the background.

    Kept off the request path deliberately: sixty sequential item lookups is
    twenty seconds, and the photos are worth showing long before the chips are.
    """
    global _details_running
    with _details_lock:
        if _details_running:
            return
        todo = [i["id"] for i in items[:top] if i["id"] not in _details]
        if not todo:
            return
        _details_running = True

    def work():
        global _details_running
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                done = list(pool.map(lambda i: (i, _load_detail(client, i)), todo))
            with _details_lock:
                _details.update(dict(done))
        finally:
            with _details_lock:
                _details_running = False

    threading.Thread(target=work, daemon=True).start()


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
