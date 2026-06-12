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
from . import targets as target_table
from .models import Listing


@dataclass
class Score:
    listing: Listing
    score: float = 0.0
    opportunity: float = 0.0
    confidence: float = 0.0
    stream: str = ""               # "repair" or "collector"
    mode: str = ""                 # short label for the alert
    reasons: list = field(default_factory=list)
    risk_tags: list = field(default_factory=list)
    action_note: str = ""
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


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _seller_confidence(l: Listing) -> float:
    pct, cnt = l.seller_feedback_pct, l.seller_feedback_score
    if pct <= 0:
        return 45.0
    score = 35.0
    score += min(28.0, cnt / 40.0)
    if pct >= 99.8:
        score += 24.0
    elif pct >= 99:
        score += 19.0
    elif pct >= 97:
        score += 12.0
    elif pct >= 95:
        score += 5.0
    elif pct < 92:
        score -= 12.0
    if cnt < 20:
        score -= 14.0
    return _clamp(score)


def _risk_tags(t: str, asp: dict, listing: Listing, stream: str, year: int | None) -> list[str]:
    risks = []
    cc = (listing.country or "").upper()
    if cc and cc != "US":
        risks.append("import-fees")
        if cc not in K.PREFERRED_COUNTRIES:
            risks.append("foreign-market")
    if cc in K.HUMID_COUNTRIES:
        risks.append("humidity/moisture")
    if listing.seller_feedback_score < 100:
        risks.append("seller-count")
    if listing.seller_feedback_pct and listing.seller_feedback_pct < 99:
        risks.append("seller-feedback")
    if not listing.image_url:
        risks.append("no-image")
    if stream in ("repair", "rare", "chrono") and not asp.get("movement"):
        risks.append("no-movement-info")
    if _first_hit(t, K.NEGATIVE_KEYWORDS):
        risks.append("condition-risk")
    if "unpolished" not in t and ("polished" in t or "refinished" in t):
        risks.append("polished")
    if year and year < K.AGE_FLOOR_YEAR:
        risks.append("pre-1960")
    size = _parse_size(asp.get("case size", ""))
    if size and size < K.SIZE_TINY:
        risks.append("small-case")
    if listing.buying_option == "AUCTION" and not listing.bid_count:
        risks.append("low-bid-signal")
    return risks


def _finish(r: Score, stream: str, mode: str, score: float, reasons: list,
            t: str, asp: dict, opportunity_bias: float = 0.0,
            confidence_bias: float = 0.0, action_note: str = "") -> Score:
    year = _parse_year(asp.get("year manufactured", "") or asp.get("year", "")) \
        or _parse_year(t)
    risks = _risk_tags(t, asp, r.listing, stream, year)
    opportunity = _clamp(score * 5.2 + opportunity_bias - len(risks) * 2.0)
    confidence = _seller_confidence(r.listing) + confidence_bias
    if r.listing.auth_guarantee:
        confidence += 8.0
    if "box&papers" in reasons or "great-seller" in reasons:
        confidence += 6.0
    confidence -= len(risks) * 5.0
    r.score = score
    r.opportunity = round(opportunity, 1)
    r.confidence = round(_clamp(confidence), 1)
    r.stream = stream
    r.mode = mode
    r.reasons = reasons
    r.risk_tags = risks
    r.action_note = action_note
    return r


def _is_womens_watch(t: str, asp: dict) -> bool:
    dept = f" {asp.get('department', '')} "
    if any(w in dept for w in (" women ", " womens ", " women's ", " ladies ", " lady ")):
        return True
    return bool(re.search(r"\b(women'?s|lad(y|ies)|girls?|female)\b", t))


