"""Wild Cards: watches nobody is searching for, found without naming them.

Every other search in this app starts from a name: a seller, a reference. That
is the road everyone else is on, and it ends at a Speedmaster. This one starts
from what a watch *is*:

1. The net (presets/wildcard.yaml → queries) is cast on attributes only —
   complications, craft words and their misspellings, movement suppliers,
   edition sizes — across the whole Wristwatches category.
2. The gate throws out every maker the market already knows, by name where the
   title gives it away and by measurement where it doesn't: eBay is asked how
   many live listings carry that Brand, and a maker with hundreds is not a
   wild card whoever they are.
3. What survives is scored on substance (what the watch has) and obscurity
   (how few of that maker are for sale), with the text each point came from.

These watches surface a few times a year, so this is a monitor rather than a
search: every sweep is written to SQLite, the tab shows the best of what is
live now, and the digest pushes when something new clears `alert_score`. The
makers it finds accumulate into a private list, which is the one list of
unknown watches that searching for does not make known.

Cost per sweep: two searches per query (Buy It Now and auction), up to
`pages` each; one getItem per listing never read before, capped at
`detail_per_sweep`; one count per maker not measured in `brand_ttl_days`.
A listing's description is read once and kept, so a quiet week costs little
more than the searches.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from . import authguard
from . import hunt
from .config import load_config
from .ebay import EbayClient
from .knowledge import PROJECT_KEYWORDS
from .notify import Notifier
from .rarities import case_mm
from .storage import Storage

CONFIG_PATH = Path(__file__).resolve().parent.parent / "presets" / "wildcard.yaml"

BUY_NOW = "buyingOptions:{FIXED_PRICE|BEST_OFFER}"
AUCTION = "buyingOptions:{AUCTION}"
ANY_SALE = "buyingOptions:{FIXED_PRICE|BEST_OFFER|AUCTION}"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _word_re(words) -> re.Pattern | None:
    """One whole-word alternation; None when the list is empty."""
    words = [str(w).strip() for w in (words or []) if str(w).strip()]
    if not words:
        return None
    # \b fails beside punctuation ("women's", "mb&f"), so the boundary is
    # "not a letter or digit" on either side.
    body = "|".join(re.escape(w).replace(r"\ ", r"\s+")
                    for w in sorted(words, key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9])(?:" + body + r")(?![a-z0-9])", re.I)


def load_config_file(path: Path | str = CONFIG_PATH) -> dict:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = dict(raw.get("settings") or {})
    cfg["weights"] = dict(raw.get("weights") or {})
    cfg["queries"] = [q if isinstance(q, dict) else {"q": str(q)}
                      for q in (raw.get("queries") or [])]
    cfg["known_brands"] = [str(b).lower() for b in (raw.get("known_brands") or [])]
    cfg["blocked_brands"] = [str(b).lower() for b in (raw.get("blocked_brands") or [])]
    cfg["blocked_countries"] = [str(c).lower() for c in (raw.get("blocked_countries") or [])]
    cfg["min_year"] = int(raw.get("min_year") or 0)
    cfg["_known"] = _word_re(cfg["known_brands"])
    cfg["_blocked"] = _word_re(cfg["blocked_brands"])
    cfg["_blocked_words"] = _word_re(raw.get("blocked_words"))
    # hunt.summary_item wants these two off a preset.
    cfg["_parts"] = [re.compile(r"(?i)for parts|\bparts\b|not working|non.?running")]
    cfg.setdefault("home_country", "US")
    return cfg


def public_config(cfg: dict) -> dict:
    keys = ("price_min", "price_max", "known_over", "min_substance", "show",
            "ttl_hours", "alert_score", "weights")
    out = {k: cfg.get(k) for k in keys}
    out["queries"] = len(cfg["queries"])
    return out


# ---------------------------------------------------------------------------
# The title gate: free, runs on every search hit.
# ---------------------------------------------------------------------------

def title_verdict(title: str, cfg: dict) -> str:
    """'' to keep the listing, else why it is not a wild card."""
    t = title or ""
    for key, why in (("_blocked_words", "blocked word"),
                     ("_blocked", "blocked maker"),
                     ("_known", "known maker")):
        rx = cfg.get(key)
        m = rx.search(t) if rx else None
        if m:
            return f"{why}: {m.group(0).lower()}"
    return ""


# ---------------------------------------------------------------------------
# Substance: what the watch has, each with the text it was read from.
#
# (label, points, pattern[, needs]). A group's points are summed and capped at
# its weight. `needs` is a second pattern that must appear somewhere in the
# listing before the first counts — a tourbillon under $15k is only worth
# points when the listing also says Swiss.
# ---------------------------------------------------------------------------

_SWISS = r"swiss|switzerland|suisse|gen[eè]ve|geneva"

COMPLICATIONS = [
    ("Split-seconds chronograph", 16,
     r"rattrapante|split[\s-]*seconds?|doppel[\s-]*chrono\w*|double[\s-]+chronograph"),
    ("Perpetual calendar", 16,
     r"perpetual\s+calend[ae]r|quanti[eè]me\s+perp[eé]tuel"),
    ("Repeater", 16,
     r"(?:minute|quarter|five[\s-]minute)[\s-]+repeater|r[eé]p[eé]tition\s+minutes?"),
    ("Equation of time", 10, r"equation\s+of\s+time|[eé]quation\s+du\s+temps"),
    ("Tourbillon", 10, r"tourbillon", _SWISS),
    ("Annual calendar", 9, r"annual\s+calend[ae]r|quanti[eè]me\s+annuel"),
    ("Triple calendar", 7,
     r"triple[\s-]+(?:date|calend[ae]r)|(?:complete|full)\s+calend[ae]r"
     r"|day[\s-]*date[\s-]*month|triple[\s-]*date"),
    ("Flyback", 7, r"fly[\s-]*back|retour\s+en\s+vol"),
    ("Jump hour", 7, r"jump(?:ing)?[\s-]+hours?|heures?\s+sautantes?"),
    ("Dead-beat seconds", 7, r"dead[\s-]*beat\s+seconds?|seconde\s+morte"),
    ("Chronograph", 6, r"chronograph\w*|\bchrono\b"),
    ("Regulator", 5, r"\br[eé]gulat(?:or|eur)\b"),
    ("Alarm", 5, r"\balarm\b|\br[eé]veil\b"),
    ("Moonphase", 4, r"moon[\s-]*phases?|phases?\s+de\s+lune"),
    ("Second time zone", 4,
     r"world[\s-]*time[rs]?|\bGMT\b|dual[\s-]+time|second\s+time[\s-]*zone"),
    ("Retrograde", 4, r"retrograde"),
    ("Power reserve display", 3,
     r"power[\s-]+reserve\s+(?:indicator|display|indication)|r[eé]serve\s+de\s+marche"),
    ("Big date", 2, r"big[\s-]+date|grande?\s+date|outsize\s+date"),
]

# Guilloché, as spelled by people who know (guilloché, guilloche) and by people
# who have only heard it said (guiloche, guillioche, gilloche, guilloshe, gioshe).
GUILLOCHE = (r"\bgu?il{1,2}i?o(?:ch|sh)[eé]?\w*|\bgioshe\w*"
             r"|engine[\s-]*turned|flinqu[eé]")

CRAFT = [
    ("Guilloché", 6, GUILLOCHE),
    ("Enamel dial", 6,
     r"grand\s+feu|cloisonn[eé]|champlev[eé]|(?:enamel(?:led)?|[eé]mail)\s+(?:\w+\s+)?dial"
     r"|dial\s+(?:in\s+|of\s+)?enamel"),
    ("Gold rotor", 6,
     r"(?:\d{2}\s*k(?:t|arat)?\.?\s+(?:\w+\s+){0,2}|solid\s+gold\s+|\bgold\s+)"
     r"(?:rotor|oscillating\s+weight|winding\s+mass)"
     r"|rotor\s+(?:in|of|is)\s+(?:solid\s+)?(?:\d{2}\s*k\w*\.?\s+)?gold"),
    ("Hand engraved", 5,
     r"hand[\s-]*engrav\w+|engraved\s+by\s+hand|engraved\s+(?:movement|rotor|dial)"),
    ("Hand made", 4, r"hand[\s-]*(?:made|crafted|finished|built)\b"),
    ("Blued hands", 3,
     r"blued?\s+(?:steel\s+)?hands|(?:heat|flame|hand)[\s-]*blued"),
    ("Movement finishing", 3,
     r"anglage|c[oô]tes\s+de\s+gen[eè]ve|geneva\s+(?:stripes|waves)|perlage"
     r"|hand[\s-]*bevel\w*"),
    ("Chronometer", 3, r"\bCOSC\b|chronom[eè]t(?:er|re)\b"),
    ("Silver dial", 3, r"(?:solid|sterling|massive)\s+silver\s+dial"),
    ("Display back", 2,
     r"(?:display|exhibition|sapphire|see[\s-]*through|glass)[\s-]+(?:case[\s-]*)?back"),
]

MOVEMENT = [
    ("Haute-horlogerie movement maker", 8,
     r"christophe\s+claret|renaud\s*(?:&|et|and)\s*papi|\bagenhor\b|\bchronode\b"
     r"|geneva\s+seal|poin[cç]on\s+de\s+gen[eè]ve"),
    ("Frédéric Piguet", 7,
     r"fr[eé]d[eé]ric\s+piguet|\bf\.\s*piguet\b|piguet\s+(?:cal\w*\.?\s*)?(?:1185|1150)\b"),
    ("Vaucher", 6, r"\bvaucher\b"),
    ("Minerva", 6, r"\bminerva\b"),
    ("El Primero", 6, r"el\s+primero"),
    ("Valjoux column wheel", 6,
     r"\bvaljoux\s+(?:cal\w*\.?\s*)?(?:72|22|23|88|90|92|726|730)\b"),
    ("Lemania", 5, r"\b(?:nouvelle\s+)?lemania\b"),
    ("Venus", 5, r"\bvenus\s+(?:cal\w*\.?\s*)?\d{3}\b"),
    ("La Joux-Perret / Jaquet", 5, r"la\s+joux[\s-]*perret|\bjaquet\b"),
    ("Column wheel", 5, r"column[\s-]*wheel|roue\s+[aà]\s+colonnes"),
    ("Micro-rotor", 5, r"micro[\s-]*rotor"),
    ("Dubois Dépraz", 4, r"dubois[\s-]*d[eé]praz"),
    ("In-house movement", 4,
     r"in[\s-]*house\s+(?:movement|cal\w+)|manufacture\s+(?:movement|cal\w+)"),
    ("Valjoux 7750 family", 2, r"\b(?:valjoux|eta)[\s-]*77\d\d\b|\b775\d\b"),
    ("Peseux / Unitas", 2, r"\b(?:peseux|unitas)\b"),
]

SCARCITY_WORDS = [
    ("One of a kind", 10,
     r"pi[eè]ce\s+unique|unique\s+piece|one[\s-]of[\s-]a[\s-]kind|\bprototype\b"),
    ("Limited edition", 4,
     r"limited\s+(?:edition|series|production|run)|[eé]dition\s+limit[eé]e"
     r"|\bnumbered\b|\bltd\.?\s+ed"),
]

USABILITY = [
    ("Automatic", 2, r"\bautomati[ck]\w*|self[\s-]*winding"),
    ("Serviced", 1, r"\b(?:recently\s+|freshly\s+|just\s+)?(?:serviced|overhauled)\b"),
]


def _compile(table):
    out = []
    for row in table:
        label, pts, pattern = row[0], row[1], row[2]
        needs = re.compile(row[3], re.I) if len(row) > 3 else None
        out.append((label, pts, re.compile(pattern, re.I), needs))
    return out


GROUPS = {
    "complications": _compile(COMPLICATIONS),
    "craft": _compile(CRAFT),
    "movement": _compile(MOVEMENT),
    "scarcity": _compile(SCARCITY_WORDS),
    "usability": _compile(USABILITY),
}

# Edition size. Each pattern's last group is the size of the run.
_EDITION_RES = [re.compile(p, re.I) for p in [
    # "No. 045/250", "#12 of 100", "numbered 7/50", "limited edition 045/250"
    r"(?:n[o°º]\.?|#|number(?:ed)?|limited(?:\s+edition)?|edition|pieces?)\s*[:.]?\s*"
    r"\d{1,4}\s*(?:/|of|out\s+of)\s*(\d{1,4})\b",
    # "limited to 99", "limited edition of 250", "edition of only 50"
    r"(?:limited|edition|s[eé]rie)\b[^.\d]{0,25}?\b(?:to|of)\s+(?:only\s+|just\s+)?"
    r"(?:approx\w*\.?\s+|about\s+|around\s+)?(\d{1,4})\b",
    # "one of 250", "1 of approx 250", "one out of approximately 250"
    r"\b(?:one|1)\s+(?:out\s+)?of\s+(?:only\s+|just\s+)?"
    r"(?:approx\w*\.?\s+|about\s+|around\s+)?(\d{1,4})\b",
    # "only 250 made", "just 99 pieces"
    r"\b(?:only|just)\s+(\d{1,4})\s+(?:\w+\s+){0,2}"
    r"(?:made|produced|pieces|examples|units|built|exist)",
    # "250 pieces worldwide", "99 examples made"
    r"\b(\d{1,4})\s+(?:pieces|examples|units)\s+(?:were\s+|ever\s+)?"
    r"(?:made|produced|worldwide|only|in\s+total)",
]]
# A bare "045/250" with nothing to announce it: titles only, and only when it
# cannot be a date.
_BARE_FRACTION_RE = re.compile(r"(?<![\d/.])(\d{1,3})\s*/\s*(\d{2,4})(?![\d/])")


def _year_like(n: int) -> bool:
    return 1900 <= n <= 2099


def edition_size(texts: list[tuple[str, str]]):
    """(size, where, snippet) for the smallest stated production run, or None."""
    best = None
    for where, text in texts:
        if not text:
            continue
        found = []
        for rx in _EDITION_RES:
            for m in rx.finditer(text):
                found.append((int(m.group(m.lastindex)), m))
        if where == "title":
            for m in _BARE_FRACTION_RE.finditer(text):
                n, of = int(m.group(1)), int(m.group(2))
                if n <= of and of >= 10 and not _year_like(of):
                    found.append((of, m))
        for size, m in found:
            if size < 1 or _year_like(size):
                continue
            if best is None or size < best[0]:
                best = (size, where, hunt._snippet(text, m.start(), m.end()))
        if best:
            break       # the title's word beats the description's
    return best


def edition_points(size: int) -> int:
    if size <= 50:
        return 12
    if size <= 100:
        return 10
    if size <= 300:
        return 8
    if size <= 1000:
        return 5
    return 2


# "no chronograph", "not a limited edition", "without date". hunt._negated is
# not used here: it also treats "finish" as a negation (for "Case Finish:
# Polished"), which would throw out "guilloché finish, hand-blued hands".
_NEGATED_RE = re.compile(
    r"(?i)\b(?:not|never|no|non|without|isn.?t|hasn.?t|lacks?)\W+(?:\w+\W+){0,2}$")


def _negated(text: str, start: int) -> bool:
    return bool(_NEGATED_RE.search(text[max(0, start - 30):start]))


def _aspect_text(aspects: dict) -> str:
    return " · ".join(f"{k}: {v}" for k, v in (aspects or {}).items() if k and v)


def _aspect(aspects: dict, pattern: str) -> str:
    for k, v in (aspects or {}).items():
        if re.search(pattern, k or "", re.I):
            return str(v or "")
    return ""


def parse_substance(title: str, description: str, aspects: dict, cfg: dict) -> list[dict]:
    """Every signal found: {group, label, pts, where, snippet}."""
    specifics = _aspect_text(aspects)
    texts = [("title", title or ""), ("item specifics", specifics),
             ("description", description or "")]
    everything = "\n".join(t for _, t in texts)
    desc_factor = float(cfg.get("description_factor", 0.5))
    out: list[dict] = []
    for group, table in GROUPS.items():
        for label, pts, rx, needs in table:
            if needs and not needs.search(everything):
                continue
            for where, text in texts:
                m = next((m for m in rx.finditer(text)
                          if not _negated(text, m.start())), None)
                if m:
                    out.append({
                        "group": group, "label": label, "where": where,
                        "pts": round(pts * (desc_factor if where == "description" else 1), 1),
                        "snippet": hunt._snippet(text, m.start(), m.end()),
                    })
                    break

    ed = edition_size(texts)
    if ed:
        size, where, snip = ed
        # A stated run replaces the bare words "limited edition".
        out = [s for s in out if s["label"] != "Limited edition"]
        pts = edition_points(size)
        out.append({"group": "scarcity", "label": f"Edition of {size}", "where": where,
                    "pts": round(pts * (desc_factor if where == "description" else 1), 1),
                    "snippet": snip, "edition": size})

    # Usability that needs more than a word match.
    plain = [("title", title or ""), ("description", description or "")]
    br = hunt.bracelet_status(plain, aspects or {})
    if br["status"] == "confirmed":
        out.append({"group": "usability", "label": "Bracelet", "pts": 2,
                    "where": br["where"], "snippet": br["snippet"]})
    size_text = _aspect(aspects, r"case\s+size") or ""
    mm = case_mm(size_text.lower()) or case_mm((title or "").lower())
    if mm is not None and 36 <= mm <= 42:
        out.append({"group": "usability", "label": f"{mm:g}mm", "pts": 2,
                    "where": "item specifics" if size_text else "title",
                    "snippet": size_text or (title or "")})
    features = _aspect(aspects, r"^features$|^complications?$")
    m = re.search(r"(?i)\bdate\b", features) or re.search(r"(?i)\bdate\b", title or "")
    if m:
        out.append({"group": "usability", "label": "Date", "pts": 1,
                    "where": "item specifics" if features and re.search(r"(?i)\bdate\b", features) else "title",
                    "snippet": features or (title or "")})
    full = hunt.parse_signals(title or "", description or "", aspects or {}, {})
    if full["full_set"]:
        ev = full["full_set_evidence"][0]
        out.append({"group": "usability", "label": "Box & papers", "pts": 2,
                    "where": ev["where"], "snippet": ev["snippet"]})
    return out


def title_substance(title: str, cfg: dict) -> float:
    """What the title alone promises. Decides who gets a getItem first."""
    sig = parse_substance(title, "", {}, cfg)
    return sum(s["pts"] for s in sig if s["group"] != "usability")


# ---------------------------------------------------------------------------
# Obscurity: how many of this maker are for sale right now.
# ---------------------------------------------------------------------------

def obscurity_fraction(live: int | None, known_over: int) -> float:
    """1.0 at three or fewer live listings, 0 at `known_over`, log scale between."""
    if live is None:
        return 0.5              # couldn't be measured: neither rewarded nor punished
    live = max(1, live)
    if live <= 3:
        return 1.0
    if live >= known_over:
        return 0.0
    return 1.0 - math.log(live / 3) / math.log(known_over / 3)


def brand_key(brand: str) -> str:
    return re.sub(r"\s+", " ", (brand or "").strip().lower())


def live_count(client: EbayClient, brand: str, category: str) -> int | None:
    """Live Wristwatches listings carrying this Brand. None if eBay won't say."""
    brand = (brand or "").strip()
    if not brand:
        return None
    # The aspect filter has its own punctuation; a name that uses any of it is
    # counted by keyword instead, which over-counts slightly — the safe side.
    if not re.search(r"[|,{}:\\]", brand):
        try:
            n = client.count(category=category, filters=[ANY_SALE],
                             aspect_filter=f"categoryId:{category},Brand:{{{brand}}}")
            return max(1, n)        # the listing we read it from is one
        except Exception:
            pass
    try:
        return max(1, client.count(query=f'"{brand}"', category=category,
                                   filters=[ANY_SALE]))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The detail gate: what only the listing page can say.
