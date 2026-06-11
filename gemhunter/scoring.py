"""The scorer: shared hard gates, then split into two streams:

  🔧 repair    (Path B) — as-is/for-parts serviceable chronographs you fix.
  🎯 collector (Path A) — nice complete pieces (box & papers, original, great seller),
                          incl. grails like Rolex Submariner / Daytona.

score_listing(listing) -> Score(stream, score, mode, reasons, rejected).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import knowledge as K
from .models import Listing


@dataclass
class Score:
    listing: Listing
    score: float = 0.0
    stream: str = ""               # "repair" or "collector"
    mode: str = ""                 # short label for the alert
    reasons: list = field(default_factory=list)
    rejected: bool = False
    reject_reason: str = ""


def _first_hit(text: str, needles) -> str | None:
    return next((n for n in needles if n in text), None)


_CHRONO_RE = re.compile(r"\bchrono\b")


def _is_chrono(text: str) -> bool:
    # "chronograph" or standalone "chrono" — but NOT "chronometer" (a certification).
    return "chronograph" in text or bool(_CHRONO_RE.search(text))


def _parse_size(value: str) -> float | None:
    m = re.search(r"(\d{2}(?:\.\d+)?)\s*mm", value.lower())
    return float(m.group(1)) if m else None


def _parse_year(value: str) -> int | None:
    m = re.search(r"\b(18|19|20)\d{2}\b", value)
    return int(m.group(0)) if m else None


def _seller_gate(l: Listing):
    pct, cnt = l.seller_feedback_pct, l.seller_feedback_score
    if pct <= 0:
        return True, ""
    if pct < 90:
        return False, f"seller {pct:.0f}% (<90%)"
    if cnt < 20 and pct < 100:
        return False, f"seller unproven ({cnt} sales, {pct:.0f}%)"
    if 20 <= cnt < 1000 and pct < 99:
        return False, f"seller {pct:.0f}% (<99% under 1k sales)"
    if cnt >= 1000 and pct < 95:
        return False, f"seller {pct:.0f}% (<95%)"
    return True, ""


def _great_seller(l: Listing) -> bool:
    return l.seller_feedback_score >= K.GREAT_SELLER_SCORE and \
        l.seller_feedback_pct >= K.GREAT_SELLER_PCT


def _movement_match(t: str, movement_asp: str):
    cal_hit, cal_pts, is_cw = None, 0, False
    for kw, (pts, cw) in K.VALUED_CALIBERS.items():
        if (kw in t or kw in movement_asp) and pts > cal_pts:
            cal_hit, cal_pts, is_cw = kw.strip(), pts, cw
    return cal_hit, cal_pts, is_cw


def _size_adjust(asp: dict, score: float, reasons: list) -> float:
    size = _parse_size(asp.get("case size", ""))
    if size and size >= K.SIZE_MIN:
        score += K.W_SIZE_OK
        reasons.append(f"{size:g}mm")
    elif size and size < K.SIZE_TINY:
        score += K.W_SIZE_SMALL
        reasons.append(f"{size:g}mm-small")
    return score


def _reject(r: Score, why: str) -> Score:
    r.rejected, r.reject_reason = True, why
    return r


def score_listing(listing: Listing) -> Score:
    r = Score(listing=listing)
    t = (listing.title or "").lower()
    asp = {k.lower(): str(v).lower() for k, v in (listing.aspects or {}).items()}
    brand_asp = asp.get("brand", "")
    movement_asp = asp.get("movement", "")
    features_asp = asp.get("features", "")
    blob = f"{t} {brand_asp} {movement_asp} {features_asp}"

    # ----------------------- HARD GATES (shared) -----------------------
    hit = _first_hit(blob, K.DISLIKED_CALIBERS)
    if hit:
        return _reject(r, f"disliked caliber {hit}")
    if not _first_hit(t, K.JAPANESE_EXCEPTIONS):
        hit = _first_hit(f"{t} {brand_asp}",
                         K.JAPANESE_BRANDS + K.RUSSIAN_BRANDS + K.CHINESE_BRANDS)
        if hit:
            return _reject(r, f"excluded region/brand: {hit.strip()}")
    hit = _first_hit(f"{t} {brand_asp}", K.EXCLUDE_BRANDS)
    if hit:
        return _reject(r, f"excluded brand: {hit.strip()}")
    hit = _first_hit(t, K.EXCLUDE_KEYWORDS)
    if hit:
        return _reject(r, f"excluded: {hit.strip()}")
    if _first_hit(t, K.QUARTZ_KEYWORDS) or "quartz" in movement_asp:
        return _reject(r, "quartz")
    qm = _first_hit(t, K.QUARTZ_MODELS)
    if qm and "automatic" not in movement_asp and "mechanical" not in movement_asp:
        return _reject(r, f"quartz model ({qm})")
    year = _parse_year(asp.get("year manufactured", "") or asp.get("year", ""))
    if year and year < K.AGE_FLOOR_YEAR:
        return _reject(r, f"pre-{K.AGE_FLOOR_YEAR} ({year})")
    ok, why = _seller_gate(listing)
    if not ok:
        return _reject(r, why)

    # ----------------------- STREAM SELECT -----------------------
    # A project = title says as-is/for-parts, OR eBay condition is "For parts or not working".
    cond = (listing.condition or "").lower()
    project = _first_hit(t, K.PROJECT_KEYWORDS) or \
        ("for parts (condition)" if "parts" in cond else None)
    if project:
        return _score_repair(r, t, asp, movement_asp, features_asp, brand_asp, project)
    return _score_collector(r, t, blob, asp, movement_asp, features_asp, brand_asp)


def _score_repair(r, t, asp, movement_asp, features_asp, brand_asp, project) -> Score:
    """Path B — as-is/for-parts serviceable chronographs."""
    if _first_hit(f"{t} {brand_asp}", K.NO_REPAIR_BRANDS):
        return _reject(r, "no-repair brand (route to collector)")
    taste, reasons = 0.0, []
    brand_hit = _first_hit(f"{t} {brand_asp}", K.TASTE_BRANDS)
    chrono_hit = _is_chrono(f"{t} {features_asp}")
    cal_hit, cal_pts, is_cw = _movement_match(t, movement_asp)
    if brand_hit:
        taste += K.W_BRAND
        reasons.append(f"brand:{brand_hit.strip()}")
    if chrono_hit:
        taste += K.W_CHRONO
        reasons.append("chronograph")
    if cal_hit:
        taste += cal_pts
        reasons.append(f"caliber:{cal_hit}")
    if taste < K.TASTE_MIN:
        return _reject(r, f"below taste gate ({taste:.0f})")

    score = taste
    if is_cw:
        score += K.W_COLUMN_WHEEL
        reasons.append("column-wheel")
    grade = next((b for b in K.BRAND_GRADE if b in t), None)
    if grade:
        score += K.BRAND_GRADE[grade]
        reasons.append(f"grade:{grade}")
    score += K.W_PROJECT
    reasons.append(f"project:{project.strip()}")
    score = _size_adjust(asp, score, reasons)
    neg = _first_hit(t, K.NEGATIVE_KEYWORDS)
    if neg:
        score += K.W_NEGATIVE
        reasons.append(f"⚠{neg.strip()}")

    r.score, r.stream, r.mode, r.reasons = score, "repair", "🔧 for parts/repair", reasons
    return r


def _score_collector(r, t, blob, asp, movement_asp, features_asp, brand_asp) -> Score:
    """Not-a-project pieces. Routes to two streams:
       rolex  — Rolex (box & papers float to the top)
       chrono — vintage/other chronographs (need not be full set)
    Anything that's neither Rolex nor a chronograph is dropped.
    """
    is_rolex = "rolex" in f"{t} {brand_asp}"
    rolex_target = is_rolex and bool(_first_hit(t, K.ROLEX_TARGETS))
    brand_hit = _first_hit(f"{t} {brand_asp}", K.TASTE_BRANDS)
    model_hit = _first_hit(t, K.COLLECTOR_TARGETS)
    chrono_hit = _is_chrono(f"{t} {features_asp}")
    cal_hit, cal_pts, is_cw = _movement_match(t, movement_asp)
    is_chrono = bool(chrono_hit or cal_hit)

    # Route to one of five tabs (repair handled earlier).
    is_patek = "patek" in f"{t} {brand_asp}"
    is_iwc = "iwc" in f"{t} {brand_asp}"
    if rolex_target:
        stream, mode = "rolex", "🎯 box & papers"
    elif is_patek:
        stream, mode = "patek", "👑 patek"
    elif is_iwc:
        stream, mode = "iwc", "✈️ iwc golden era"
    elif is_chrono:
        stream, mode = "chrono", "⏱ chronograph"
    elif brand_hit or model_hit:
        stream, mode = "taste", "💎 taste"
    else:
        return _reject(r, "not rolex/patek/iwc/chrono/taste")

    reasons, score = [], 0.0
    if brand_hit:
        score += K.W_TARGET
        reasons.append(f"brand:{brand_hit.strip()}")
    if model_hit:
        score += K.W_MODEL
        reasons.append(f"model:{model_hit.strip()}")
    if chrono_hit:
        score += K.W_CHRONO
        reasons.append("chronograph")
    if cal_hit:
        score += cal_pts
        reasons.append(f"caliber:{cal_hit}")
        if is_cw:
            score += K.W_COLUMN_WHEEL
            reasons.append("column-wheel")
    grade = next((b for b in K.BRAND_GRADE if b in t), None)
    if grade:
        score += K.BRAND_GRADE[grade]
        reasons.append(f"grade:{grade}")

    if stream == "iwc":
        tgt = _first_hit(t, K.IWC_TARGETS)
        if tgt:
            score += K.W_IWC_TARGET
            reasons.append(f"era-ref:{tgt.strip()}")
        yr = _parse_year(asp.get("year manufactured", "") or asp.get("year", "")) \
            or _parse_year(t)
        if yr and yr > K.IWC_ERA_END:
            score += K.W_IWC_MODERN
            reasons.append(f"⚠modern({yr})")

    # Condition premium — pushes box & papers / original pieces to the top.
    fullset = (_first_hit(blob, K.FULLSET_KEYWORDS)
               or asp.get("with papers") == "yes"
               or asp.get("with original box/packaging") == "yes")
    if fullset:
        score += K.W_FULLSET
        reasons.append("box&papers")
    orig = _first_hit(t, K.ORIGINAL_KEYWORDS)
    if orig:
        score += K.W_ORIGINAL
        reasons.append(orig.strip())
    if "unpolished" not in t and ("polished" in t or "refinished" in t):
        score += K.W_POLISHED
        reasons.append("⚠polished")
    if _great_seller(r.listing):
        score += K.W_GREAT_SELLER
        reasons.append("great-seller")
    if r.listing.auth_guarantee:
        score += K.W_AUTH_GUARANTEE
        reasons.append("auth-guarantee")
    score = _size_adjust(asp, score, reasons)
    neg = _first_hit(t, K.NEGATIVE_KEYWORDS)
    if neg:
        score += K.W_NEGATIVE
        reasons.append(f"⚠{neg.strip()}")

    r.score, r.stream, r.mode, r.reasons = score, stream, mode, reasons
    return r
