"""The scorer: gate -> taste -> multiplier, per the playbook scoring philosophy.

score_listing(listing) -> Score
  1. Hard gates (exclusions, region, quartz, disliked caliber, age, seller) -> reject.
  2. Taste gate (brand / chronograph / valued caliber) -> drop if below threshold,
     even if it's a steal.
  3. Multipliers (column-wheel, as-is opportunity, box&papers, grade, size, ...) that
     only BOOST things which already passed the taste gate.
Returns a numeric score, a mode tag, and human-readable reasons for the alert.
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
    mode: str = ""                 # project / wishlist / undervalued
    reasons: list = field(default_factory=list)
    rejected: bool = False
    reject_reason: str = ""


def _first_hit(text: str, needles) -> str | None:
    return next((n for n in needles if n in text), None)


def _parse_size(value: str) -> float | None:
    m = re.search(r"(\d{2}(?:\.\d+)?)\s*mm", value.lower())
    return float(m.group(1)) if m else None


def _parse_year(value: str) -> int | None:
    m = re.search(r"\b(18|19|20)\d{2}\b", value)
    return int(m.group(0)) if m else None


def _seller_gate(l: Listing):
    """Return (ok, reason). Lenient when seller data is missing."""
    pct, cnt = l.seller_feedback_pct, l.seller_feedback_score
    if pct <= 0:
        return True, ""                       # no data -> don't reject on it
    if pct < 90:
        return False, f"seller {pct:.0f}% (<90%)"
    if cnt < 20 and pct < 100:
        return False, f"seller unproven ({cnt} sales, {pct:.0f}%)"
    if 20 <= cnt < 1000 and pct < 99:
        return False, f"seller {pct:.0f}% (<99% under 1k sales)"
    if cnt >= 1000 and pct < 95:
        return False, f"seller {pct:.0f}% (<95%)"
    return True, ""


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

    # ----------------------- HARD GATES -----------------------
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
    qmodel = _first_hit(t, K.QUARTZ_MODELS)
    if qmodel and "automatic" not in movement_asp and "mechanical" not in movement_asp:
        return _reject(r, f"quartz model ({qmodel})")

    year = _parse_year(asp.get("year manufactured", "") or asp.get("year", ""))
    if year and year < K.AGE_FLOOR_YEAR:
        return _reject(r, f"pre-{K.AGE_FLOOR_YEAR} ({year})")

    ok, why = _seller_gate(listing)
    if not ok:
        return _reject(r, why)

    # ----------------------- TASTE GATE -----------------------
    taste, reasons = 0.0, []
    brand_hit = _first_hit(f"{t} {brand_asp}", K.TASTE_BRANDS)
    chrono_hit = _first_hit(f"{t} {features_asp}", K.CHRONO_KEYWORDS)

    cal_hit, cal_pts, is_cw = None, 0, False
    for kw, (pts, cw) in K.VALUED_CALIBERS.items():
        if (kw in t or kw in movement_asp) and pts > cal_pts:
            cal_hit, cal_pts, is_cw = kw.strip(), pts, cw

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
        return _reject(r, f"below taste gate ({taste:.0f}<{K.TASTE_MIN:.0f})")

    # ----------------------- MULTIPLIERS -----------------------
    score = taste

    if is_cw:
        score += K.W_COLUMN_WHEEL
        reasons.append("column-wheel")

    grade = next((b for b in K.BRAND_GRADE if b in t), None)
    if grade:
        score += K.BRAND_GRADE[grade]
        reasons.append(f"grade:{grade}")

    proj = _first_hit(t, K.PROJECT_KEYWORDS)
    if proj:
        score += K.W_PROJECT
        reasons.append(f"project:{proj.strip()}")

    box = _first_hit(f"{t} {asp.get('with original box/packaging','')} "
                     f"{asp.get('with papers','')}", K.POSITIVE_CONDITION)
    if box or asp.get("with papers") == "yes":
        score += K.W_BOX_PAPERS
        reasons.append("box&papers")

    if listing.auth_guarantee:
        score += K.W_AUTH_GUARANTEE
        reasons.append("auth-guarantee")

    size = _parse_size(asp.get("case size", ""))
    if size:
        if size >= K.SIZE_MIN:
            score += K.W_SIZE_OK
            reasons.append(f"{size:g}mm")
        elif size < K.SIZE_TINY:
            score += K.W_SIZE_SMALL
            reasons.append(f"{size:g}mm-small")

    caseback = asp.get("caseback", "") + asp.get("case material", "")
    if "screw" in caseback or "solid" in caseback or "stainless" in caseback:
        score += K.W_SOLID_SCREW

    neg = _first_hit(t, K.NEGATIVE_KEYWORDS)
    if neg:
        score += K.W_NEGATIVE
        reasons.append(f"⚠{neg.strip()}")

    # ----------------------- MODE TAG -----------------------
    if proj and (cal_hit or chrono_hit):
        mode = "project"
    elif brand_hit and not proj:
        mode = "wishlist"
    else:
        mode = "undervalued"

    r.score, r.mode, r.reasons = score, mode, reasons
    return r
