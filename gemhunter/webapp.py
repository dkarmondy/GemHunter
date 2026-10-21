"""Private mobile web app for GemHunter: Listing lookup and National Rarities.

The Pi serves this over Tailscale. No framework, no build step, no public
surface area: a small JSON API, a few mobile pages, and SQLite for the
Rarities votes.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

from . import authguard
from .authguard import strip_html as _strip_html
from .config import load_config
from .ebay import EbayClient
from .rarities import SELLER as RARITIES_SELLER
from .rarities import STORE_URL as RARITIES_STORE_URL
from .rarities import apply_details, start_detail_pass
from .rarities import fetch as fetch_seller_auctions
from .storage import Storage

try:
    from zoneinfo import ZoneInfo
    MOUNTAIN = ZoneInfo("America/Denver")
except Exception:
    MOUNTAIN = None


TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{2,}")
STOP = {
    "watch", "watches", "mens", "men", "with", "and", "the", "for", "from",
    "dial", "case", "date", "automatic", "manual", "vintage", "pre", "owned",
    "papers", "paper", "box", "full", "set", "stainless", "steel", "gold",
}


def _now_str() -> str:
    now = datetime.now(MOUNTAIN) if MOUNTAIN else datetime.now().astimezone()
    hour12 = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    return f"{now.month}/{now.day}/{now.year} {hour12}:{now.minute:02d} {ampm} {now.strftime('%Z')}"


def _tokens(text: str) -> set[str]:
    return {t for t in TOKEN_RE.findall((text or "").lower()) if t not in STOP}


# ---------------------------------------------------------------------------
# Live eBay lookup — one listing, straight from the Browse API on demand.
# ---------------------------------------------------------------------------

_ebay_lock = threading.Lock()
_ebay_client = None


def ebay_client() -> EbayClient:
    """Built once, lazily: the app must still boot with no keys configured."""
    global _ebay_client
    with _ebay_lock:
        if _ebay_client is None:
            cfg = load_config()
            if not cfg.has_ebay_keys:
                raise RuntimeError(
                    "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set — "
                    "check .env in the service's working directory")
            _ebay_client = EbayClient(
                cfg.ebay_client_id, cfg.ebay_client_secret,
                cfg.marketplace, cfg.buyer_country, cfg.buyer_postal_code)
        return _ebay_client


BROWSE_ID_RE = re.compile(r"v1\|\d+\|\d+")
# The number a human can copy off a listing page or out of a URL. Handles
# /itm/1234, /itm/some-title-slug/1234, ?item=1234, and a bare number.
LEGACY_ID_RE = re.compile(
    r"(?:/itm/(?:[^/?#]+/)?|[?&](?:item|iid)=|^)(\d{9,15})(?!\d)")

# Redirects are only ever followed within eBay, so a share link can't be used
# to make this Pi fetch an arbitrary address.
EBAY_HOSTS = ("ebay.io", "ebay.us", "ebay.com", "ebay.co.uk", "ebay.de",
              "ebay.fr", "ebay.it", "ebay.es", "ebay.ca", "ebay.com.au")


def _is_ebay_host(netloc: str) -> bool:
    host = netloc.lower().split("@")[-1].split(":")[0]
    return any(host == h or host.endswith("." + h) for h in EBAY_HOSTS)


def _find_id(text: str):
    found = BROWSE_ID_RE.search(text)
    if found:
        return "browse", found.group(0)
    found = LEGACY_ID_RE.search(text)
    if found:
        return "legacy", found.group(1)
    return None


def resolve_ebay_link(url: str, hops: int = 5) -> str:
    """Follow an eBay share link far enough to expose the item id.

    The share button on the eBay app hands out ebay.io/m/xxxx, which carries no
    id at all. Only Location headers are read — the listing page itself 403s
    for non-browser clients and its body is never needed.

    Must be GET, not HEAD: eBay's short-link service answers HEAD with a 302 to
    /n/error and only reveals the real target to a GET. stream=True keeps the
    body off the wire, which is the reason HEAD looked attractive to begin with.
    """
    current = url
    for _ in range(hops):
        parsed = urlparse(current)
        if not _is_ebay_host(parsed.netloc):
            raise ValueError("that link doesn't point at eBay")
        try:
            resp = requests.get(current, allow_redirects=False, timeout=10,
                                stream=True)
            location = resp.headers.get("Location")
            resp.close()
        except requests.RequestException as exc:
            raise ValueError(f"couldn't follow that link ({exc})") from exc
        if not location:
            return current
        current = (location if "://" in location
                   else f"{parsed.scheme}://{parsed.netloc}{location}")
        if _find_id(current):
            return current
    return current


def parse_item_ref(raw: str) -> tuple[str, str]:
    """('browse'|'legacy', id) from a Browse id, a bare number, or any eBay URL."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("pass ?id= a listing id, an item number, or an eBay link")
    found = _find_id(raw)
    if found:
        return found
    if raw.lower().startswith(("http://", "https://")):
        resolved = resolve_ebay_link(raw)
        found = _find_id(resolved)
        if found:
            return found
        raise ValueError("that's an eBay link, but not to a single listing"
                         if resolved == raw else
                         "that link redirected but never revealed a listing id")
    raise ValueError("no listing id found in that text")


# Aspect names vary by seller ("Case Size", "Case Diameter", "Dial Diameter"),
# so promote by pattern rather than exact key. First match wins, and the
# specific patterns are listed before the loose ones.
SPEC_PATTERNS = [
    ("Case size", r"case\s*(size|diameter)|dial\s*diameter|watch\s*size"),
    ("Band/lug width", r"(band|strap|lug|bracelet)\s*(width|size)"),
    ("Thickness", r"thick|depth"),
    ("Weight", r"weight"),
    ("Movement", r"movement|calib(er|re)"),
]

# Sentences in the description that mention a dimension worth reading.
SPEC_HINT_RE = re.compile(
    r"(?i)(thick|depth|weigh|\blug\b|band width|strap width|case size|"
    r"case diameter|diameter|\d+\s?mm\b|\d+(\.\d+)?\s?(g|gram|grams|oz)\b)")


def _key_specs(aspects: dict) -> tuple[list, list]:
    """(promoted [label, value] pairs, aspect names consumed)."""
    promoted, used = [], []
    for label, pattern in SPEC_PATTERNS:
        rx = re.compile(pattern, re.I)
        for name, value in aspects.items():
            if name in used or not rx.search(name or ""):
                continue
            promoted.append([label, value])
            used.append(name)
            break
    return promoted, used


# What the seller says is wrong with it. On a for-parts watch this decides
# whether it's an afternoon at the bench or a donor, and it only ever appears
# in prose — no aspect or API field carries it.
FAULT_HINT_RE = re.compile(
    r"(?i)\b(overwound|over-wound|not running|does ?n[o']t run|won'?t run|frozen|"
    r"seized|stuck|rust|corrosion|pitted|scratch|dent|crack|chip|hairline|"
    r"missing|broken|bent|replaced|non-?original|redial|refinish|water damage|"
    r"needs? (a )?(service|cleaning|repair)|as[- ]is|for parts|does ?n[o']t "
    r"(advance|set|wind|keep)|mainspring|balance staff|stem)\b")


def _match_notes(text: str, pattern, limit: int = 6) -> list:
    """Sentences from a description matching a keyword pattern."""
    notes = []
    for chunk in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        line = chunk.strip()
        if 8 <= len(line) <= 240 and pattern.search(line):
            notes.append(line)
            if len(notes) >= limit:
                break
    return notes


def _spec_notes(text: str, limit: int = 6) -> list:
    """Lines from the description that mention size, thickness or weight."""
    return _match_notes(text, SPEC_HINT_RE, limit)


def _fault_notes(text: str, limit: int = 8) -> list:
    return _match_notes(text, FAULT_HINT_RE, limit)


# "Ref. 6606A-1127-55B", "Reference: 145.022", "ref no. 6239" — capture stops
# at the first comma/semicolon/newline so trailing prose stays out.
# The prefix matches any case, but the ref itself must be uppercase/digits —
# with (?i) everywhere, "no reference here" captured "here". A following
# lowercase word ends the ref, so "145.022 from 1969" stops at "145.022".
_REF_RE = re.compile(
    r"(?i:\bref(?:erence)?\.?\s*(?:number|no\.?|#)?\s*[:.\-]?)\s*"
    r"([A-Z0-9][A-Z0-9\-/\. ]{1,24}?)(?=\s*[,;)\n]|\s+[a-z]|$)")

# Year with context ("circa 1965", "Ca. 1950s", "manufactured 1972") beats a
# bare year, which beats nothing — descriptions are full of stray numbers.
_YEAR_CTX_RE = re.compile(
    r"(?i)(?:circa|ca\.?|c\.|year|manufactured|made|produced|dates?\s+(?:to|from)|from)"
    r"\s*[:\-]?\s*((?:19[0-9]{2}|20[0-2][0-9])(?:'?s)?)")
_YEAR_ANY_RE = re.compile(r"\b((?:19[0-9]{2}|20[0-2][0-9])(?:'?s)?)\b")


def _model_ref(aspects: dict, text: str):
    """(model, reference) from item specifics first, description as fallback."""
    model = ref = None
    for name, value in aspects.items():
        low = (name or "").lower()
        if model is None and "model" in low and "year" not in low:
            model = value
        if ref is None and ("reference" in low or low == "mpn"):
            ref = value
    if ref in ("Does not apply", "Does Not Apply", "NA", "N/A"):
        ref = None
    if not ref:
        found = _REF_RE.search(text or "")
        if found:
            ref = found.group(1).strip(" .")
    return model, ref


def _year_guess(*texts):
    """Specific year (or decade) from prose; context-anchored matches first."""
    for rx in (_YEAR_CTX_RE, _YEAR_ANY_RE):
        for t in texts:
            found = rx.search(t or "")
            if found:
                return found.group(1)
    return None


# Region ids whose scope covers the US when they appear in regionIncluded.
_US_COVERING = {"US", "WORLDWIDE", "NORTH_AMERICA", "AMERICAS"}


def _ships_to_us(d: dict):
    """True/False from shipToLocations; None when the listing carries no data.

    An explicit US (or Americas-wide) exclusion wins over any inclusion —
    'ships worldwide except US' is a common pattern on overseas listings.
    """
    ship_to = d.get("shipToLocations") or {}
    included = {r.get("regionId") for r in ship_to.get("regionIncluded") or []}
    excluded = {r.get("regionId") for r in ship_to.get("regionExcluded") or []}
    if not included and not excluded:
        return None
    if excluded & _US_COVERING:
        return False
    return bool(included & _US_COVERING)