# ---------------------------------------------------------------------------

_NO_BRAND = {"", "unbranded", "does not apply", "n/a", "na", "none", "unknown",
             "handmade", "custom", "no brand", "generic", "see description",
             "see photos", "swiss", "swiss made", "vintage"}
_YEAR_RE = re.compile(r"\b(1[89]\d\d|20[0-3]\d)\b")


def detail_verdict(detail: dict, cfg: dict) -> str:
    """'' to keep, else why this listing is out now its page has been read."""
    aspects = detail.get("aspects") or {}
    brand = detail.get("brand") or ""
    if brand_key(brand) in _NO_BRAND:
        return "no maker named"
    # The Brand field is the seller's own answer, so it gets the same gate the
    # title had.
    why = title_verdict(brand, cfg)
    if why:
        return why
    movement = _aspect(aspects, r"^movement$")
    if re.search(r"(?i)quartz|battery|solar|kinetic", movement):
        return "quartz"
    if re.search(r"(?i)women|girl|ladies", _aspect(aspects, r"^department$")):
        return "ladies"
    if re.search(r"(?i)pocket", _aspect(aspects, r"^type$")):
        return "pocket watch"
    country = _aspect(aspects, r"country").strip().lower()
    if country and country in cfg.get("blocked_countries", []):
        return f"made in {country}"
    years = [int(y) for y in _YEAR_RE.findall(_aspect(aspects, r"year\s+manufactured"))]
    if years and cfg.get("min_year") and max(years) < cfg["min_year"]:
        return f"pre-{cfg['min_year']}"
    pct = detail.get("feedback_pct")
    if detail.get("feedback_score") and pct is not None and pct < 90:
        return "seller under 90%"
    return ""


