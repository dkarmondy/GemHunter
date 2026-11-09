"""Reference Hunt: chase one exact reference across eBay and rank what turns up.

Driven entirely by presets/hunts.yaml — a preset names the queries, the
title patterns that rule a listing in or out, the price band, and the market
benchmark the score is measured against. The first preset is the Submariner
Date 126610LN; adding another reference is a YAML edit, not a code change.

Cost per refresh: one search call per query (two if a query runs past 200
hits), cached for `ttl_minutes`. The description parse needs a getItem per
listing; those run on a background pass, are capped at `detail_top`, and are
kept for the life of the process because a listing's description doesn't
change. Same shape as the National Rarities detail pass.
"""

from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from . import authguard
from .ebay import EbayClient

PRESETS_PATH = Path(__file__).resolve().parent.parent / "presets" / "hunts.yaml"
FOR_PARTS = authguard.FOR_PARTS_CONDITION_ID


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def _compile(patterns) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in (patterns or [])]


def load_presets(path: Path | str = PRESETS_PATH) -> list[dict]:
    """Every preset with the defaults folded in and its patterns compiled."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    out = []
    for p in raw.get("presets") or []:
        merged = {**defaults, **p}
        merged["weights"] = {**(defaults.get("weights") or {}), **(p.get("weights") or {})}
        merged["_require"] = _compile(merged.get("require"))
        merged["_exclude"] = _compile(list(defaults.get("exclude_common") or [])
                                      + list(p.get("exclude") or []))
        merged["_parts"] = _compile(merged.get("parts_words"))
        merged["id"] = str(merged["id"])
        out.append(merged)
    return out


def public_preset(p: dict) -> dict:
    """What the page needs to know about a preset (no compiled regexes)."""
    keys = ("id", "name", "blurb", "queries", "price_min", "price_max",
            "benchmark_low", "benchmark_high", "deal_floor", "weights",
            "include_parts_default", "international_default", "home_country",
            "negotiable_days", "ttl_minutes")
    return {k: p.get(k) for k in keys}


def title_verdict(title: str, preset: dict) -> str:
    """'' if the title is the watch, else the pattern that ruled it out."""
    t = title or ""
    for rx in preset["_require"]:
        if not rx.search(t):
            return f"missing {rx.pattern}"
    for rx in preset["_exclude"]:
        if rx.search(t):
            return rx.pattern
    return ""


# ---------------------------------------------------------------------------
# Parsing title + description (best-effort, always showing its evidence)
# ---------------------------------------------------------------------------

CARD_WORDS = re.compile(
    r"(?i)\b(card|warranty|guarantee|papers|purchased|bought|dated|"
    r"sold\s+new|ad\s|authorized\s+dealer)")
# A service at Rolex (RSC) comes with its own card and a 2-year service
# warranty. Its date says when the watch was serviced, not when it was sold,
# so a year whose nearest keyword is one of these is not the warranty card.
SERVICE_WORDS = re.compile(
    r"(?i)\b(servic\w*|overhaul\w*|rsc|maintenance|maintained)\b")
YEAR_RE = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"], start=1)}
MONTH_NAME_RE = re.compile(
    r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+"
    r"(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?(19[89]\d|20[0-3]\d)\b")
NUM_DATE_RE = re.compile(r"\b(0?[1-9]|1[0-2])\s*[/\-.]\s*(?:\d{1,2}\s*[/\-.]\s*)?"
                         r"(19[89]\d|20[0-3]\d)\b")

FULL_SET_RES = _compile([
    r"box\s*(?:&|and|\+|/|w/?)\s*papers",
    r"\bb\s*&\s*p\b",
    r"\bfull\s+set\b",
    r"\bcomplete\s+set\b",
])
POSITIVE_RES = [(re.compile(p, re.I), label) for p, label in [
    (r"\bunworn\b", "unworn"),
    (r"\bstickers?\b", "stickers"),
    (r"\bunpolished\b", "unpolished"),
    (r"\bnever\s+(?:been\s+)?polished\b", "never polished"),
    (r"\bnot\s+(?:been\s+)?polished\b", "not polished"),
]]
NEGATIVE_RES = [(re.compile(p, re.I), label) for p, label in [
    (r"\bpolished\b", "polished"),
    (r"\brefinished\b", "refinished"),
    (r"\baftermarket\b", "aftermarket"),
    (r"\bservice\s+dial\b", "service dial"),
]]


# "hasn't been polished", "never polished", and the spec line "Case Finish:
# Polished" (a description of brushed/polished surfaces, not of a refinish)
# all contain the word without being the bad news.
NEGATED_RE = re.compile(
    r"(?i)(?:\b(?:not|never|no|without|hasn.?t|has\s+not|un)|finish:?)\W*"
    r"(?:\w+\W+){0,2}$")


def _negated(text: str, start: int) -> bool:
    return bool(NEGATED_RE.search(text[max(0, start - 30):start]))


def _snippet(text: str, start: int, end: int, pad: int = 45) -> str:
    a, b = max(0, start - pad), min(len(text), end + pad)
    s = re.sub(r"\s+", " ", text[a:b]).strip()
    return ("…" if a else "") + s + ("…" if b < len(text) else "")


def _is_card_year(text: str, start: int, end: int, reach: int = 60) -> bool:
    """True if the keyword nearest this year is a sale/card word, not a service one.

    "Card dated 2021, serviced by Rolex 2025 with the service card": 2021 sits
    next to "dated", 2025 next to "Rolex"/"service", so only 2021 counts.
    """
    a = max(0, start - reach)
    window = text[a:end + reach]
    best = None   # (distance, is_service)
    for rx, service in ((CARD_WORDS, False), (SERVICE_WORDS, True)):
        for k in rx.finditer(window):
            ks, ke = a + k.start(), a + k.end()
            dist = start - ke if ke <= start else ks - end if ks >= end else 0
            # On a tie the service word wins: "service warranty card 2025".
            if best is None or dist < best[0] or (dist == best[0] and service):
                best = (dist, service)
    return best is not None and not best[1]


def _card_date(texts: list[tuple[str, str]], preset: dict):
    """(date, approximate?, where, snippet) for the most plausible card date."""
    lo, hi = int(preset.get("min_card_year") or 1990), int(preset.get("max_card_year") or 2100)
    best = None
    for where, text in texts:
        if not text:
            continue
        for m in YEAR_RE.finditer(text):
            year = int(m.group(1))
            if not lo <= year <= hi:
                continue
            if not _is_card_year(text, m.start(), m.end()):
                continue
            # A month next to the year sharpens the warranty clock.
            month, approx = 7, True
            around = text[max(0, m.start() - 25): m.end()]
            for rx in (MONTH_NAME_RE, NUM_DATE_RE):
                mm = None
                for mm in rx.finditer(around):
                    pass
                if mm and int(mm.group(2)) == year:
                    g = mm.group(1).lower()
                    month = MONTHS.get(g[:3]) if g[:3] in MONTHS else int(g)
                    approx = False
                    break
            cand = (date(year, month, 1), approx, where, _snippet(text, m.start(), m.end()))
            # Newest plausible date wins: older years in a description are
            # usually "the model launched in 2020", not this watch's card.
            if best is None or cand[0] > best[0]:
                best = cand
        if best:
            break   # title beats description; description beats nothing
    return best


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


# Bracelet vs strap. For a Rolex the bracelet is half of what is being bought,
# so a watch that comes on a strap with no bracelet in sight is a lesser watch.
# A strap *as well as* the bracelet is fine.
BRACELET_MISSING_RE = re.compile(
    r"(?i)\bno\s+(?:original\s+)?(?:bracelet|band)\b"
    r"|\bwithout\s+(?:the\s+|its\s+|an?\s+)?(?:original\s+)?(?:bracelet|band)\b"
    r"|\b(?:bracelet|band)\s+(?:is\s+)?not\s+included\b"
    r"|\bmissing\s+(?:the\s+)?(?:original\s+)?bracelet\b"
    r"|\b(?:watch\s+)?head\s+only\b|\bwatch\s+head\b")
BRACELET_RE = re.compile(r"(?i)\b(?:oyster\s+)?bracelet\b")
STRAP_RE = re.compile(
    r"(?i)\b(?:rubber|leather|nato|fabric|canvas|silicone|aftermarket|everest|"
    r"oysterflex|oyster\s+flex|custom)\s+(?:watch\s+)?(?:strap|band)\b|\bon\s+(?:a\s+)?strap\b"
    r"|\bstrap\b|\b(?:rubber|leather|nato|oysterflex)\b")
STEEL_BAND_RE = re.compile(r"(?i)stainless|oystersteel|\bsteel\b|bracelet")


def bracelet_status(texts: list[tuple[str, str]], aspects: dict) -> dict:
    """{'status': confirmed|missing|unknown, 'label', 'where', 'snippet'}."""
    for where, text in texts:
        m = BRACELET_MISSING_RE.search(text)
        if m:
            return {"status": "missing", "label": "no bracelet", "where": where,
                    "snippet": _snippet(text, m.start(), m.end())}
    for where, text in texts:
        for m in BRACELET_RE.finditer(text):
            if not _negated(text, m.start()):
                return {"status": "confirmed", "label": "bracelet", "where": where,
                        "snippet": _snippet(text, m.start(), m.end())}
    for k, v in aspects.items():
        if re.search(r"(?i)band|strap|bracelet", k or "") and STEEL_BAND_RE.search(str(v)):
            return {"status": "confirmed", "label": "bracelet", "where": "item specifics",
                    "snippet": f"{k}: {v}"}
    for where, text in texts:
        m = STRAP_RE.search(text)
        if m:
            return {"status": "missing", "label": "strap, no bracelet mentioned",
                    "where": where, "snippet": _snippet(text, m.start(), m.end())}
    return {"status": "unknown", "label": "", "where": "", "snippet": ""}


def parse_signals(title: str, description: str, aspects: dict, preset: dict) -> dict:
    texts = [("title", title or ""), ("description", description or "")]
    out: dict = {"card": None, "warranty_months": None, "full_set": False,
                 "full_set_evidence": [], "positive": [], "negative": [],
                 "bracelet": None}
    if preset.get("bracelet_primary"):
        out["bracelet"] = bracelet_status(texts, aspects)

    card = _card_date(texts, preset)
    if card:
        d, approx, where, snip = card
        expiry = date(d.year + int(preset.get("warranty_years") or 5), d.month, 1)
        left = max(0, _months_between(date.today(), expiry))
        out["card"] = {"year": d.year, "month": None if approx else d.month,
                       "approx": approx, "where": where, "snippet": snip}
        out["warranty_months"] = left

    for where, text in texts:
        for rx in FULL_SET_RES:
            m = rx.search(text)
            if m:
                out["full_set"] = True
                out["full_set_evidence"].append(
                    {"where": where, "snippet": _snippet(text, m.start(), m.end())})
                break
    box = next((v for k, v in aspects.items()
                if re.search(r"(?i)original\s+box|box/packaging", k or "")), None)
    papers = next((v for k, v in aspects.items()
                   if re.search(r"(?i)with\s+papers|papers/coa", k or "")), None)
    if str(box).lower() == "yes" and str(papers).lower() == "yes":
        out["full_set"] = True
        out["full_set_evidence"].append(
            {"where": "item specifics", "snippet": f"Box: {box} · Papers: {papers}"})

    for key, res in (("positive", POSITIVE_RES), ("negative", NEGATIVE_RES)):
        seen = set()
        for where, text in texts:
            for rx, label in res:
                m = next((m for m in rx.finditer(text)
                          if key == "positive" or not _negated(text, m.start())), None)
                if m and label not in seen:
                    seen.add(label)
                    out[key].append({"label": label, "where": where,
                                     "snippet": _snippet(text, m.start(), m.end())})
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# eBay's condition grade, as a starting point before the description speaks.
CONDITION_GRADE = {1000: 1.0, 1500: 0.9, 1750: 0.6, 2990: 0.7, 3000: 0.5,
                   3010: 0.25, 7000: 0.0}


# Off a strap with no bracelet is worse than a polished case: half the
# condition points go. Only applies where the preset sets bracelet_primary.
BRACELET_MISSING_PENALTY = 0.5


def condition_fraction(item: dict) -> tuple[float, str]:
    """0–1: the listing's grade, lifted by unworn/unpolished, cut by polished."""
    sig = item.get("signals") or {}
    grade = CONDITION_GRADE.get(item.get("condition_id"), 0.4)
    pos = [s["label"] for s in sig.get("positive") or []]
    neg = [s["label"] for s in sig.get("negative") or []]
    br = (sig.get("bracelet") or {}).get("status")
    frac = (grade + (0.3 if pos else 0) - (0.4 if neg else 0)
            - (BRACELET_MISSING_PENALTY if br == "missing" else 0))
    bits = [item.get("condition") or "condition unknown"]
    if pos:
        bits.append("+ " + ", ".join(pos))
    if neg:
        bits.append("− " + ", ".join(neg))
    if br == "missing":
        bits.append("− no bracelet")
    elif br == "confirmed":
        bits.append("+ bracelet")
    return max(0.0, min(1.0, frac)), " ".join(bits)