def _format_time_left(secs: float) -> str:
    """Always show two useful units, seconds included once inside the hour.

    This is a snapshot taken at fetch time — it is already stale by the time it
    reaches a phone. The page re-derives the countdown from `ends` and ticks it
    live; this exists for API callers, which is why it carries seconds too.
    """
    if secs <= 0:
        return "ended"
    days, rem = int(secs // 86400), secs % 86400
    hours, mins, sec = int(rem // 3600), int(rem % 3600 // 60), int(rem % 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m {sec}s"
    return f"{mins}m {sec}s"


def _format_age(secs: float) -> str:
    """How long ago something was listed, to the minute inside the first day.

    "0d ago" is the least useful thing to say about a listing posted this
    morning: under a day the hours and minutes are the whole point, because
    that is the only window in which being early is worth anything.
    """
    if secs < 0:
        return "0m"
    days = int(secs // 86400)
    if days:
        return f"{days}d"
    hours, mins = int(secs % 86400 // 3600), int(secs % 3600 // 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def item_summary(d: dict) -> dict:
    """The handful of fields worth reading on a phone, off the raw response."""
    options = d.get("buyingOptions") or []
    is_auction = "AUCTION" in options
    money = (d.get("currentBidPrice") if is_auction else d.get("price")) \
        or d.get("price") or {}
    end = d.get("itemEndDate") or ""
    time_left = None
    if end:
        try:
            dt = datetime.strptime(end[:19], "%Y-%m-%dT%H:%M:%S") \
                         .replace(tzinfo=timezone.utc)
            time_left = _format_time_left(
                (dt - datetime.now(timezone.utc)).total_seconds())
        except ValueError:
            pass
    seller = d.get("seller") or {}
    primary = (d.get("image") or {}).get("imageUrl", "")
    created = d.get("itemCreationDate") or ""
    listed_days = listed_ago = None
    if created:
        try:
            cdt = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S") \
                          .replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - cdt).total_seconds()
            listed_days = int(age // 86400)
            listed_ago = _format_age(age)
        except ValueError:
            pass
    # Sold vs expired-unsold changes how to read the price entirely: a sold
    # listing is a comp, an unsold one is a relist candidate you can lowball.
    avail = (d.get("estimatedAvailabilities") or [{}])[0]
    ship = (d.get("shippingOptions") or [{}])[0]
    coupon = (d.get("availableCoupons") or [{}])[0]
    marketing = d.get("marketingPrice") or {}
    loc = d.get("itemLocation") or {}
    aspects = {a.get("name"): a.get("value")
               for a in (d.get("localizedAspects") or []) if a.get("name")}
    key_specs, spec_names = _key_specs(aspects)
    description = _strip_html(d.get("description") or "")
    short_desc = d.get("shortDescription") or ""
    # The AG-arbitrage read: is this covered by Authenticity Guarantee
    # while the seller quietly says in prose that it doesn't run?
    auth = authguard.classify(d)
    cond_desc = _strip_html(d.get("conditionDescription") or "")
    model, ref = _model_ref(aspects, short_desc + "\n" + description)
    box = next((v for k, v in aspects.items()
                if re.search(r"(?i)original\s+box|box/packaging", k or "")), None)
    papers = next((v for k, v in aspects.items()
                   if re.search(r"(?i)with\s+papers|papers/coa", k or "")), None)
    manual = next((v for k, v in aspects.items()
                   if re.search(r"(?i)manual|booklet", k or "")), None)
    caseback = next((v for k, v in aspects.items()
                     if re.match(r"(?i)case\s*back$", (k or "").strip())), None)
    # Numeric diameter for colour-coding. Sellers write "40 mm" or
    # "44.70mm X 36mm" (lug-to-lug x diameter) — the smaller plausible
    # number is the diameter.
    case_val = next((v for label, v in key_specs if label == "Case size"), None)
    case_mm = None
    if case_val:
        nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", case_val)]
        nums = [n for n in nums if 16 <= n <= 60]
        if nums:
            case_mm = min(nums)
    lug_val = next((v for label, v in key_specs if label == "Band/lug width"), None)
    lug_mm = None
    if lug_val:
        nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", lug_val)]
        nums = [n for n in nums if 6 <= n <= 30]
        if nums:
            lug_mm = nums[0]
    # Prose year first (the seller's own dating), aspect Year Manufactured as
    # the fallback — specifics are often left at a placeholder value.
    year = _year_guess(short_desc, cond_desc, description) \
        or next((v for k, v in aspects.items()
                 if "year" in (k or "").lower() and v), None)
    return {
        "title": d.get("title"),
        # getItem hands back the full-size render; ask for the small one so a
        # phone on cellular pulls ~9 KB instead of ~175 KB.
        "image": re.sub(r"s-l\d+", "s-l500", primary) if primary else "",
        "price": money.get("value"),
        "currency": money.get("currency"),
        "buying": options,
        "is_auction": is_auction,
        "bid_count": d.get("bidCount"),
        # Bids alone can't tell a two-bidder duel from broad demand.
        "unique_bidders": d.get("uniqueBidderCount"),
        # The seller's own one-line summary — usually names the calibre.
        "short_description": d.get("shortDescription"),
        "brand": d.get("brand"),
        "model": model,
        "reference": ref,
        "year": year,
        "box": box,
        "papers": papers,
        "manual": manual,
        "caseback": caseback,
        "case_mm": case_mm,
        "lug_mm": lug_mm,
        "condition": d.get("condition"),
        # Numeric and locale-stable (7000 = for parts, 3000 = used), unlike
        # the free-text condition above.
        "condition_id": auth["condition_id"],
        # Authenticity Guarantee comes straight off qualifiedPrograms.
        # Never inferred from price: that threshold varies by category
        # and eBay has moved it before.
        "has_authenticity_guarantee": auth["has_authenticity_guarantee"],
        "qualified_programs": auth["qualified_programs"],
        "description_indicates_as_is": auth["description_indicates_as_is"],
        "as_is_terms": auth["as_is_terms"],
        "as_is_excerpt": auth["as_is_excerpt"],
        "auth_arbitrage_class": auth["auth_arbitrage_class"],
        "ends": end,
        "time_left": time_left,
        "listed": created,
        "listed_days": listed_days,
        # Whole days for anything older, hours and minutes inside the first
        # one — `listed_days` alone flattens a fresh listing to a bare 0.
        "listed_ago": listed_ago,
        "availability": avail.get("estimatedAvailabilityStatus"),
        "sold_qty": avail.get("estimatedSoldQuantity"),
        "remaining_qty": avail.get("estimatedRemainingQuantity"),
        "min_bid": (d.get("minimumPriceToBid") or {}).get("value"),
        "shipping_cost": (ship.get("shippingCost") or {}).get("value"),
        "shipping_type": ship.get("shippingCostType"),
        "coupon": ({"amount": (coupon.get("discountAmount") or {}).get("value"),
                    "code": coupon.get("redemptionCode")}
                   if coupon.get("discountAmount") else None),
        "discount_pct": marketing.get("discountPercentage"),
        "location": ", ".join(x for x in (loc.get("city"),
                                          loc.get("stateOrProvince"),
                                          loc.get("country")) if x),
        "lot_size": d.get("lotSize") or None,
        "ships_to_us": _ships_to_us(d),
        "seller": seller.get("username"),
        "feedback_pct": seller.get("feedbackPercentage"),
        "feedback_score": seller.get("feedbackScore"),
        "returns": (d.get("returnTerms") or {}).get("returnsAccepted"),
        "photos": (1 if d.get("image") else 0) + len(d.get("additionalImages") or []),
        "aspects": aspects,
        # Dimensions first — they decide whether it fits the wrist and whether
        # a band you own will fit it, before price is worth thinking about.
        "key_specs": key_specs,
        "key_spec_names": spec_names,
        "spec_notes": _spec_notes(description),
        "fault_notes": _fault_notes(description),
        # Some sellers write a free-text condition note on top of eBay's
        # canonical condition name; it's where the real faults get described.
        "condition_description": cond_desc or None,
        "description": description[:8000],
        "description_truncated": len(description) > 8000,
        "url": d.get("itemWebUrl"),
        # Same date shape as Listed/Ends (2026-08-06), not 8/6/2026.
        # Always Mountain time (America/Denver), wherever the server runs.
        "fetched": (datetime.now(MOUNTAIN) if MOUNTAIN else datetime.now())
                   .strftime("%Y-%m-%d %I:%M %p").replace(" 0", " ", 1),
    }


def fetch_item(ref: str) -> dict:
    kind, item_id = parse_item_ref(ref)
    client = ebay_client()
    return (client.get_item(item_id) if kind == "browse"
            else client.get_item_by_legacy_id(item_id))


# ---------------------------------------------------------------------------
# National Rarities: the turnaroundrarities weekly auction drop. One trusted
# consignment seller (source of the IWC 3706, Navitimer A23322, and Chronomat),
# whose auctions mostly end Sunday night — so this is a browse-the-week page,
# not an alert stream. Everything they list is shown; taste only sets the order.
# ---------------------------------------------------------------------------

RARITIES_TTL = 180  # seconds; reopening the tab shouldn't cost an eBay sweep

_rarities_lock = threading.Lock()
_rarities_cache: dict = {"ts": 0.0, "items": None}


def _invalidate_rarities() -> None:
    """Force the next load to re-rank — a new heart changes the order."""
    with _rarities_lock:
        _rarities_cache["ts"] = 0.0


def fetch_rarities(db_path: str, fresh: bool = False) -> dict:
    """The week's lots, taste-ranked, with what he has hearted layered on top."""
    with _rarities_lock:
        age = time.time() - _rarities_cache["ts"]
        if _rarities_cache["items"] is not None and age < RARITIES_TTL and not fresh:
            return {"cached_secs": int(age), "items": _rarities_cache["items"]}
    items = fetch_seller_auctions(ebay_client())
    storage = Storage(db_path)
    try:
        hearted = storage.rarities_liked_titles()
        thumbed = storage.rarities_disliked_titles()
        saved_ids, disliked_ids = storage.rarities_voted_ids()
    finally:
        storage.close()
    likes = Counter()
    for title in hearted:
        likes.update(_tokens(title))
    # A thumbs-down is an explicit "send this to the bottom", so it gets an
    # order of magnitude more weight than a heart — enough to cross the tier gaps.
    thumbs_down = Counter()
    for title in thumbed:
        thumbs_down.update(_tokens(title))
    for item in items:
        bag = _tokens(item["title"])
        boost = (min(6.0, sum(min(likes[t], 4) for t in bag) * 0.22)
                 - min(60.0, sum(min(thumbs_down[t], 4) for t in bag) * 3.0))
        item["saved"] = item["id"] in saved_ids
        item["disliked"] = item["id"] in disliked_ids
        item["boost"] = round(boost, 2)
        item["taste"] = round(item["taste"] + boost, 2)
        # The one he actually thumbed down goes below everything, similar or
        # not — he has already looked at it and said no.
        if item["disliked"]:
            item["taste"] = -999.0
    # Hearts are not pinned to the top — that would crowd out the new lots the
    # tab exists to surface. They shift the ranking instead, and the filters
    # are there when he wants either shortlist on its own.
    items.sort(key=lambda r: (-r["taste"], r["ends"] or "9999"))
    with _rarities_lock:
        _rarities_cache["ts"] = time.time()
        _rarities_cache["items"] = items
    return {"cached_secs": 0, "items": items}


# ---------------------------------------------------------------------------
# The dashboard. `/` is the board; every tool hangs off it and links back.
# HUB_PORT is a second service (Watch Hub) on this same Pi, so its links are
# built client-side from whatever hostname you arrived on — that keeps one
# home-screen icon working over both Tailscale and the LAN.
# ---------------------------------------------------------------------------

HUB_PORT = 8090

APPS = [
    {"id": "lookup", "name": "Listing lookup", "icon": "&#128269;", "where": "local",
     "path": "/item", "blurb": "Paste an eBay listing, get live JSON back."},
    {"id": "rarities", "name": "National Rarities", "icon": "&#127963;", "where": "local",
     "path": "/rarities", "blurb": "The turnaroundrarities weekly drop, ranked to your taste."},
    {"id": "search", "name": "Quick search", "icon": "&#9889;", "where": "hub",
     "path": "/search", "blurb": "Live eBay search, any query, newest first."},
    {"id": "lathe", "name": "Lathe outfits", "icon": "&#128296;", "where": "hub",
     "path": "/search?hunt=LATHE&preset=1&days=30",
     "blurb": "The standing 8mm lathe sweep, scored."},
    {"id": "jacot", "name": "Jacot tools", "icon": "&#9881;", "where": "hub",
     "path": "/search?hunt=JACOT&preset=1&days=30",
     "blurb": "The standing Jacot / pivot-polisher sweep."},
]

# Health is a property of the service a tile lives on, not of the tile. Probing
# per-tile meant tiles with nothing to probe defaulted to green while their
# service was down. Both probes are cheap and neither costs an eBay call.
SERVICES = {
    "local": {"name": "GemHunter", "health": "/api/health"},
    "hub": {"name": "Watch Hub", "health": "/manifest.json"},
}

DASHBOARD = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#08111f"><title>GemHunter</title>
<link rel="manifest" href="/manifest.json">
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#08111f;color:#e9eef6;font:16px/1.45 -apple-system,
 BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:18px 14px 48px;
 max-width:720px;margin:0 auto}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.eyebrow{font-size:12px;font-weight:750;color:#9fb0c9}
h1{font-size:40px;line-height:.95;margin:4px 0 0;font-weight:900;letter-spacing:-1.6px;
 background:linear-gradient(90deg,#f8fafc,#7dd3fc 48%,#facc15);
 -webkit-background-clip:text;background-clip:text;color:transparent}
.refresh{border:1px solid rgba(148,163,184,.22);background:rgba(15,23,42,.72);
 color:#e9eef6;width:44px;height:44px;border-radius:14px;font-size:20px;font-weight:800;
 flex:none;display:grid;place-items:center}
.refresh:disabled{opacity:.5}
.status{font-size:12px;color:#8ba0bd;margin:14px 0 4px;min-height:17px}
a.tile{display:flex;gap:13px;align-items:center;text-decoration:none;color:inherit;
 background:rgba(16,26,45,.96);border:1px solid rgba(148,163,184,.16);
 border-radius:20px;padding:15px;margin-top:11px}
a.tile:active{background:rgba(26,40,66,.96)}
.orb{width:46px;height:46px;border-radius:15px;display:grid;place-items:center;
 font-size:22px;background:#0b1425;border:1px solid rgba(148,163,184,.18);flex:none}
.tile h2{margin:0;font-size:17px;font-weight:800}
.tile p{margin:3px 0 0;color:#8ba0bd;font-size:13px}
.dot{width:9px;height:9px;border-radius:50%;background:#334765;flex:none}
.dot.ok{background:#4ade80}.dot.bad{background:#f87171}
.foot{color:#64758c;font-size:12px;margin-top:22px;line-height:1.6}
.foot code{color:#8ba0bd;font-size:11px}
</style></head><body>
<div class="head">
  <div><div class="eyebrow">Private watch tools</div><h1>Dashboard</h1></div>
  <button class="refresh" id="refreshBtn" onclick="refreshAll()"
          aria-label="Refresh all apps">&#8635;</button>
</div>
<div class="status" id="status"></div>
<div id="tiles"></div>
<div class="foot">
  Every tool here is also a plain GET JSON endpoint &mdash;
  <code>/api/apps</code> lists them.
</div>
<script>
var APPS = __APPS__, SERVICES = __SERVICES__, HUB_PORT = __HUBPORT__;
var HUB = location.protocol + '//' + location.hostname + ':' + HUB_PORT;
function base(w){ return w === 'hub' ? HUB : ''; }
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g,
  function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

function render(){
  document.getElementById('tiles').innerHTML = APPS.map(function(a){
    return '<a class="tile" id="tile-' + a.id + '" href="' + base(a.where) + a.path + '">'
      + '<div class="orb">' + a.icon + '</div>'
      + '<div style="flex:1"><h2>' + esc(a.name) + '</h2><p>' + esc(a.blurb) + '</p></div>'
      + '<div class="dot" id="dot-' + a.id + '"></div></a>';
  }).join('');
}
render();

function setDots(where, cls){
  APPS.filter(function(a){ return a.where === where; }).forEach(function(a){
    document.getElementById('dot-' + a.id).className = 'dot ' + cls;
  });
}

// "Refresh all" re-checks every service and cache-busts each tile link so
// opening an app never lands on a stale page.
function refreshAll(){
  var btn = document.getElementById('refreshBtn'), st = document.getElementById('status');
  btn.disabled = true; st.textContent = 'Refreshing\\u2026';
  var stamp = Date.now(), notes = [], pending = 0;

  APPS.forEach(function(a){
    var tile = document.getElementById('tile-' + a.id);
    tile.href = base(a.where) + a.path
              + (a.path.indexOf('?') < 0 ? '?' : '&') + 'v=' + stamp;
  });

  function finish(){
    if (--pending > 0) return;
    btn.disabled = false;
    st.textContent = notes.length ? notes.join('  \\u00b7  ') : 'All services responded.';
  }

  Object.keys(SERVICES).forEach(function(w){
    pending++;
    fetch(base(w) + SERVICES[w].health + '?v=' + stamp, {cache: 'no-store'})
      .then(function(r){ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(d){
        if (d.ebay_keys === false) {
          setDots(w, 'bad'); notes.push(SERVICES[w].name + ': eBay keys missing');
        } else {
          setDots(w, 'ok');
        }
      })
      .catch(function(e){
        setDots(w, 'bad'); notes.push(SERVICES[w].name + ' unreachable (' + e.message + ')');
      })
      .then(finish);
  });
}
refreshAll();
</script></body></html>
"""


def _dashboard() -> bytes:
    return (DASHBOARD
            .replace("__APPS__", json.dumps(APPS))
            .replace("__SERVICES__", json.dumps(SERVICES))
            .replace("__HUBPORT__", str(HUB_PORT))
            ).encode("utf-8")


ITEM_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#08111f"><title>Listing lookup</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#08111f;color:#e9eef6;font:16px/1.45 -apple-system,
 BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:16px 14px 48px;max-width:760px;margin:0 auto}
h1{font-size:20px;margin:4px 0 2px}a{color:#7cc4ff}
.sub{color:#8ba0bd;font-size:13px;margin:0 0 16px}
input,button{width:100%;font:inherit;color:#e9eef6;background:#0f1c30;
 border:1px solid #23395c;border-radius:11px;padding:12px 13px;appearance:none}
/* flex, so the wrapper is exactly the input's height — as a block it picked up
   the inline baseline gap and anything centred in it sat low. */
.field{position:relative;display:flex}
.field input{flex:1;padding-right:52px}
/* Pinned top and bottom rather than centred: fills the input's full height
   whatever that height is, and gives a proper thumb-sized target. */
/* margin:0 matters — the generic `button` rule below carries margin-top:12px,
   which this never overrode, so the button sat 12px low and 12px short. */
.clear{display:none;position:absolute;top:0;bottom:0;right:0;width:48px;height:auto;
 margin:0;padding:0;border:0;border-radius:0 11px 11px 0;background:transparent;
 color:#8ba0bd;font-size:25px;font-weight:400;line-height:1;place-items:center}
.clear.on{display:grid}
.clear:active{background:rgba(148,163,184,.15);color:#e9eef6}
button{background:#c9a227;color:#0b1220;border:0;font-weight:700;margin-top:12px}
button:disabled{opacity:.55}
.card{background:#0f1c30;border:1px solid #1d2f4c;border-radius:14px;padding:14px;margin-top:16px}
.t{font-size:15px;margin:0 0 10px;line-height:1.35}
.shot{display:block;width:100%;max-width:260px;margin:0 auto 12px;border-radius:12px;
 background:#08111f}
.sd{margin:0 0 10px;padding:9px 11px;background:#0b1526;border-left:3px solid #c9a227;
 border-radius:0 10px 10px 0;color:#c9d6e8;font-size:13px;line-height:1.4}
.sd.note{border-left-color:#ff4d3d}
.callout{margin:0 0 10px;padding:10px 12px;border-radius:11px;background:#122239;
 border:1px solid #23395c;font-size:18px;font-weight:800;text-align:center}
.callout small{font-size:11px;font-weight:600;opacity:.6;vertical-align:middle}
.callout.bad{background:rgba(255,77,61,.12);border-color:#ff4d3d;color:#ff6b5c;font-size:21px}
.callout.good{background:rgba(74,222,128,.1);border-color:#4ade80;color:#7ee2a8}
.callout.warn{background:rgba(251,191,36,.1);border-color:#fbbf24;color:#fde68a;font-size:17px}
/* Authenticity Guarantee, in the badge eBay puts on the listing itself: a
   blue seal and a tick when it applies, a red one and a cross when it does
   not. One fact, stated the way he already reads it on eBay — the reasoning
   about conditionId belongs in the code, not on the card. */
.agb{display:flex;align-items:center;gap:10px;margin:0 0 10px;padding:11px 12px;
 border-radius:12px;border:1px solid;font-size:16px;font-weight:800;line-height:1.2}
/* The condition field is what decides the guarantee, so it is stated right
   under the verdict rather than repeated in a callout of its own. */
.agb small{display:block;margin-top:4px;font-size:13px;font-weight:700;opacity:.82}
.agb svg{width:25px;height:25px;flex:none}
.agb .tick{fill:none;stroke:#fff;stroke-width:2.3;stroke-linecap:round;
 stroke-linejoin:round}
.agb.yes{background:rgba(54,101,243,.14);border-color:rgba(54,101,243,.6);color:#d3e1ff}
.agb.yes .seal{fill:#3665f3}
.agb.no{background:rgba(224,50,43,.1);border-color:rgba(224,50,43,.5);color:#ffc9c1}
.agb.no .seal{fill:#e0322b}
/* What the seller admits, under the badge. Blue seal plus a gold note is the
   find: authenticated by eBay, and broken according to the man selling it. */
.agnote{margin:-2px 0 10px;padding:9px 11px;border-radius:0 10px 10px 0;
 font-size:13px;line-height:1.4;background:rgba(240,214,122,.1);
 border-left:3px solid #f0d67a;color:#ffe9a3}
.agnote.unread{background:rgba(251,191,36,.08);border-left-color:#fbbf24;color:#fde68a}
.listed{color:#7dd3fc;font-size:17px;font-weight:800}
.model{font-weight:800;font-size:15px}
.fb{font-weight:700}
.fb.good{color:#4ade80;font-size:17px;font-weight:800}
.fb.bad{color:#ff4d3d;font-size:17px;font-weight:800}
.fb.gold{color:#f0d67a;font-size:17px;font-weight:800}
.fb.purple{color:#c084fc;font-size:17px;font-weight:800}
.notes{margin:0 0 10px;padding:9px 11px;background:#0b1526;border:1px solid #1d2f4c;
 border-radius:11px;font-size:13px;color:#c9d6e8}
.notes b{display:block;color:#8ba0bd;font-size:11px;text-transform:uppercase;
 letter-spacing:.6px;margin-bottom:5px}
.notes p{margin:0 0 5px}
.notes.fault{border-color:rgba(255,77,61,.4);background:rgba(255,77,61,.07)}
.notes.fault b{color:#ff8a7a}
.desc{white-space:pre-wrap;background:#0b1526;border:1px solid #1d2f4c;border-radius:11px;
 padding:12px;font-size:13px;line-height:1.5;color:#c9d6e8;max-height:340px;overflow-y:auto}
.big{font-size:26px;font-weight:700;color:#f0d67a}
.k{display:flex;justify-content:space-between;gap:12px;padding:7px 0;
 border-top:1px solid #1d2f4c;font-size:14px}
.k span:first-child{color:#8ba0bd}
.k span:last-child{text-align:right}
.tl{font-variant-numeric:tabular-nums;font-size:19px;font-weight:800;line-height:1.1}
.tl.soon{color:#ff4d3d}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.tl.soon{animation:pulse 2s ease-in-out infinite}
.err{background:#3a1c1c;border:1px solid #6b2f2f;color:#ffc9bd;padding:12px;
 border-radius:11px;margin-top:16px;font-size:14px}
pre{background:#0b1526;border:1px solid #1d2f4c;border-radius:11px;padding:12px;
 overflow-x:auto;font-size:11px;line-height:1.4;color:#b9cbe4}
summary{cursor:pointer;color:#8ba0bd;font-size:13px;margin-top:14px}
</style></head><body>
<h1><a href="/">&larr;</a> &nbsp;Listing lookup</h1>
<p class="sub">Live from the eBay Browse API with your developer keys. Paste a
listing URL, an item number, or a share link from the eBay app.</p>
<div class="field">
  <input id="q" placeholder="ebay.io/m/pG5UUs &nbsp;or&nbsp; 127998919225"
         autocapitalize="none" autocorrect="off" enterkeyhint="go">
  <button type="button" class="clear" id="clear" onclick="clearField()"
          aria-label="Clear">&times;</button>
</div>
<button id="btn" onclick="go()">Fetch live data</button>
<div id="out"></div>
<script>
var P = new URLSearchParams(location.search);
if (P.get('id')) { document.getElementById('q').value = P.get('id'); }
document.getElementById('q').addEventListener('keydown', function(e){
  if (e.key === 'Enter') go();
});
document.getElementById('q').addEventListener('input', toggleClear);

function toggleClear(){
  document.getElementById('clear')
          .classList.toggle('on', !!document.getElementById('q').value);
}
// Clearing means "I'm looking up something else", so the old card goes too —
// leaving it would show a frozen listing whose countdown and auto-refresh have
// already stopped, which reads as live when it isn't.
function clearField(){
  var q = document.getElementById('q');
  q.value = '';
  toggleClear();
  if (ticker) { clearInterval(ticker); ticker = null; }
  if (refresher) { clearTimeout(refresher); refresher = null; }
  lastFetch = 0; staleMsg = '';
  document.getElementById('out').innerHTML = '';
  history.replaceState(null, '', '/item');
  q.focus();
}
toggleClear();
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g,
  function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function row(k, v){ return v == null || v === '' ? '' :
  '<div class="k"><span>' + k + '</span><span>' + esc(v) + '</span></div>'; }
function noteBlock(label, lines, cls){
  if (!lines || !lines.length) return '';
  return '<div class="notes' + cls + '"><b>' + label + '</b>'
       + lines.map(function(n){ return '<p>' + esc(n) + '</p>'; }).join('') + '</div>';
}
// Case diameter, colour-coded to wearability: under 36 red, 36-41 green,
// over 41 purple. Unparseable sizes stay gold — flagged, not judged.
function caseAndLug(s){
  var ks = s.key_specs || [], caseKv = null, lugKv = null, rest = [];
  ks.forEach(function(kv){
    if (kv[0] === 'Case size') caseKv = kv;
    else if (kv[0] === 'Band/lug width') lugKv = kv;
    else rest.push(kv);
  });
  var h = '';
  if (caseKv) {
    var cls = 'gold';
    if (s.case_mm != null)
      cls = s.case_mm > 41 ? 'purple' : (s.case_mm >= 36 ? 'good' : 'bad');
    h += '<div class="k"><span>Case diameter</span><span class="fb ' + cls + '">'
       + esc(caseKv[1])
       + (s.case_mm != null && /x/i.test(caseKv[1])
            ? ' <small style="opacity:.65">(' + s.case_mm + 'mm dia)</small>' : '')
       + '</span></div>';
  }
  if (lugKv) {
    // 18 gold, 19-22 green, over 22 purple; below 18 stays neutral (no band
    // was specified for it). Unparseable stays neutral too.
    var lc = '';
    if (s.lug_mm != null) {
      if (s.lug_mm === 18) lc = ' gold';
      else if (s.lug_mm >= 19 && s.lug_mm <= 22) lc = ' good';
      else if (s.lug_mm > 22) lc = ' purple';
    }
    h += '<div class="k"><span>Lug width</span><span class="fb' + lc + '">'
       + esc(lugKv[1]) + '</span></div>';
  }
  return h + rest.map(function(kv){ return row(kv[0], kv[1]); }).join('');
}

function yesNoRow(label, v){
  if (!v) return '';
  var cls = /^yes/i.test(v) ? ' good' : (/^no/i.test(v) ? ' bad' : '');
  return '<div class="k"><span>' + label + '</span><span class="fb' + cls + '">'
       + esc(v) + '</span></div>';
}
function money(v){
  var n = Number(v);
  return isNaN(n) ? String(v)
       : '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2,
                                          maximumFractionDigits: 2});
}
function isOver(s){ return s.ends ? Date.parse(s.ends) <= Date.now() : false; }
// Sold vs expired-unsold, from estimatedAvailabilities. "ended" alone hides
// the only fact that decides how to read the final price: a sold listing is a
// comp, an unsold one is a relist candidate you can approach with a low offer.
function outcomeBadge(s){
  if (!isOver(s)) return '';
  // sold_qty > 0 is definitive. OUT_OF_STOCK alone only counts when eBay
  // omitted the quantity — an unsold ending also reads OUT_OF_STOCK, but with
  // an explicit sold_qty of 0.
  var sold = Number(s.sold_qty || 0) > 0
          || (s.sold_qty == null && s.availability === 'OUT_OF_STOCK');
  return sold
    ? '<div class="callout good">SOLD \\u2014 price above is a comp</div>'
    : '<div class="callout warn">ENDED UNSOLD \\u2014 relist / lowball candidate</div>';
}
function runLength(listed, ends){
  var a = listed ? Date.parse(listed) : NaN, b = ends ? Date.parse(ends) : NaN;
  if (isNaN(a) || isNaN(b) || b <= a) return '';
  return ' \\u00b7 ' + Math.round((b - a) / 86400000) + 'd run';
}

// The countdown is re-derived from the listing's own end timestamp and ticked
// here, so it stays true no matter how long the page has been open. Anything
// computed on the Pi is stale the moment it's sent.
var ticker = null, lastFetch = 0, staleMsg = '', lastItem = null;

// navigator.clipboard needs HTTPS; the Pi serves plain http, so fall back to
// the textarea/execCommand path, which still works there.
function copyJson(){
  if (!lastItem) return;
  var text = JSON.stringify(lastItem, null, 2);
  var done = function(ok){
    var b = document.getElementById('copyBtn');
    if (!b) return;
    b.textContent = ok ? 'Copied \\u2713' : 'Copy failed \\u2014 long-press the JSON above';
    setTimeout(function(){ b.textContent = 'Copy raw JSON'; }, 2500);
  };
  var legacy = function(){
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    done(ok);
  };
  if (navigator.clipboard && window.isSecureContext) {
    // If the modern API refuses (permissions, focus), try the old way
    // before reporting failure.
    navigator.clipboard.writeText(text).then(function(){ done(true); }, legacy);
    return;
  }
  legacy();
}
function ago(ms){
  var s = Math.floor(ms / 1000);
  if (s < 5) return 'just now';
  if (s < 60) return s + 's ago';
  var m = Math.floor(s / 60);
  return m < 60 ? m + 'm ago' : Math.floor(m / 60) + 'h ago';
}
// Listing age, as opposed to ago() above, which says how stale *our fetch* is
// and rounds away the minutes. Four hours old and four minutes old are
// different opportunities, so inside the first day this keeps them.
function listedAgo(iso){
  var t = Date.parse(iso);
  if (isNaN(t)) return '';
  var secs = Math.max(0, Math.floor((Date.now() - t) / 1000));
  var d = Math.floor(secs / 86400);
  if (d) return d + 'd ago';
  var h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60);
  return (h ? h + 'h ' + m + 'm' : m + 'm') + ' ago';
}
function fmtLeft(ms){
  if (ms <= 0) return 'ended';
  var t = Math.floor(ms / 1000);
  var d = Math.floor(t / 86400), h = Math.floor(t % 86400 / 3600),
      m = Math.floor(t % 3600 / 60), s = t % 60;
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm ' + s + 's';
  return m + 'm ' + s + 's';
}
function startTicker(ends){
  if (ticker) { clearInterval(ticker); ticker = null; }
  var el = document.getElementById('tl');
  var end = ends ? Date.parse(ends) : NaN;
  if (!el || isNaN(end)) return;
  var paint = function(){
    var left = end - Date.now();
    el.textContent = fmtLeft(left);
    el.classList.toggle('soon', left > 0 && left < 3600000);
    // Relative age, not a wall-clock stamp: two refreshes inside the same
    // minute have to look different, or the display can't be trusted.
    var f = document.getElementById('fresh');
    if (f) f.textContent = staleMsg || (lastFetch ? ago(Date.now() - lastFetch) : '');
    if (left <= 0 && ticker) { clearInterval(ticker); ticker = null; }
  };
  paint();
  ticker = setInterval(paint, 1000);
}

// The clock ticks on its own, but price and bid count only change when we ask
// eBay again. Re-fetch on a cadence matched to how fast the listing can move,
// and never while the tab is hidden — a phone in your pocket shouldn't burn
// API quota.
var refresher = null;
function refreshEvery(left){
  if (left < 600000) return 20000;      // last 10 min: sniping range
  if (left < 3600000) return 60000;     // last hour
  if (left < 86400000) return 300000;   // last day
  return 900000;
}
function scheduleRefresh(ends){
  if (refresher) { clearTimeout(refresher); refresher = null; }
  var end = ends ? Date.parse(ends) : NaN;
  if (isNaN(end)) return;               // fixed-price with no end: nothing to chase
  var left = end - Date.now();
  if (left <= 0) return;
  refresher = setTimeout(function(){
    if (document.hidden) { scheduleRefresh(ends); return; }
    go(true);
  }, refreshEvery(left));
}
function stale(msg){
  staleMsg = msg;
  var f = document.getElementById('fresh');
  if (f) f.textContent = msg;
}
// Coming back to the tab should show current numbers, not whatever was on
// screen when you locked the phone.
document.addEventListener('visibilitychange', function(){
  if (!document.hidden && document.getElementById('tl')) go(true);
});
// eBay's own seal, redrawn: a twelve-lobed rosette with a tick through it,
// or a cross when the programme does not cover this listing.
var AG_SEAL = '<svg viewBox="0 0 24 24" aria-hidden="true">'
  + '<path class="seal" d="M12.00 2.10Q15.03 0.70 16.95 3.43Q20.27 3.73 20.57 7.05Q23.30 8.97 21.90 12.00Q23.30 15.03 20.57 16.95Q20.27 20.27 16.95 20.57Q15.03 23.30 12.00 21.90Q8.97 23.30 7.05 20.57Q3.73 20.27 3.43 16.95Q0.70 15.03 2.10 12.00Q0.70 8.97 3.43 7.05Q3.73 3.73 7.05 3.43Q8.97 0.70 12.00 2.10Z"/>';
var AG_TICK = AG_SEAL + '<path class="tick" d="M7.4 12.2l3.1 3.1 6.1-6.4"/></svg>';
var AG_CROSS = AG_SEAL + '<path class="tick" d="M8.6 8.6l6.8 6.8M15.4 8.6l-6.8 6.8"/></svg>';
// One badge answers the only question worth asking first: does eBay
// authenticate this watch? Anything the seller admits about its condition
// rides underneath, because it is a separate fact and not a smaller one.
function agBanner(s){
  var has = s.has_authenticity_guarantee;
  if (has == null) return '';
  var out = '<div class="agb ' + (has ? 'yes' : 'no') + '">'
          + (has ? AG_TICK : AG_CROSS) + '<span>'
          + (has ? 'Authenticity Guarantee' : 'No Authenticity Guarantee')
          + (s.condition ? '<small>Condition: ' + esc(s.condition) + '</small>' : '')
          + '</span></div>';
  // On a for-parts listing the condition line already said it; quoting the
  // seller saying it again is just the same fact twice.
  if (s.description_indicates_as_is === true && Number(s.condition_id) !== 7000)
    out += '<div class="agnote"><b>Seller says it needs work</b>'
         + (s.as_is_excerpt ? ' \\u2014 \\u201c' + esc(s.as_is_excerpt)
            + '\\u201d' : '') + '</div>';
  else if (has && s.description_indicates_as_is === null)
    out += '<div class="agnote unread">No readable description came back, so '
         + 'nothing here confirms it is sound.</div>';
  return out;
}
function go(silent){
  var q = document.getElementById('q').value.trim();
  if (!q) return;
  var btn = document.getElementById('btn'), out = document.getElementById('out');
  if (!silent) {
    history.replaceState(null, '', '/item?id=' + encodeURIComponent(q));
    btn.disabled = true; btn.textContent = 'Fetching\\u2026'; out.innerHTML = '';
  }
  fetch('/api/item?id=' + encodeURIComponent(q) + (silent ? '&_=' + Date.now() : ''),
        {cache: 'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d.error) {
        // A background refresh must never wipe a card you're reading — an
        // ended auction 404s, and that answer belongs next to the last price.
        if (silent) { stale(d.error); return; }
        out.innerHTML = '<div class="err">' + esc(d.error) + '</div>';
        return;
      }
      var s = d.summary;
      // What is it -> what shape is it in -> what will it cost me and how long
      // have I got -> who am I buying from -> housekeeping.
      var bids = s.bid_count == null ? null
               : (s.unique_bidders ? s.bid_count + ' from ' + s.unique_bidders + ' bidders'
                                   : String(s.bid_count));
      // A 100%/720 seller and a 91%/3 seller should not look alike at a glance.
      var pct = s.feedback_pct == null ? null : Number(s.feedback_pct);
      var sc = Number(s.feedback_score || 0);
      var fbCls = pct == null ? ''
                : (sc < 10 || pct < 97 ? ' bad'
                : (pct >= 99 && sc >= 100 ? ' good' : ''));
      var fbText = pct == null ? null
                 : pct + '% · ' + sc + (sc < 10 ? ' sales — thin history' : ' sales');
      // The server sends listed_ago too; recomputing here keeps a page left
      // open honest, and covers a fixed-price listing that never refreshes.
      var listedAge = listedAgo(s.listed) || s.listed_ago
                    || (s.listed_days != null ? s.listed_days + 'd ago' : '');
      var h = '<div class="card">'
        + (s.image ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">'
                     + '<img class="shot" src="' + esc(s.image) + '" alt=""></a>' : '')
        + '<p class="t">' + esc(s.title) + '</p>'
        + (s.short_description ? '<p class="sd">' + esc(s.short_description) + '</p>' : '')
        + agBanner(s)
        + (s.condition_description
             ? '<p class="sd note">' + esc(s.condition_description) + '</p>' : '')
        + caseAndLug(s)
        + noteBlock('Dimensions in the description', s.spec_notes, '')
        + noteBlock('What the seller says is wrong', s.fault_notes, ' fault')
        + row('Brand', s.brand)
        + ((s.model || s.reference)
             ? '<div class="k"><span>Model</span><span class="model">'
               + esc([s.model, s.reference ? 'Ref. ' + s.reference : null]
                     .filter(Boolean).join(' \\u2014 '))
               + '</span></div>' : '')
        + (s.year ? row('Year', s.year) : '')
        + '<div class="big">' + (s.price ? money(s.price) : 'n/a')
        + (s.is_auction ? ' <span style="font-size:13px;color:#8ba0bd">current bid</span>' : '')
        + '</div>'
        + outcomeBadge(s)
        + (s.ships_to_us === false
             ? '<div class="callout bad">DOES NOT SHIP TO THE US</div>' : '')
        + (s.ends ? '<div class="k"><span>Time left</span>'
                    + '<span id="tl" class="tl">' + esc(s.time_left || '') + '</span></div>'
                  : row('Time left', s.time_left))
        + row('Bids', bids)
        + (s.is_auction && s.min_bid && !isOver(s) ? row('Next bid', money(s.min_bid)) : '')
        + (s.listed
             ? '<div class="k"><span>Listed</span><span class="listed">'
               + esc(s.listed.slice(0,10))
               + (listedAge ? ' \\u00b7 ' + listedAge : '')
               + '</span></div>'
             : '')
        + ((s.remaining_qty != null && (s.remaining_qty > 1 || (s.sold_qty || 0) > 1))
             ? row('Quantity', (s.sold_qty || 0) + ' sold \\u00b7 '
                               + s.remaining_qty + ' left') : '')
        + (s.shipping_cost != null
             ? row('Shipping', (Number(s.shipping_cost) === 0 ? 'free'
                                : money(s.shipping_cost))
                   + (s.shipping_type === 'CALCULATED' ? ' (calculated)' : '')) : '')
        + (s.coupon ? '<div class="k"><span>Coupon</span><span class="fb good">'
                      + money(s.coupon.amount) + ' off'
                      + (s.coupon.code ? ' \\u00b7 ' + esc(s.coupon.code) : '')
                      + '</span></div>' : '')
        + (s.discount_pct ? row('Marked down', s.discount_pct + '%') : '')
        + row('Location', s.location)
        + (s.lot_size ? row('Lot size', s.lot_size) : '')
        + row('Seller', s.seller)
        + (fbText ? '<div class="k"><span>Feedback</span>'
                    + '<span class="fb' + fbCls + '">' + esc(fbText) + '</span></div>' : '')
        + row('Returns', s.returns === null ? null : (s.returns ? 'yes' : 'no'))
        + row('Photos', s.photos)
        + row('Ends', s.ends
                ? s.ends.slice(0,10) + runLength(s.listed, s.ends)
                : null)
        + '<div class="k"><span>Last Updated</span><span id="fresh">'
        + esc(s.fetched || '') + '</span></div>'
        + yesNoRow('Original box', s.box)
        + yesNoRow('Manual/booklet', s.manual)
        + yesNoRow('Papers', s.papers)
        + (s.caseback ? '<div class="k"><span>Caseback</span>'
                        + '<span class="fb gold">' + esc(s.caseback)
                        + '</span></div>' : '');
      // Everything else, minus whatever was already promoted above.
      var used = s.key_spec_names || [];
      Object.keys(s.aspects || {}).forEach(function(k){
        if (used.indexOf(k) >= 0) return;
        if (s.brand && /^brand$/i.test(k)) return;
        if (s.model && /^model$/i.test(k)) return;
        if (s.reference && /reference/i.test(k)) return;
        if (s.year && /^year/i.test(k)) return;
        if (s.box && /original\s+box|box\/packaging/i.test(k)) return;
        if (s.papers && /with\s+papers|papers\/coa/i.test(k)) return;
        if (s.manual && /manual|booklet/i.test(k)) return;
        if (s.caseback && /^case\s*back$/i.test(k.trim())) return;
        h += row(k, s.aspects[k]);
      });
      h += '<div class="k"><span></span><span><a target="_blank" rel="noopener" href="'
         + esc(s.url) + '">open on eBay</a></span></div></div>'
         + (s.description
              ? '<details open><summary>Full description</summary><div class="desc">'
                + esc(s.description)
                + (s.description_truncated ? '\\n\\n[truncated \\u2014 see raw JSON]' : '')
                + '</div></details>'
              : '')
         + '<details><summary>Raw JSON from eBay</summary><pre>'
         + esc(JSON.stringify(d.item, null, 2)) + '</pre></details>'
         + '<button type="button" class="go" id="copyBtn" onclick="copyJson()">'
         + 'Copy raw JSON</button>';
      lastItem = d.item;
      out.innerHTML = h;
      lastFetch = Date.now(); staleMsg = '';
      startTicker(s.ends);
      scheduleRefresh(s.ends);
    })
    .catch(function(e){
      if (silent) { stale('refresh failed'); return; }
      out.innerHTML = '<div class="err">' + esc(e) + '</div>';
    })
    .then(function(){
      if (!silent) { btn.disabled = false; btn.textContent = 'Fetch live data'; }
    });
}
if (P.get('id')) go();
</script></body></html>
"""


RARITIES_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#08111f"><title>National Rarities</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#08111f;color:#e9eef6;font:16px/1.45 -apple-system,
 BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:560px;margin:0 auto;
 padding:16px 14px calc(84px + env(safe-area-inset-bottom,0px))}
h1{font-size:20px;margin:4px 0 2px}a{color:#7cc4ff}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.sub{color:#8ba0bd;font-size:13px;margin:0 0 6px}
.refresh{border:1px solid rgba(148,163,184,.22);background:rgba(15,23,42,.72);
 color:#e9eef6;width:44px;height:44px;border-radius:14px;font-size:20px;font-weight:800;
 flex:none;display:grid;place-items:center}
.refresh:disabled{opacity:.5}
.status{font-size:12px;color:#8ba0bd;min-height:17px;margin-bottom:8px}
.card{background:#0f1c30;border:1px solid #1d2f4c;border-radius:16px;
 margin-top:14px;overflow:hidden}
/* The picture IS the page — full-bleed inside the card, info rides below. */
.shot{display:block;width:100%;min-height:200px;background:#0b1526}
.pic{position:relative;display:block}
/* Floats over the top-right of the photo. Sized for a thumb, and dark enough
   underneath that a white dial doesn't swallow it. */
.votes{position:absolute;top:9px;right:9px;display:flex;gap:7px}
.vote{width:42px;height:42px;border:0;border-radius:50%;
 background:rgba(8,17,31,.55);
 backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);
 color:#e9eef6;font-size:21px;line-height:1;display:grid;place-items:center;
 padding:0;transition:transform .12s ease}
.vote:active{transform:scale(.86)}
.vote svg{width:20px;height:20px;stroke:currentColor;fill:none}
.heart.on{color:#ff4d6a;background:rgba(8,17,31,.72)}
.down.on{color:#fbbf24;background:rgba(8,17,31,.72)}
.card.saved{border-color:rgba(255,77,106,.55)}
/* Demoted, not hidden: dimmed so it reads as "sent to the bottom" while
   staying easy to undo from the thumbs-down filter. */
.card.disliked{border-color:rgba(251,191,36,.4);opacity:.62}
.filters{display:flex;gap:8px;margin-bottom:4px}
.filt{font:inherit;font-size:12px;font-weight:700;color:#8ba0bd;
 background:#0f1c30;border:1px solid #23395c;border-radius:9px;padding:5px 11px}
.filt.on{color:#ff4d6a;border-color:rgba(255,77,106,.5);
 background:rgba(255,77,106,.1)}
.filt.down.on{color:#fbbf24;border-color:rgba(251,191,36,.5);
 background:rgba(251,191,36,.1)}
.filt svg{width:13px;height:13px;stroke:currentColor;fill:none;
 vertical-align:-2px;margin-right:3px}
.info{padding:11px 13px 13px}
.row{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.bid{font-size:24px;font-weight:800;color:#f0d67a}
.bid small{font-size:12px;font-weight:600;color:#8ba0bd;margin-left:6px}
.ends{text-align:right;font-size:14px;font-weight:700;color:#7dd3fc;
 font-variant-numeric:tabular-nums}
.ends .left{display:block;font-size:12px;font-weight:600;color:#8ba0bd}
.ends.soon,.ends.soon .left{color:#ff4d3d}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.ends.soon{animation:pulse 2s ease-in-out infinite}
.t{margin:7px 0 0;color:#c9d6e8;font-size:13px;line-height:1.35;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tags{display:flex;gap:8px;align-items:center;margin-top:10px;font-size:12px;
 padding-top:9px;border-top:1px solid #17263f}
.parts{color:#ff8a7a;border:1px solid rgba(255,77,61,.4);border-radius:8px;
 padding:2px 8px;font-weight:700}
.copy{margin-left:auto;font:inherit;font-size:11px;font-weight:700;
 color:#8ba0bd;background:transparent;border:1px solid #23395c;
 border-radius:8px;padding:3px 9px}
.copy:active{background:rgba(148,163,184,.15);color:#e9eef6}
.copy.done{color:#4ade80;border-color:rgba(74,222,128,.5)}
/* Straight under the photo: what the seller's own Condition Description says,
   which is the only place "Pre-owned - Good" gets contradicted. */
.flags{display:flex;flex-wrap:wrap;gap:6px;padding:11px 13px 0}
.flags:empty{display:none}
.fl{font-size:11px;font-weight:800;letter-spacing:.4px;border-radius:7px;
 padding:3px 8px;border:1px solid}
.fl.bad{color:#ff5b48;border-color:#ff4d3d;background:rgba(255,77,61,.13)}
.fl.warn{color:#fbbf24;border-color:rgba(251,191,36,.5);background:rgba(251,191,36,.1)}
.fl.good{color:#4ade80;border-color:rgba(74,222,128,.45);background:rgba(74,222,128,.1)}
/* Authenticity Guarantee is switched on by conditionId, so a lot listed
   pre-owned whose description admits it is broken still ships authenticated.
   That is the find on this page, and it gets the gold and the card border. */
.fl.ag{color:#ffe9a3;border-color:#f0d67a;background:rgba(240,214,122,.16)}
.fl.agc{color:#7ee2a8;border-color:rgba(74,222,128,.45);background:rgba(74,222,128,.1)}
.card.arb{border-color:#f0d67a;box-shadow:0 0 0 1px rgba(240,214,122,.25)}
/* The seller's own sentence, quoted under the chips — the evidence for the
   verdict, in his words rather than ours. */
.why{flex-basis:100%;margin:3px 0 0;color:#c9d6e8;font-size:12px;line-height:1.35}
.picks{display:flex;gap:8px;margin:8px 0 2px}
.picks select{font:inherit;font-size:12px;font-weight:700;color:#c9d6e8;
 background:#0f1c30;border:1px solid #23395c;border-radius:9px;padding:5px 8px}
.picks select.on{color:#ffe9a3;border-color:#f0d67a}
.mm{color:#8ba0bd;font-weight:700}
.mm.ok{color:#7dd3fc}
.err{background:#3a1c1c;border:1px solid #6b2f2f;color:#ffc9bd;padding:12px;
 border-radius:11px;margin-top:16px;font-size:14px}
.more{width:100%;font:inherit;font-weight:700;color:#e9eef6;background:#0f1c30;
 border:1px solid #23395c;border-radius:12px;padding:13px;margin-top:16px}
.more:disabled{opacity:.55}
/* Fixed, because this list runs to a few hundred cards and the header scrolls
   away immediately. Bottom-left keeps it under the thumb and clear of the
   per-card buttons on the right. */
.fab{position:fixed;left:12px;z-index:50;display:flex;gap:9px;
 bottom:calc(14px + env(safe-area-inset-bottom,0px))}
.fab a,.fab button{width:48px;height:48px;border-radius:50%;padding:0;
 display:grid;place-items:center;font-size:21px;font-weight:800;
 color:#e9eef6;text-decoration:none;background:rgba(15,28,48,.92);
 border:1px solid #23395c;box-shadow:0 8px 22px rgba(0,0,0,.45);
 backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}
.fab a:active,.fab button:active{background:rgba(30,48,78,.95)}
/* Only worth showing once there is something to scroll back up through. */
#topBtn{display:none}
#topBtn.on{display:grid}
</style></head><body>
<div class="head">
  <div>
    <h1><a href="/">&larr;</a> &nbsp;National Rarities</h1>
    <p class="sub"><a href="__STORE__" target="_blank" rel="noopener">turnaroundrarities</a>
    &middot; the week's auctions, ranked to your taste &middot; most end Sunday night</p>
  </div>
  <button class="refresh" id="refreshBtn" onclick="load(true)"
          aria-label="Refresh">&#8635;</button>
</div>
<div class="filters" id="filters"></div>
<div class="picks" id="picks"></div>
<div class="status" id="status"></div>
<div id="out"></div>
<div id="foot"></div>
<div class="fab">
  <a href="/" aria-label="Back to dashboard" title="Dashboard">&larr;</a>
  <button type="button" id="topBtn" onclick="toTop()"
          aria-label="Back to top" title="Back to top">&uarr;</button>
</div>
<script>
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g,
  function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function money(v){
  var n = Number(v);
  return isNaN(n) ? String(v)
       : '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2,
                                          maximumFractionDigits: 2});
}
// "Sun 6:42 PM" in the phone's own timezone — the day is the fact that
// matters when everything funnels toward Sunday night.
function endLabel(iso){
  var d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleString(undefined, {weekday:'short'}) + ' '
       + d.toLocaleString(undefined, {hour:'numeric', minute:'2-digit'});
}
function fmtLeft(ms){
  if (ms <= 0) return 'ended';
  var t = Math.floor(ms / 1000);
  var d = Math.floor(t / 86400), h = Math.floor(t % 86400 / 3600),
      m = Math.floor(t % 3600 / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}
// Start on the top slice: the whole consignment runs to several hundred lots,
// which is a slow parse and a slow paint on a phone. LIMIT 0 means everything.
var LIMIT = 60, TOTAL = 0, ITEMS = [], ticker = null, flagTries = 0;
var SAVED = 0, DOWN = 0, VIEW = '';
var KLASS = '', SORT = '', AUTH_COUNTS = {}, AUTH_READ = 0;
// Kept in the page's order of interest, which is also the server's sort order.
var AG_CLASSES = ['AG_ARBITRAGE', 'HONEST_BROKEN', 'AG_UNKNOWN',
                  'AG_CLEAN', 'NO_AG_OTHER'];
var AG_NAMES = {
  AG_ARBITRAGE: '\\u2726 AG arbitrage', HONEST_BROKEN: 'for parts \\u00b7 no AG',
  AG_UNKNOWN: 'AG \\u00b7 unread description', AG_CLEAN: 'AG \\u00b7 nothing claimed',
  NO_AG_OTHER: 'no AG'
};
var AG_CHIPS = {AG_ARBITRAGE: ['ag', '\\u2726 AG ARBITRAGE']};
// Same monochrome thumb the main app uses, so a vote looks the same everywhere.
var THUMB_SVG = '<svg viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round"'
  + ' stroke-linejoin="round" aria-hidden="true"><path d="M17 14V3"/>'
  + '<path d="M7 10.5 9.1 3H17v11h-5.2l-1.5 5.2c-.2.7-.8 1.2-1.6 1.2h-.4c-.8 0'
  + '-1.4-.8-1.2-1.6L8.4 14H5.2c-1.2 0-2.1-1.1-1.8-2.3l1.3-5.2C5 5.6 5.8 5 6.7 5h2"/>'
  + '</svg>';
function flagHtml(it){
  // The gold chip is the find; otherwise just say whether eBay authenticates
  // it, which the search hit knows for every lot from the first paint.
  var out = '', chip = AG_CHIPS[it.auth_arbitrage_class];
  if (chip) out += '<span class="fl ' + chip[0] + '">' + chip[1] + '</span>';
  else if (it.has_authenticity_guarantee)
    out += '<span class="fl agc">AG</span>';
  out += (it.flags || []).map(function(f){
    return '<span class="fl ' + esc(f.sev) + '">' + esc(f.label) + '</span>';
  }).join('');
  // Why we called it arbitrage: the sentence the seller wrote.
  if (it.auth_arbitrage_class === 'AG_ARBITRAGE' && it.as_is_excerpt)
    out += '<p class="why">\\u201c' + esc(it.as_is_excerpt) + '\\u201d</p>';
  return out;
}
function toTop(){
  var from = window.scrollY;
  window.scrollTo({top: 0, behavior: 'smooth'});
  // Some engines accept the smooth request and never animate. If nothing has
  // moved shortly after, jump — checking for *no* movement means a real
  // animation in progress is left alone rather than cut short.
  setTimeout(function(){
    if (from > 0 && window.scrollY === from) window.scrollTo(0, 0);
  }, 250);
}
// Also called after each render, not only on scroll: reopening the app can
// restore a scroll position, and the button has to match where you actually
// are rather than where the last scroll event left it.
function paintTop(){
  var b = document.getElementById('topBtn');
  if (b) b.classList.toggle('on', window.scrollY > 700);
}
window.addEventListener('scroll', paintTop, {passive: true});
// Voting is optimistic: the icon fills immediately, because waiting on the Pi
// round-trip makes a tap feel broken. If the write fails it reverts.
// Tapping the same vote again clears it, so a mis-tap is one tap to undo.
function vote(i, want){
  var it = ITEMS[i];
  if (!it) return;
  var had = it.saved ? 1 : (it.disliked ? -1 : 0);
  var now = (had === want) ? 0 : want;
  paintVote(i, now);
  it.saved = (now === 1); it.disliked = (now === -1);
  fetch('/api/rarities/vote', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({item_id: it.id, vote: now, title: it.title})
  })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d.ok) throw new Error('rejected');
      if (had === 1) SAVED--; if (had === -1) DOWN--;
      if (now === 1) SAVED++; if (now === -1) DOWN++;
      paintFilters();
    })
    .catch(function(){
      it.saved = (had === 1); it.disliked = (had === -1);
      paintVote(i, had);
    });
}
function paintVote(i, v){
  var hr = document.getElementById('hr-' + i),
      dn = document.getElementById('dn-' + i),
      card = document.getElementById('card-' + i);
  if (hr) {
    hr.classList.toggle('on', v === 1);
    hr.innerHTML = v === 1 ? '\\u2665' : '\\u2661';
  }
  if (dn) dn.classList.toggle('on', v === -1);
  if (card) {
    card.classList.toggle('saved', v === 1);
    card.classList.toggle('disliked', v === -1);
  }
}
function paintFilters(){
  var el = document.getElementById('filters');
  if (!el) return;
  el.innerHTML =
    '<button type="button" class="filt' + (VIEW === 'saved' ? ' on' : '')
      + '" onclick="setView(\\'saved\\')">'
      + (VIEW === 'saved' ? '\\u2665' : '\\u2661') + ' hearted'
      + (SAVED ? ' (' + SAVED + ')' : '') + '</button>'
    + '<button type="button" class="filt down'
      + (VIEW === 'disliked' ? ' on' : '') + '" onclick="setView(\\'disliked\\')">'
      + THUMB_SVG + 'buried' + (DOWN ? ' (' + DOWN + ')' : '') + '</button>';
}
// Filter and sort on the AG class. Only lots the background detail pass has
// already read carry one, so the status line says how many that is.
var PICKS_SIG = '';
function paintPicks(){
  var el = document.getElementById('picks');
  if (!el) return;
  // The flag poll lands every couple of seconds while the detail pass runs.
  // Rebuilding the selects each time would snap an open dropdown shut under
  // your thumb, so only redraw when the counts or the choices actually moved.
  var sig = KLASS + '|' + SORT + '|'
          + AG_CLASSES.map(function(c){ return AUTH_COUNTS[c] || 0; }).join(',');
  if (sig === PICKS_SIG) return;
  PICKS_SIG = sig;
  var opts = '<option value="">AG: all classes</option>'
    + AG_CLASSES.map(function(c){
        var n = AUTH_COUNTS[c] || 0;
        return '<option value="' + c + '"' + (KLASS === c ? ' selected' : '')
             + (n ? '' : ' disabled') + '>' + AG_NAMES[c]
             + (n ? ' (' + n + ')' : '') + '</option>';
      }).join('');
  el.innerHTML =
    '<select id="agSel" class="' + (KLASS ? 'on' : '')
      + '" aria-label="Filter by Authenticity Guarantee class"'
      + ' onchange="setKlass(this.value)">' + opts + '</select>'
    + '<select id="sortSel" class="' + (SORT ? 'on' : '')
      + '" aria-label="Sort order" onchange="setSort(this.value)">'
      + '<option value=""' + (SORT ? '' : ' selected') + '>sort: taste</option>'
      + '<option value="auth"' + (SORT ? ' selected' : '')
      + '>sort: AG class</option></select>';
}
function setKlass(v){ KLASS = v; flagTries = 0; load(false); }
function setSort(v){ SORT = v; flagTries = 0; load(false); }
// Every request carries the current view, class filter and sort together.
function query(){
  return '/api/rarities?limit=' + LIMIT + (VIEW ? '&view=' + VIEW : '')
       + (KLASS ? '&class=' + KLASS : '') + (SORT ? '&sort=' + SORT : '');
}
// Tapping the active filter returns to the full list.
function setView(v){
  VIEW = (VIEW === v) ? '' : v;
  flagTries = 0;
  paintFilters();
  paintPicks();
  load(false);
}
// The Pi serves plain http, where navigator.clipboard does not exist, so the
// old textarea trick is the path that actually works on the phone.
function copyText(text, btn){
  var done = function(ok){
    if (!btn) return;
    var was = btn.textContent;
    btn.textContent = ok ? 'copied \\u2713' : 'copy failed';
    btn.classList.toggle('done', ok);
    setTimeout(function(){
      btn.textContent = was; btn.classList.remove('done');
    }, 1800);
  };
  var legacy = function(){
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    done(ok);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function(){ done(true); }, legacy);
    return;
  }
  legacy();
}
// The card's own record — title, bid, bids, closing time, size, score, and
// whatever the condition description flagged. Everything the panel shows.
function copyItem(i){
  var it = ITEMS[i];
  if (!it) return;
  copyText(JSON.stringify(it, null, 2), document.getElementById('cp-' + i));
}
// Condition text lands after the photos do. Patch the chips into the cards
// already on screen rather than re-rendering — a full repaint would throw
// away your scroll position mid-browse.
function patchFlags(items){
  items.forEach(function(it, i){
    var el = document.getElementById('fl-' + i);
    if (el && it.flags) el.innerHTML = flagHtml(it);
    var card = document.getElementById('card-' + i);
    if (card) card.classList.toggle('arb',
      it.auth_arbitrage_class === 'AG_ARBITRAGE');
  });
}
function paintClocks(){
  ITEMS.forEach(function(it, i){
    var el = document.getElementById('ends-' + i);
    if (!el || !it.ends) return;
    var left = Date.parse(it.ends) - Date.now();
    el.querySelector('.left').textContent = fmtLeft(left);
    el.classList.toggle('soon', left > 0 && left < 3600000);
  });
}
function render(items){
  ITEMS = items;
  document.getElementById('out').innerHTML = items.map(function(it, i){
    return '<div class="card' + (it.saved ? ' saved' : '')
      + (it.disliked ? ' disliked' : '')
      + (it.auth_arbitrage_class === 'AG_ARBITRAGE' ? ' arb' : '')
      + '" id="card-' + i + '">'
      + '<div class="pic">'
      +   '<a href="' + esc(it.url) + '" target="_blank" rel="noopener">'
      +   (it.image ? '<img class="shot" loading="lazy" src="' + esc(it.image) + '" alt="">' : '')
      +   '</a>'
      +   '<div class="votes">'
      +     '<button type="button" class="vote heart' + (it.saved ? ' on' : '')
      +       '" id="hr-' + i + '" onclick="vote(' + i + ',1)" '
      +       'aria-label="More like this">'
      +       (it.saved ? '&#9829;' : '&#9825;') + '</button>'
      +     '<button type="button" class="vote down' + (it.disliked ? ' on' : '')
      +       '" id="dn-' + i + '" onclick="vote(' + i + ',-1)" '
      +       'aria-label="Less like this">' + THUMB_SVG + '</button>'
      +   '</div>'
      + '</div>'
      + '<div class="flags" id="fl-' + i + '">' + flagHtml(it) + '</div>'
      + '<div class="info">'
      +   '<div class="row">'
      +     '<div class="bid">' + money(it.bid)
      +       '<small>' + (it.bids ? it.bids + ' bid' + (it.bids > 1 ? 's' : '')
                                   : 'no bids yet') + '</small></div>'
      +     '<div class="ends" id="ends-' + i + '">' + esc(endLabel(it.ends))
      +       '<span class="left"></span></div>'
      +   '</div>'
      +   '<p class="t">' + esc(it.title) + '</p>'
      +   '<div class="tags">'
      +     (it.for_parts ? '<span class="parts">FOR PARTS</span>' : '')
      +     (it.mm ? '<span class="mm' + (it.mm >= 36 ? ' ok' : '') + '">'
                     + it.mm + 'mm</span>' : '')
      +     '<button type="button" class="copy" id="cp-' + i + '" '
      +       'onclick="copyItem(' + i + ')">copy JSON</button>'
      +     '<a class="inspect" href="/item?id=' + encodeURIComponent(it.id) + '">inspect &rarr;</a>'
      +   '</div>'
      + '</div></div>';
  }).join('');
  if (ticker) clearInterval(ticker);
  paintClocks();
  ticker = setInterval(paintClocks, 30000);
  paintFoot();
  paintTop();
}
// Says what is on screen and, when it matters, how much of the consignment has
// actually been read: an AG filter only sees lots the detail pass has fetched,
// and pretending otherwise would make a thin result look like proof.
function paintStatus(d){
  var st = document.getElementById('status');
  if (!st) return;
  var what = KLASS
             ? d.items.length + (d.items.length === 1 ? ' lot ' : ' lots ')
               + '\\u00b7 ' + AG_NAMES[KLASS]
           : (VIEW ? d.items.length + (VIEW === 'saved' ? ' hearted' : ' buried')
           : (d.items.length < TOTAL
               ? 'Top ' + d.items.length + ' of ' + TOTAL + ' lots'
               : TOTAL + ' auctions'));
  var read = (KLASS || SORT)
    ? ' \\u00b7 ' + AUTH_READ + ' of ' + (d.consignment_total || TOTAL)
      + ' read' + (d.details_pending ? ', still reading' : '')
    : '';
  st.textContent = what + read + ' \\u00b7 updated ' + d.updated
                 + (d.cached_secs ? ' (cached)' : '');
}
function paintFoot(){
  var foot = document.getElementById('foot');
  if (LIMIT && TOTAL > ITEMS.length) {
    foot.innerHTML = '<button type="button" class="more" id="moreBtn">'
      + 'Show all ' + TOTAL + ' lots</button>';
    document.getElementById('moreBtn').onclick = function(){
      this.disabled = true;
      this.textContent = 'Loading all ' + TOTAL + '…';
      LIMIT = 0;
      load(false);
    };
  } else {
    foot.innerHTML = '';
  }
}
// A failed "show all" must not leave a dead disabled button behind: put the
// cap back so the footer offers the expansion again.
// The background pass is still reading condition text. Come back for it a few
// times, backing off each round, then stop rather than polling forever.
function pollFlags(){
  if (flagTries >= 6) return;
  flagTries++;
  setTimeout(function(){
    fetch(query() + '&_=' + Date.now(), {cache: 'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d.items) return;
        ITEMS = d.items;
        AUTH_COUNTS = d.auth_counts || {};
        AUTH_READ = d.auth_read || 0;
        paintPicks();
        patchFlags(d.items);
        paintStatus(d);
        if (d.details_pending) pollFlags();
      })
      .catch(function(){});
  }, 2000 * flagTries);
}
function failedExpand(){
  if (!LIMIT) { LIMIT = 60; paintFoot(); }
}
function load(fresh){
  var btn = document.getElementById('refreshBtn'), st = document.getElementById('status');
  btn.disabled = true;
  if (!ITEMS.length) st.textContent = 'Fetching the week\\u2019s auctions\\u2026';
  fetch(query() + (fresh ? '&fresh=1' : ''), {cache: 'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d.error) {
        if (!ITEMS.length)
          document.getElementById('out').innerHTML =
            '<div class="err">' + esc(d.error) + '</div>';
        st.textContent = 'Refresh failed \\u2014 showing what was loaded.';
        failedExpand();
        return;
      }
      TOTAL = d.total || d.items.length;
      SAVED = d.saved_count || 0;
      DOWN = d.disliked_count || 0;
      AUTH_COUNTS = d.auth_counts || {};
      AUTH_READ = d.auth_read || 0;
      paintFilters();
      paintPicks();
      render(d.items);
      flagTries = 0;
      if (d.details_pending) pollFlags();
      paintStatus(d);
    })
    .catch(function(e){
      if (!ITEMS.length)
        document.getElementById('out').innerHTML = '<div class="err">' + esc(e) + '</div>';
      st.textContent = 'Refresh failed.';
      failedExpand();
    })
    .then(function(){ btn.disabled = false; });
}
// Coming back to the tab re-pulls through the server cache — current bids on
// screen, but no eBay call unless the cache has actually gone stale.
document.addEventListener('visibilitychange', function(){
  if (!document.hidden && ITEMS.length) load(false);
});
load(false);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    db_path = "gemhunter.db"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: object) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, _dashboard(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/apps":
            self._json(HTTPStatus.OK, {"hub_port": HUB_PORT, "apps": APPS,
                                       "services": SERVICES})
            return
        if parsed.path == "/api/health":
            try:
                keys = load_config().has_ebay_keys
            except Exception:
                keys = False
            self._json(HTTPStatus.OK, {
                "ok": True,
                "ebay_keys": keys,
                "db": self.db_path,
                "updated": _now_str(),
            })
            return
        if parsed.path == "/item":
            self._send(HTTPStatus.OK, ITEM_PAGE.encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if parsed.path == "/rarities":
            self._send(HTTPStatus.OK,
                       RARITIES_PAGE.replace("__STORE__", RARITIES_STORE_URL)
                                    .encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if parsed.path == "/api/rarities":
            params = parse_qs(parsed.query)
            fresh = params.get("fresh", [""])[0] in ("1", "true", "yes")
            try:
                data = fetch_rarities(self.db_path, fresh)
            except RuntimeError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                self._json(HTTPStatus.BAD_GATEWAY,
                           {"error": f"eBay returned HTTP {code}"})
                return
            except requests.RequestException as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            # The cache holds the whole consignment because ranking needs it,
            # but a few hundred cards is a slow parse and a slow paint on a
            # phone. Send the top slice; "show all" asks for limit=0.
            items = data["items"]
            try:
                limit = int(params.get("limit", ["60"])[0])
            except ValueError:
                limit = 60
            # Attach everything the detail pass has read so far across the whole
            # consignment: filtering and sorting on the AG class needs the class
            # present before the slice, not after it.
            apply_details(items)
            auth_counts = authguard.class_counts(
                i.get("auth_arbitrage_class") for i in items)
            view = params.get("view", [""])[0]
            if view == "saved":
                items = [i for i in items if i.get("saved")]
            elif view == "disliked":
                items = [i for i in items if i.get("disliked")]
            klass = params.get("class", [""])[0].upper()
            if klass in authguard.CLASSES:
                items = [i for i in items
                         if i.get("auth_arbitrage_class") == klass]
            if params.get("sort", [""])[0] == "auth":
                # AG_ARBITRAGE first, then taste inside each bucket. Lots the
                # detail pass has not reached yet have no class, and sort last.
                rank = {name: n for n, name in enumerate(authguard.CLASSES)}
                items = sorted(items, key=lambda i: (
                    rank.get(i.get("auth_arbitrage_class"), len(rank)),
                    -i["taste"], i["ends"] or "9999"))
            shown = items[:limit] if limit > 0 else items
            # Condition text arrives on a background pass; the page asks again
            # while `details_pending` is non-zero and patches the chips in.
            pending = apply_details(shown)
            if pending:
                # Always the taste-ranked full list, never the filtered view:
                # which lots are worth a getItem is not the filter's business.
                start_detail_pass(ebay_client(), data["items"], top=max(limit, 60))
            self._json(HTTPStatus.OK, {
                "updated": _now_str(),
                "seller": RARITIES_SELLER,
                "store": RARITIES_STORE_URL,
                "cached_secs": data["cached_secs"],
                "total": len(items),
                "saved_count": sum(1 for i in data["items"] if i.get("saved")),
                "disliked_count": sum(1 for i in data["items"] if i.get("disliked")),
                "details_pending": pending,
                "auth_counts": auth_counts,
                "auth_read": sum(auth_counts.values()),
                "consignment_total": len(data["items"]),
                "items": shown,
            })
            return
        if parsed.path == "/api/item":
            params = parse_qs(parsed.query)
            ref = (params.get("id", [""])[0] or params.get("url", [""])[0])
            try:
                item = fetch_item(ref)
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                self._json(HTTPStatus.BAD_GATEWAY, {"error": (
                    "eBay has no live record of that listing — ended listings "
                    "drop out of the Browse API" if code == 404
                    else f"eBay returned HTTP {code}")})
                return
            except requests.RequestException as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            # raw=1 hands back eBay's response untouched, nothing of ours added.
            if params.get("raw", [""])[0] in ("1", "true", "yes"):
                self._json(HTTPStatus.OK, item)
            else:
                self._json(HTTPStatus.OK, {"summary": item_summary(item),
                                           "item": item})
            return
        if parsed.path == "/manifest.json":
            self._json(HTTPStatus.OK, {
                "name": "GemHunter",
                "short_name": "GemHunter",
                "display": "standalone",
                "start_url": "/",
                "theme_color": "#08111f",
                "background_color": "#08111f",
            })
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        item_id = str(body.get("item_id", ""))
        if not item_id:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "missing item_id"})
            return
        if self.path != "/api/rarities/vote":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            vote = int(body.get("vote", 0))
        except (TypeError, ValueError):
            vote = 0
        vote = max(-1, min(1, vote))
        storage = Storage(self.db_path)
        try:
            ok = storage.set_rarities_vote(item_id, vote,
                                           str(body.get("title", ""))[:300])
        finally:
            storage.close()
        # The ranking is learned from these votes, so it must be recomputed.
        _invalidate_rarities()
        self._json(HTTPStatus.OK, {"ok": ok, "vote": vote})

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="GemHunter private mobile app")
    parser.add_argument("--db", default="gemhunter.db")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[web] serving GemHunter on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