def read_detail(d: dict) -> dict:
    """The parts of a getItem response the scoring needs, small enough to keep."""
    desc = authguard._trim_boilerplate(authguard.strip_html(d.get("description") or ""))
    text = "\n".join(x for x in (d.get("shortDescription") or "",
                                 authguard.strip_html(d.get("conditionDescription") or ""),
                                 desc) if x)
    aspects = {a.get("name"): a.get("value")
               for a in (d.get("localizedAspects") or []) if a.get("name")}
    seller = d.get("seller") or {}
    try:
        pct = float(seller.get("feedbackPercentage") or 0)
    except (TypeError, ValueError):
        pct = None
    auth = authguard.classify(d)
    return {
        "brand": (d.get("brand") or aspects.get("Brand") or "").strip(),
        "text": text[:8000],
        "aspects": aspects,
        "listed": d.get("itemCreationDate") or "",
        "feedback_pct": pct,
        "feedback_score": int(seller.get("feedbackScore") or 0),
        "ag": auth["has_authenticity_guarantee"],
        "as_is_excerpt": auth["as_is_excerpt"],
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

GROUP_LABELS = [("complications", "Complications"), ("obscurity", "Obscurity"),
                ("craft", "Craft"), ("scarcity", "Scarcity"),
                ("movement", "Movement"), ("price", "Price"),
                ("usability", "Usability")]
SUBSTANCE_GROUPS = ("complications", "craft", "movement", "scarcity")


def price_fraction(item: dict, cfg: dict) -> tuple[float, str]:
    total = item["price"] + (item.get("shipping") or 0)
    if item.get("is_auction"):
        # A live bid is not a price; half the points and no more.
        return 0.5, f"auction, ${total:,.0f} bid so far: scored at half"
    lo, hi = float(cfg["price_min"]), float(cfg["price_max"])
    if total <= lo:
        frac = 1.0
    elif total >= hi:
        frac = 0.0
    else:
        frac = 1.0 - math.log(total / lo) / math.log(hi / lo)
    return frac, f"${total:,.0f} in the ${lo:,.0f}–${hi:,.0f} band"


def score(item: dict, signals: list[dict], live: int | None, cfg: dict
          ) -> tuple[float, float, list[dict]]:
    """(score, substance points, breakdown rows)."""
    w = cfg["weights"]
    rows = {}
    for group, _ in GROUP_LABELS:
        mine = [s for s in signals if s["group"] == group]
        if group in ("obscurity", "price"):
            continue
        cap = float(w.get(group, 0))
        raw = sum(s["pts"] for s in mine)
        rows[group] = {"pts": min(cap, raw),
                       "note": ", ".join(s["label"] for s in mine) or "nothing found"}
    frac = obscurity_fraction(live, int(cfg.get("known_over") or 400))
    rows["obscurity"] = {
        "pts": float(w.get("obscurity", 0)) * frac,
        "note": ("maker's live listings could not be counted" if live is None
                 else f"{live:,} live listing{'s' if live != 1 else ''} for this maker")}
    pfrac, pnote = price_fraction(item, cfg)
    rows["price"] = {"pts": float(w.get("price", 0)) * pfrac, "note": pnote}
    breakdown = [{"label": label, "pts": round(rows[g]["pts"], 1), "note": rows[g]["note"]}
                 for g, label in GROUP_LABELS]
    substance = round(sum(rows[g]["pts"] for g in SUBSTANCE_GROUPS), 1)
    return round(sum(r["pts"] for r in breakdown), 1), substance, breakdown


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def _search(client: EbayClient, cfg: dict) -> tuple[dict, list[str], int]:
    """{item id: (summary, [queries that found it])}, errors, search calls made."""
    cat = str(cfg.get("category") or "31387")
    pages = int(cfg.get("pages") or 2)
    band = [f"price:[{cfg['price_min']}..{cfg['price_max']}]", "priceCurrency:USD"]
    cap = [f"price:[..{cfg['price_max']}]", "priceCurrency:USD"]
    jobs = []
    for q in cfg["queries"]:
        extra = ["searchInDescription:true"] if q.get("in_description") else []
        jobs.append((q["q"], [BUY_NOW] + band + extra))
        if q.get("auctions", True):
            jobs.append((q["q"], [AUCTION] + cap + extra))

    def run(job):
        query, filters = job
        try:
            return query, client.search_summaries(query, cat, filters, "", pages), None
        except Exception as exc:
            return query, [], f"{query}: {exc}"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, jobs))
    found: dict[str, tuple[dict, list[str]]] = {}
    errors = []
    for query, batch, err in results:
        if err:
            errors.append(err)
        for s in batch:
            iid = s.get("itemId")
            if not iid:
                continue
            if iid in found:
                if query not in found[iid][1]:
                    found[iid][1].append(query)
            else:
                found[iid] = (s, [query])
    if errors and not found:
        raise RuntimeError("; ".join(errors[:3]))
    return found, errors, len(jobs)