def _rare_match(t: str, brand_asp: str) -> str | None:
    hay = f"{t} {brand_asp}"
    for target in target_table.rare_targets(K.RARE_TARGETS):
        if not any(b in hay for b in target["brand_any"]):
            continue
        if any(m in hay for m in target["must_any"]):
            return target["label"]
    return None


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
    if _is_womens_watch(t, asp):
        return _reject(r, "women's watch")
    if _first_hit(t, K.QUARTZ_KEYWORDS) or "quartz" in movement_asp:
        return _reject(r, "quartz")
    qm = _first_hit(t, K.QUARTZ_MODELS)
    if qm and "automatic" not in movement_asp and "mechanical" not in movement_asp:
        return _reject(r, f"quartz model ({qm})")
    rare_hit = _rare_match(t, brand_asp)
    year = _parse_year(asp.get("year manufactured", "") or asp.get("year", ""))
    if year and year < K.AGE_FLOOR_YEAR and not rare_hit:
        return _reject(r, f"pre-{K.AGE_FLOOR_YEAR} ({year})")
    ok, why = _seller_gate(listing)
    if not ok:
        return _reject(r, why)
    if listing.country and listing.country in K.BLOCKED_COUNTRIES:
        return _reject(r, f"blocked country: {listing.country}")
    if rare_hit:
        return _apply_country(_score_rare(r, t, blob, asp, movement_asp, features_asp,
                                          brand_asp, rare_hit, year))

    # ----------------------- STREAM SELECT -----------------------
    # A project = title says as-is/for-parts, OR eBay condition is "For parts or not working".
    cond = (listing.condition or "").lower()
    project = _first_hit(t, K.PROJECT_KEYWORDS) or \
        ("for parts (condition)" if "parts" in cond else None)
    if project:
        return _apply_country(
            _score_repair(r, t, asp, movement_asp, features_asp, brand_asp, project))
    return _apply_country(
        _score_collector(r, t, blob, asp, movement_asp, features_asp, brand_asp))


def _apply_country(r: Score) -> Score:
    """Import-fee penalty + moisture tag based on the listing's origin country."""
    if r.rejected:
        return r
    cc = (r.listing.country or "").upper()
    if cc and cc != "US":
        preferred = cc in K.PREFERRED_COUNTRIES
        penalty = K.W_FOREIGN_PREF if preferred else K.W_FOREIGN_OTHER
        r.score += penalty
        r.opportunity = round(_clamp(r.opportunity + penalty * 4.0), 1)
        r.confidence = round(_clamp(r.confidence - (6.0 if preferred else 14.0)), 1)
        r.reasons.append(f"{cc}+import")
        if "import-fees" not in r.risk_tags:
            r.risk_tags.append("import-fees")
        if preferred:
            if "preferred-market" not in r.risk_tags:
                r.risk_tags.append("preferred-market")
        elif "foreign-market" not in r.risk_tags:
            r.risk_tags.append("foreign-market")
        if r.action_note:
            r.action_note += f" Confirm landed cost/import fees from {cc}."
        else:
            r.action_note = f"Confirm landed cost/import fees from {cc}."
    if cc in K.HUMID_COUNTRIES:
        r.reasons.append("humidity")
        r.confidence = round(_clamp(r.confidence - 4.0), 1)
        if "humidity/moisture" not in r.risk_tags:
            r.risk_tags.append("humidity/moisture")
        if r.action_note:
            r.action_note += " Inspect dial/movement for moisture and expect box/paper degradation."
        else:
            r.action_note = "Inspect dial/movement for moisture and expect box/paper degradation."
    return r


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

    return _finish(
        r, "repair", "🔧 for parts/repair", score, reasons, t, asp,
        opportunity_bias=14.0, confidence_bias=-8.0,
        action_note="Inspect movement photos, missing parts, rust, dial, hands, and pusher/crown originality.",
    )


def _score_rare(r, t, blob, asp, movement_asp, features_asp, brand_asp, rare_hit, year) -> Score:
    """Rare radar pieces: scarce references to surface immediately."""
    reasons, score = [f"rare:{rare_hit}"], K.W_RARE_TARGET
    brand_hit = _first_hit(f"{t} {brand_asp}", K.TASTE_BRANDS)
    chrono_hit = _is_chrono(f"{t} {features_asp}")
    cal_hit, cal_pts, is_cw = _movement_match(t, movement_asp)
    if brand_hit:
        score += K.W_TARGET
        reasons.append(f"brand:{brand_hit.strip()}")
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
    if year and year < K.AGE_FLOOR_YEAR:
        reasons.append(f"pre-{K.AGE_FLOOR_YEAR}-rare")

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

    return _finish(
        r, "rare", "rare radar", score, reasons, t, asp,
        opportunity_bias=18.0, confidence_bias=-3.0,
        action_note="Verify reference, dial originality, case condition, movement correctness, and seller story.",
    )


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

    action_note = {
        "rolex": "Check full-set proof, polishing, clasp/endlink correctness, serial era, and seller history.",
        "patek": "Verify reference, dial originality, movement photos, papers, case condition, and service history.",
        "iwc": "Check reference, era, bracelet/strap correctness, movement version, case wear, and service history.",
        "chrono": "Inspect movement caliber, pusher/hands completeness, dial originality, rust, and case size.",
        "taste": "Decide whether it meaningfully advances taste, then verify condition and seller quality.",
    }.get(stream, "Review condition, originality, seller quality, and price against comps.")
    return _finish(r, stream, mode, score, reasons, t, asp, action_note=action_note)