def seller_points(item: dict, weight: float) -> tuple[float, str]:
    """Volume and rating multiplied, so a seller needs both to score.

    eBay's feedback score is the count of rated sales, the nearest thing the
    Browse API gives to "how much has this seller sold". It is read on a log
    scale: 10 ratings ≈ a quarter, 100 half, 1,000 three quarters, 10,000 all.
    The rating gates it: 100% keeps all of it, 99% two thirds, 98% a third,
    97% and under nothing, however big the seller. Top Rated Plus on the
    listing adds the last 15%.
    """
    fb = item.get("feedback_score") or 0
    pct = item.get("feedback_pct") or 0
    volume = min(1.0, math.log10(fb + 1) / 4)
    rating = max(0.0, min(1.0, (pct - 97) / 3))
    top = 1.0 if item.get("top_rated") else 0.0
    pts = weight * (0.85 * volume * rating + 0.15 * top)
    note = f"{fb:,} ratings · {pct:g}%" + (" · Top Rated Plus" if top else "")
    return pts, note


def score(item: dict, preset: dict) -> tuple[float, list[dict]]:
    w = preset["weights"]
    parts: list[dict] = []
    total_price = item["price"] + (item["shipping"] or 0)
    # No bracelet means buying one: priced in before the market comparison.
    missing_bracelet = ((item.get("signals") or {}).get("bracelet") or {}).get("status") == "missing"
    bracelet_cost = float(preset.get("bracelet_cost") or 0) if missing_bracelet else 0.0
    total_price += bracelet_cost
    lo, hi = float(preset["benchmark_low"]), float(preset["benchmark_high"])
    # A live auction's current bid is not what it will sell for. Until the bid
    # passes the middle of the market band, price it there: at most half the
    # price points, never a "steal" for a $3k opening bid.
    projected = item.get("is_auction") and total_price < (lo + hi) / 2
    priced_at = (lo + hi) / 2 if projected else total_price
    if priced_at <= lo:
        frac = 1.0
    elif priced_at >= hi:
        frac = 0.0
    else:
        frac = (hi - priced_at) / (hi - lo)
    note = f"${total_price:,.0f} vs ${lo:,.0f}–${hi:,.0f}"
    if bracelet_cost:
        note += f" (incl. ${bracelet_cost:,.0f} for a bracelet)"
    if projected:
        note += f" · auction: scored at the ${priced_at:,.0f} market middle until the bid passes it"
    elif total_price < float(preset["deal_floor"]):
        # Too cheap to trust: no price points until a look at it says otherwise.
        frac = 0.0
        note += " · under the deal floor: deal OR suspicious, no price points"
    parts.append({"label": "Price vs market", "pts": w.get("price", 0) * frac, "note": note})

    sig = item.get("signals") or {}
    months = sig.get("warranty_months")
    full_months = int(preset.get("warranty_years") or 5) * 12
    parts.append({"label": "Warranty left",
                  "pts": w.get("warranty", 0) * min(1.0, (months or 0) / full_months),
                  "note": (f"{months // 12}y {months % 12}m" if months is not None
                           else "no card date found" if item.get("detail") else
                           "description not read yet")})
    parts.append({"label": "Box & papers", "pts": w.get("full_set", 0) if sig.get("full_set") else 0,
                  "note": "full set" if sig.get("full_set") else "not stated"})
    cfrac, cnote = condition_fraction(item)
    parts.append({"label": "Condition", "pts": w.get("condition", 0) * cfrac, "note": cnote})
    parts.append({"label": "Authenticity Guarantee",
                  "pts": w.get("ag", 0) if item.get("ag") else 0,
                  "note": "eligible" if item.get("ag") else "not eligible"})
    sp, snote = seller_points(item, w.get("seller", 0))
    parts.append({"label": "Seller", "pts": sp, "note": snote})
    order = ["Price vs market", "Condition", "Box & papers", "Seller",
             "Warranty left", "Authenticity Guarantee"]
    parts.sort(key=lambda p: order.index(p["label"]))
    for p in parts:
        p["pts"] = round(p["pts"], 1)
    return round(sum(p["pts"] for p in parts), 1), parts


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _money(m) -> float:
    try:
        return float((m or {}).get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_ts(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def summary_item(s: dict, preset: dict) -> dict:
    options = s.get("buyingOptions") or []
    is_auction = "AUCTION" in options
    price = _money(s.get("currentBidPrice") if is_auction else s.get("price")) \
        or _money(s.get("price"))
    ship_opts = [o for o in (s.get("shippingOptions") or []) if isinstance(o, dict)]
    ship = None
    if ship_opts and isinstance(ship_opts[0].get("shippingCost"), dict):
        ship = min(_money(o.get("shippingCost")) for o in ship_opts
                   if isinstance(o.get("shippingCost"), dict))
    loc = s.get("itemLocation") or {}
    seller = s.get("seller") or {}
    auth = authguard.classify(s)
    try:
        pct = float(seller.get("feedbackPercentage") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    title = s.get("title") or ""
    country = (loc.get("country") or "").upper()
    img = (s.get("image") or {}).get("imageUrl", "")
    return {
        "id": s.get("itemId", ""),
        "title": title,
        "url": s.get("itemWebUrl", ""),
        "image": re.sub(r"s-l\d+", "s-l500", img) if img else "",
        "price": price,
        "currency": (s.get("price") or {}).get("currency", "USD"),
        "shipping": ship,
        "buying": options,
        "is_auction": is_auction,
        "bids": s.get("bidCount"),
        "condition": s.get("condition") or "",
        "condition_id": auth["condition_id"],
        "country": country,
        "location": ", ".join(x for x in (loc.get("city"), loc.get("stateOrProvince"),
                                          country) if x),
        "international": bool(country) and country != preset.get("home_country", "US"),
        "seller": seller.get("username", ""),
        "feedback_score": int(seller.get("feedbackScore") or 0),
        "feedback_pct": pct,
        "top_rated": bool(s.get("topRatedBuyingExperience")),
        "listed": s.get("itemCreationDate") or s.get("itemOriginDate") or "",
        "ends": s.get("itemEndDate") or "",
        "ag": auth["has_authenticity_guarantee"],
        "auth_class": auth["auth_arbitrage_class"],
        "as_is_excerpt": "",
        "parts": auth["condition_id"] == FOR_PARTS
                 or any(rx.search(title) for rx in preset["_parts"]),
        "sold_qty": None,
        "availability": None,
        "detail": False,
        "signals": parse_signals(title, "", {}, preset),
    }


_lock = threading.Lock()
_cache: dict[str, dict] = {}          # preset id -> {"ts", "items", "excluded", "errors"}
_details: dict[str, dict] = {}        # item id -> getItem-derived fields
_details_running: set[str] = set()    # preset ids with a pass in flight


def _search(client: EbayClient, preset: dict) -> tuple[list[dict], list[dict], list[str]]:
    conds = list(preset.get("conditions") or [])
    if preset.get("parts_condition"):
        conds.append(preset["parts_condition"])   # fetched always, hidden by default
    filters = [f"price:[{preset['price_min']}..{preset['price_max']}]",
               "priceCurrency:USD"]
    if conds:
        filters.append("conditionIds:{" + "|".join(str(c) for c in conds) + "}")
    aspect = (f"categoryId:{preset['category']},Brand:{{{preset['brand']}}}"
              if preset.get("brand") else "")
    seen: dict[str, dict] = {}
    excluded: list[dict] = []
    errors: list[str] = []
    for q in preset["queries"]:
        try:
            batch = client.search_summaries(q, preset["category"], filters, aspect)
        except Exception as exc:     # one bad query shouldn't sink the others
            errors.append(f"{q}: {exc}")
            continue
        for s in batch:
            iid = s.get("itemId")
            if not iid or iid in seen or any(e["id"] == iid for e in excluded):
                continue
            why = title_verdict(s.get("title", ""), preset)
            if why:
                excluded.append({"id": iid, "title": s.get("title", ""), "why": why})
                continue
            seen[iid] = summary_item(s, preset)
    if errors and not seen:
        raise RuntimeError("; ".join(errors))
    return list(seen.values()), excluded, errors


def _load_detail(client: EbayClient, item_id: str, preset: dict) -> dict:
    try:
        d = client.get_item(item_id)
    except Exception:
        return {"ok": False}
    desc = authguard.strip_html(d.get("description") or "")
    desc = authguard._trim_boilerplate(desc)   # drop the shipping/grade legend
    text = "\n".join(x for x in (d.get("shortDescription") or "",
                                 authguard.strip_html(d.get("conditionDescription") or ""),
                                 desc) if x)
    aspects = {a.get("name"): a.get("value")
               for a in (d.get("localizedAspects") or []) if a.get("name")}
    avail = (d.get("estimatedAvailabilities") or [{}])[0]
    auth = authguard.classify(d)
    return {
        "ok": True,
        "text": text[:20000],
        "aspects": aspects,
        "listed": d.get("itemCreationDate") or "",
        "sold_qty": avail.get("estimatedSoldQuantity"),
        "availability": avail.get("estimatedAvailabilityStatus"),
        "ag": auth["has_authenticity_guarantee"],
        "auth_class": auth["auth_arbitrage_class"],
        "as_is_excerpt": auth["as_is_excerpt"],
    }


def _start_detail_pass(client: EbayClient, preset: dict, items: list[dict]) -> None:
    pid = preset["id"]
    top = int(preset.get("detail_top") or 80)
    with _lock:
        if pid in _details_running:
            return
        todo = [i["id"] for i in items[:top] if i["id"] not in _details]
        if not todo:
            return
        _details_running.add(pid)

    def work():
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                done = list(pool.map(lambda i: (i, _load_detail(client, i, preset)), todo))
            with _lock:
                _details.update({i: d for i, d in done if d.get("ok")})
        finally:
            with _lock:
                _details_running.discard(pid)

    threading.Thread(target=work, daemon=True).start()


def _decorate(item: dict, preset: dict, now: datetime) -> dict:
    it = dict(item)
    d = _details.get(it["id"])
    if d:
        it["detail"] = True
        it["listed"] = it["listed"] or d["listed"]
        it["sold_qty"], it["availability"] = d["sold_qty"], d["availability"]
        it["ag"], it["auth_class"] = d["ag"], d["auth_class"]
        it["as_is_excerpt"] = d["as_is_excerpt"]
        it["signals"] = parse_signals(it["title"], d["text"], d["aspects"], preset)
    listed = _parse_ts(it["listed"])
    it["run_days"] = int((now - listed).total_seconds() // 86400) if listed else None
    it["negotiable"] = (it["run_days"] or 0) >= int(preset.get("negotiable_days") or 30)
    total = it["price"] + (it["shipping"] or 0)
    # A low bid on a running auction is normal, not a red flag.
    it["deal_or_suspicious"] = (not it.get("is_auction")
                                and total < float(preset["deal_floor"]))
    it["score"], it["breakdown"] = score(it, preset)
    return it


def fetch_hunt(client: EbayClient, preset: dict, fresh: bool = False) -> dict:
    pid = preset["id"]
    ttl = int(preset.get("ttl_minutes") or 15) * 60
    with _lock:
        c = _cache.get(pid)
        stale = not c or fresh or time.time() - c["ts"] > ttl
    if stale:
        items, excluded, errors = _search(client, preset)
        with _lock:
            c = _cache[pid] = {"ts": time.time(), "items": items,
                               "excluded": excluded, "errors": errors}
    now = datetime.now(timezone.utc)
    with _lock:
        items = [_decorate(i, preset, now) for i in c["items"]]
    items.sort(key=lambda i: -i["score"])
    pending = sum(1 for i in items[:int(preset.get("detail_top") or 80)]
                  if not i["detail"])
    if pending:
        _start_detail_pass(client, preset, items)
    return {
        "fetched_at": c["ts"],
        "cached_secs": int(time.time() - c["ts"]),
        "items": items,
        "excluded": c["excluded"],
        "errors": c["errors"],
        "details_pending": pending,
    }