def build_item(summary: dict, via: list[str], detail: dict, live: int | None,
               cfg: dict) -> dict:
    """One card: the search hit, what its page said, and the score."""
    item = hunt.summary_item(summary, cfg)
    item.pop("signals", None)
    item.pop("parts", None)
    signals = parse_substance(item["title"], detail.get("text", ""),
                              detail.get("aspects") or {}, cfg)
    total, substance, breakdown = score(item, signals, live, cfg)
    blob = (item["title"] + " " + detail.get("text", "")[:1500]).lower()
    item.update({
        "via": via,
        "brand": detail.get("brand", ""),
        "brand_live": live,
        "listed": item["listed"] or detail.get("listed", ""),
        "ag": detail.get("ag", item["ag"]),
        "as_is_excerpt": detail.get("as_is_excerpt", ""),
        "project": item["condition_id"] == authguard.FOR_PARTS_CONDITION_ID
                   or any(k in blob for k in PROJECT_KEYWORDS),
        "signals": signals,
        "score": total,
        "substance": substance,
        "breakdown": breakdown,
    })
    return item


def sweep(client: EbayClient, storage: Storage, cfg: dict | None = None,
          now: float | None = None) -> dict:
    """Search, gate, read, measure, score, remember. Returns the run's stats.

    `stats["new"]` is the cards that qualified for the first time on this run,
    best first — the digest's material.
    """
    cfg = cfg or load_config_file()
    now = now or time.time()
    found, errors, calls = _search(client, cfg)
    stats = {"ts": now, "found": len(found), "search_calls": calls,
             "errors": errors, "reasons": {}, "detail_calls": 0,
             "brand_calls": 0, "pending": 0, "shown": 0}

    def reject(why: str) -> None:
        kind = why.split(":")[0]
        stats["reasons"][kind] = stats["reasons"].get(kind, 0) + 1

    known = storage.wildcard_rows(list(found))
    blocked_makers = storage.wildcard_blocked_brands()
    rows, todo = [], []
    for iid, (summary, via) in found.items():
        title = summary.get("title") or ""
        why = title_verdict(title, cfg)
        if why:
            reject(why)
            continue
        row = known.get(iid)
        detail = json.loads(row["detail"]) if row and row["detail"] else None
        if detail is None:
            todo.append((title_substance(title, cfg), iid))
        rows.append([iid, summary, via, detail])

    # New listings get read best-promise-first, up to the sweep's budget.
    todo.sort(key=lambda t: -t[0])
    budget = [iid for _, iid in todo[:int(cfg.get("detail_per_sweep") or 120)]]

    def load(iid):
        try:
            return iid, read_detail(client.get_item(iid))
        except Exception:
            return iid, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = dict(pool.map(load, budget))
    stats["detail_calls"] = len(budget)

    cat = str(cfg.get("category") or "31387")
    brand_ttl = float(cfg.get("brand_ttl_days") or 14) * 86400
    counts: dict[str, int | None] = {}
    upserts, new_cards, live_ids = [], [], []
    for iid, summary, via, detail in rows:
        if detail is None:
            detail = fetched.get(iid)
        if detail is None:
            stats["pending"] += 1       # over budget or the call failed: next sweep
            continue
        row = known.get(iid)
        why = detail_verdict(detail, cfg)
        key = brand_key(detail.get("brand", ""))
        if not why and key in blocked_makers:
            why = "maker you blocked"
        live = None
        if not why:
            if key not in counts:
                cached = storage.wildcard_brand(key)
                if cached and cached["live_count"] is not None \
                        and now - (cached["checked_at"] or 0) < brand_ttl:
                    counts[key] = cached["live_count"]
                else:
                    counts[key] = live_count(client, detail["brand"], cat)
                    stats["brand_calls"] += 1
                    storage.wildcard_set_brand(key, detail["brand"], counts[key], now)
            live = counts[key]
            if live is not None and live > int(cfg.get("known_over") or 400):
                why = f"known maker: {live:,} live listings"
        item = None
        if not why:
            item = build_item(summary, via, detail, live, cfg)
            if item["substance"] < float(cfg.get("min_substance") or 0):
                why = "not enough substance"
        if why:
            reject(why)
        else:
            live_ids.append(iid)
            if not (row and row["status"] == "ok"):
                new_cards.append(item)
        upserts.append({
            "item_id": iid, "title": summary.get("title") or "",
            "brand": detail.get("brand", ""),
            "score": 0.0 if why else item["score"],
            "status": "excluded" if why else "ok", "why": why,
            "payload": "" if why else json.dumps(item),
            "detail": json.dumps(detail),
        })

    storage.wildcard_save(upserts, now)
    stats["shown"] = len(live_ids)
    new_cards.sort(key=lambda i: -i["score"])
    public = {k: v for k, v in stats.items()}
    storage.wildcard_set_meta("last_sweep", json.dumps(public))
    stats["new"] = new_cards
    return stats


