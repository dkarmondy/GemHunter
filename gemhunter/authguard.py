"""Authenticity-Guarantee arbitrage: an AG-covered watch described as broken.

eBay switches its Authenticity Guarantee on from the listing's `conditionId`,
not from a word of the description. `conditionId 7000` ("For parts or not
working") is excluded from the programme outright. Plenty of sellers, though,
list a watch that does not run as `3000` ("Pre-owned") and put the bad news in
free text instead — and those listings keep their AG. Same watch, same risk,
except a third party opens it, authenticates it, and photographs it before it
ships. That pairing is the find, and `auth_arbitrage_class` is what names it.

`qualifiedPrograms` is the only source of truth for AG here. Never a price
threshold: the threshold moves by category and eBay has changed it before.
"""

from __future__ import annotations

import re

AG_PROGRAM = "AUTHENTICITY_GUARANTEE"
FOR_PARTS_CONDITION_ID = 7000

# The buckets, best signal first — this order is also the UI's order.
AG_ARBITRAGE = "AG_ARBITRAGE"      # AG covers it, the seller says it's broken
HONEST_BROKEN = "HONEST_BROKEN"    # listed for parts, so no AG either way
AG_UNKNOWN = "AG_UNKNOWN"          # AG covers it, prose unread or unreadable
AG_CLEAN = "AG_CLEAN"              # AG covers it, nothing wrong is claimed
NO_AG_OTHER = "NO_AG_OTHER"        # everything else
CLASSES = [AG_ARBITRAGE, HONEST_BROKEN, AG_UNKNOWN, AG_CLEAN, NO_AG_OTHER]

# How a seller actually writes "this doesn't work" when the condition field
# says pre-owned. Compiled so that spaces and hyphens are interchangeable
# ("as is" == "as-is" == a line break between the two words), the apostrophe
# in "doesn't" may be straight, curly, or missing, and an article may sit in
# any word gap — "needs a service" is the same disclosure as "needs service",
# and "not a running watch" the same as "not running". Allowing the article
# in every gap is safe: where it isn't idiomatic ("for a parts", "sold a
# as-is") no seller writes it, so that branch simply never fires.
AS_IS_PHRASES = [
    "as is", "as-is", "for parts", "not running", "not working",
    "needs service", "needs repair", "for repair", "non-running",
    "doesn't run", "does not run", "sold as-is",
]


def _phrase_pattern(phrase: str) -> str:
    out = []
    for ch in phrase:
        if ch in " -":
            out.append(r"[\s\-]+(?:an?\s+)?")
        elif ch == "'":
            out.append("['’]?")
        else:
            out.append(re.escape(ch))
    return r"\b" + "".join(out) + r"\b"


_AS_IS_RES = [(p, re.compile(_phrase_pattern(p), re.I)) for p in AS_IS_PHRASES]


