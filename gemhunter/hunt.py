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
            window = text[max(0, m.start() - 60): m.end() + 60]
            if not CARD_WORDS.search(window):
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


def parse_signals(title: str, description: str, aspects: dict, preset: dict) -> dict:
    texts = [("title", title or ""), ("description", description or "")]
    out: dict = {"card": None, "warranty_months": None, "full_set": False,
                 "full_set_evidence": [], "positive": [], "negative": []}

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
    lo, hi = float(preset["benchmark_low"]), float(preset["benchmark_high"])
    if total_price <= lo:
        frac = 1.0
    elif total_price >= hi:
        frac = 0.0
    else:
        frac = (hi - total_price) / (hi - lo)
    note = f"${total_price:,.0f} vs ${lo:,.0f}–${hi:,.0f}"
    if total_price < float(preset["deal_floor"]):
        note += " · under the deal floor: deal OR suspicious"
    parts.append({"label": "Price vs market", "pts": w.get("price", 0) * frac, "note": note})

    sig = item.get("signals") or {}
    months = sig.get("warranty_months")
    full_months = int(preset.get("warranty_years") or 5) * 12
    parts.append({"label": "Warranty left",
                  "pts": w.get("warranty", 0) * min(1.0, (months or 0) / full_months),
                  "note": (f"{months // 12}y {months % 12}m" if months is not None
                           else "no card date found" if item.get("detail") else
                           "description not read yet")})
    parts.append({"label": "Full set", "pts": w.get("full_set", 0) if sig.get("full_set") else 0,
                  "note": "box & papers" if sig.get("full_set") else "not stated"})
    pos = [s["label"] for s in sig.get("positive") or []]
    neg = [s["label"] for s in sig.get("negative") or []]
    parts.append({"label": "Unpolished / unworn", "pts": w.get("unpolished", 0) if pos else 0,
                  "note": ", ".join(pos) or "not stated"})
    if neg:
        parts.append({"label": "Polished / not original", "pts": w.get("polished", 0),
                      "note": ", ".join(neg)})
    parts.append({"label": "Authenticity Guarantee",
                  "pts": w.get("ag", 0) if item.get("ag") else 0,
                  "note": "eligible" if item.get("ag") else "not eligible"})
    sp, snote = seller_points(item, w.get("seller", 0))
    parts.append({"label": "Seller", "pts": sp, "note": snote})
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
    it["deal_or_suspicious"] = total < float(preset["deal_floor"])
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