def live_cards(storage: Storage, cfg: dict, now: float | None = None) -> dict:
    """What the tab shows: the last sweep's survivors, best first."""
    now = now or time.time()
    raw = storage.wildcard_meta("last_sweep")
    stats = json.loads(raw) if raw else None
    items, passed = [], []
    if stats:
        new_secs = float(cfg.get("new_hours") or 48) * 3600
        for row in storage.wildcard_live(stats["ts"]):
            item = json.loads(row["payload"])
            item["first_seen"] = row["first_seen"]
            item["is_new"] = now - (row["first_seen"] or 0) < new_secs
            (passed if row["passed"] else items).append(item)
    items.sort(key=lambda i: -i["score"])
    return {"stats": stats, "items": items, "passed": passed,
            "makers": storage.wildcard_makers()}


# ---------------------------------------------------------------------------
# The digest. Same contract as the Rarities one: silent unless something new
# turned up, and the very first run only seeds.
# ---------------------------------------------------------------------------

def _line(item: dict) -> str:
    what = ", ".join(s["label"] for s in item["signals"]
                     if s["group"] in SUBSTANCE_GROUPS)[:80]
    price = f"${item['price']:,.0f}" + (" bid" if item.get("is_auction") else "")
    live = item.get("brand_live")
    return (f"{item['score']:g} · {item.get('brand') or '?'}"
            + (f" ({live} live)" if live is not None else "")
            + f" · {price}\n  {what}\n  {item['title'][:70]}")