def strip_html(html: str) -> str:
    """Seller descriptions are hand-rolled HTML; reduce to readable text."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# Most listings end with a wall of text that is not about the watch: a
# condition-grade legend spelling out every grade the seller uses ("C Needs
# repair", "Fair - these items may be in need of repair and are being sold as
# is"), then shipping and returns policy. Measured live, that legend was the
# source of *every* false AG_ARBITRAGE call, on watches with nothing wrong.
# So the scan stops where the boilerplate starts, the same cut
# rarities.condition_flags makes on conditionDescription.
_BOILERPLATE_RE = re.compile(
    r"(?i)\b(?:condition\s+(?:rank|guide|chart|key|scale)"
    r"|grading\s+(?:scale|guide|chart|key|system)"
    r"|(?:shipping|return|payment|store)\s+polic(?:y|ies)"
    r"|terms\s+(?:and|&)\s+conditions"
    r"|about\s+(?:us|our\s+(?:store|company)))\b")

# A second net for legends with no header. These name a category of stock, not
# the watch in hand: "These items may be in need of repair", "All items are
# sold as-is". A singular claim about this watch never reads like that.
_CATEGORY_RE = re.compile(
    r"(?i)\b(?:these|all|any|our|most|some)\s+(?:items?|watches|pieces|lots)\b"
    r"|\bitems\s+(?:may|are|that|with|in)\b")


# "we chose to leave it as-is", "the bezel was left as-is", "kept as is". In
# watch prose that is a claim of *originality* — unpolished, unmolested, the
# thing he actually wants — and it is the opposite of "sold as-is". Only the
# words immediately before the phrase tell the two apart.
_PRESERVED_RE = re.compile(
    r"(?i)\b(?:leave|leaving|left|keep|keeping|kept|remain(?:s|ed|ing)?"
    r"|retain(?:s|ed|ing)?|preserv(?:e|ed|ing))\b(?:\W+\w+){0,4}\W*$")


def _trim_boilerplate(text: str) -> str:
    """Drop the policy tail and grade legend — they describe stock, not this watch."""
    cut = _BOILERPLATE_RE.search(text)
    return text[:cut.start()].strip() if cut else text


# A description that is nothing but a link to the description. eBay hands these
# back for listings whose prose lives in an iframe on ebaydesc.com.
_URL_ONLY_RE = re.compile(r"^\s*(?:<[^>]*>\s*)*https?://\S+\s*$", re.I)
_LETTER_RE = re.compile(r"[A-Za-z]")


def _readable(raw: str) -> str | None:
    """Prose out of one HTML blob, or None if there was no prose in it."""
    if _URL_ONLY_RE.match(raw):
        return None
    try:
        text = strip_html(raw)
    except Exception:
        return None
    return text if text and _LETTER_RE.search(text) else None


# Every field a seller might put the bad news in. `conditionDescription` is
# the one consignment houses actually use — National Rarities writes "currently
# running" or "crown is stripped" there while `condition` still reads
# "Pre-owned" — so leaving it out would blind this to the seller he buys from.
DESCRIPTION_FIELDS = ("description", "shortDescription", "conditionDescription")


def description_text(item: dict) -> tuple[str | None, bool]:
    """(the seller's readable words, whether any field could not be read).

    `description` is whatever HTML the seller pasted, and sometimes it is not
    prose at all — an `<iframe>` aimed at ebaydesc.com, or the bare URL of one.
    The second half of the return is the whole point: an unread field is not a
    clean one, and only by reporting it can a broken watch be kept out of a
    bucket it has not earned. It matters only when nothing else confessed —
    a fault named in any readable field is an answer, whatever went unread.
    """
    chunks, unread = [], False
    for key in DESCRIPTION_FIELDS:
        raw = item.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = _readable(raw)
        if text is None:
            unread = True
            continue
        # A field that is all policy tail told us nothing about this watch;
        # that is not the same as failing to read it, so it is simply dropped.
        text = _trim_boilerplate(text)
        if text:
            chunks.append(text)
    return ("\n".join(chunks) if chunks else None), unread


def as_is_terms(text: str) -> list[str]:
    """Which of the as-is phrases the seller used, longest wording of each.

    The list overlaps by design — "sold as-is" contains "as-is" contains
    "as is" — so one disclosure would otherwise report as three findings.
    Only the fullest wording of each hit survives.
    """
    hits = []
    for phrase, rx in _AS_IS_RES:
        found = next((m for m in rx.finditer(text)
                      if _is_disclosure(text, m)), None)
        if found:
            hits.append((phrase, re.sub(r"[\s\-]+", " ", found.group(0).lower())))
    kept, seen = [], set()
    for phrase, form in hits:
        if form in seen or any(form != other and form in other for _, other in hits):
            continue
        seen.add(form)
        kept.append(phrase)
    return kept


def _sentence_around(text: str, at: int) -> str:
    """The sentence a match sits in — the unit a claim is made in."""
    start = max((text.rfind(end, 0, at) for end in (". ", "! ", "? ", "\n")),
                default=-1)
    end = min((e for e in (text.find(mark, at) for mark in (". ", "! ", "? ", "\n"))
               if e != -1), default=len(text))
    return text[start + 1:end]


def _is_category_claim(text: str, match) -> bool:
    """Is this match about the seller's stock in general rather than this watch?"""
    return bool(_CATEGORY_RE.search(_sentence_around(text, match.start())))


def _is_preserved(text: str, match) -> bool:
    """"Left as-is" — originality, not a fault. The verb before it decides."""
    return bool(_PRESERVED_RE.search(text[:match.start()]))


def _is_disclosure(text: str, match) -> bool:
    """Does this match actually say something is wrong with *this* watch?"""
    return not _is_category_claim(text, match) and not _is_preserved(text, match)


def _excerpt(text: str, limit: int = 220) -> str:
    """The sentence that tripped the match — the reason, next to the verdict."""
    for chunk in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        line = chunk.strip()
        if line and _CATEGORY_RE.search(line):
            continue
        if line and any(_is_disclosure(line, m)
                        for _, rx in _AS_IS_RES
                        for m in rx.finditer(line)):
            # Stripping tags leaves a space before the punctuation it stood in
            # front of ("needs service ."); close it up before quoting him.
            line = re.sub(" +([.,;:!?])", lambda m: m.group(1), line)
            return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"
    return ""


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify(item: dict) -> dict:
    """The AG-arbitrage read on one Browse API `getItem` payload."""
    condition_id = _int_or_none(item.get("conditionId"))
    programs = item.get("qualifiedPrograms") or []
    has_ag = AG_PROGRAM in programs if isinstance(programs, list) else False
    text, unread = description_text(item)
    terms = as_is_terms(text) if text else []
    excerpt = _excerpt(text) if terms else ""
    if terms:
        indicates = True          # he said it plainly, in one field or another
    elif text is None or unread:
        indicates = None          # nothing confessed, but we could not read it all
    else:
        indicates = False

    if condition_id == FOR_PARTS_CONDITION_ID:
        # For-parts is out of the programme whatever the description says, so
        # it never reaches the AG buckets below.
        klass = HONEST_BROKEN
    elif not has_ag:
        klass = NO_AG_OTHER
    elif indicates is None:
        klass = AG_UNKNOWN
    elif indicates:
        klass = AG_ARBITRAGE
    else:
        klass = AG_CLEAN

    return {
        "condition_id": condition_id,
        "condition": item.get("condition") or "",
        "price": (item.get("price") or {}).get("value"),
        "qualified_programs": programs if isinstance(programs, list) else [],
        "has_authenticity_guarantee": has_ag,
        "description_indicates_as_is": indicates,
        "as_is_terms": terms,
        "as_is_excerpt": excerpt,
        "auth_arbitrage_class": klass,
    }


def class_counts(classes) -> dict:
    """Tally of every bucket, zeros included — a bucket that never fires is news."""
    counts = {name: 0 for name in CLASSES}
    for name in classes:
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def format_counts(counts: dict) -> str:
    return " · ".join(f"{name} {counts.get(name, 0)}" for name in CLASSES)


def log_counts(where: str, classes) -> None:
    """Print the per-bucket tally, so how often AG_ARBITRAGE fires is visible."""
    names = [name for name in classes if name]
    if not names:
        return
    print(f"[auth] {where} · {format_counts(class_counts(names))}")