def build_digest(cards: list[dict], lead: int = 5) -> tuple[str, str, str]:
    top = cards[0]
    title = (f"Wild Card: {top.get('brand') or 'unknown maker'} · {top['score']:g}"
             if len(cards) == 1 else
             f"Wild Cards: {len(cards)} new · best {top['score']:g}")
    body = [_line(c) for c in cards[:lead]]
    if len(cards) > lead:
        body.append(f"+{len(cards) - lead} more in the app")
    return title, "\n".join(body), top.get("url", "")


def run_digest(db_path: str, dry_run: bool = False, min_score: float | None = None) -> int:
    app = load_config()
    if not app.has_ebay_keys:
        raise RuntimeError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set")
    client = EbayClient(app.ebay_client_id, app.ebay_client_secret,
                        app.marketplace, app.buyer_country, app.buyer_postal_code)
    cfg = load_config_file()
    storage = Storage(db_path)
    try:
        seeding = storage.wildcard_meta("last_sweep") is None
        stats = sweep(client, storage, cfg)
    finally:
        storage.close()
    print(f"[wildcard] {stats['found']} found · {stats['shown']} wild cards live · "
          f"{len(stats['new'])} new · {stats['pending']} still to read · "
          f"{stats['search_calls']}+{stats['detail_calls']}+{stats['brand_calls']} calls"
          + (f" · {len(stats['errors'])} queries failed" if stats["errors"] else ""))
    floor = float(cfg.get("alert_score") or 55) if min_score is None else min_score
    cards = [c for c in stats["new"] if c["score"] >= floor]
    if dry_run:
        for c in stats["new"][:10]:
            print(_line(c))
        return len(cards)
    if seeding:
        print("[wildcard] first sweep: seeded quietly; the next run reports what's new.")
        return 0
    if cards:
        title, message, url = build_digest(cards)
        app_notifier = Notifier(app.pushover_user_key, app.pushover_api_token)
        app_notifier.send_digest(title, message, url)
    return len(cards)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wild Cards sweep and digest")
    parser.add_argument("--db", default="gemhunter.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="sweep and print the new finds instead of pushing")
    parser.add_argument("--min-score", type=float, default=None,
                        help="push threshold (default: alert_score in wildcard.yaml)")
    args = parser.parse_args()
    count = run_digest(args.db, args.dry_run, args.min_score)
    print(f"[wildcard] {count} new find(s) at or over the alert score.")


if __name__ == "__main__":
    main()
