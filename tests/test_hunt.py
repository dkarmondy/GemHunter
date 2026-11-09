"""Reference Hunt: the title/description parsers and the score built on them."""

from datetime import date, datetime, timezone

import pytest

from gemhunter import hunt


@pytest.fixture(scope="module")
def sub():
    """The real 126610LN preset, so the tests track the YAML."""
    return next(p for p in hunt.load_presets() if p["id"] == "126610LN")


def signals(sub, title="Rolex Submariner Date 126610LN", desc="", aspects=None):
    return hunt.parse_signals(title, desc, aspects or {}, sub)


def listing(sub, price, *, auction=False, title="Rolex Submariner Date 126610LN",
            desc="", condition_id=3000, shipping=0):
    return {"price": price, "shipping": shipping, "is_auction": auction,
            "condition_id": condition_id, "condition": "Pre-owned", "ag": True,
            "feedback_score": 5000, "feedback_pct": 100.0, "top_rated": True,
            "detail": True, "signals": signals(sub, title, desc)}


def part(breakdown, label):
    return next(p for p in breakdown if p["label"] == label)


# --- presets -----------------------------------------------------------------

def test_presets_load_and_have_the_keys_scoring_needs():
    presets = hunt.load_presets()
    assert presets, "presets/hunts.yaml has no presets"
    ids = [p["id"] for p in presets]
    assert len(ids) == len(set(ids)), "duplicate preset id"
    for p in presets:
        assert p["benchmark_low"] < p["benchmark_high"], p["id"]
        assert p["deal_floor"] < p["benchmark_low"], p["id"]
        assert sum(p["weights"].values()) == 100, p["id"]


@pytest.mark.parametrize("title, ruled_out", [
    ("Rolex Submariner Date 126610LN Box & Papers 2022", False),
    ("Rolex 126610LN Submariner box only", True),
    ("Rolex 126610LN watch and papers only", False),     # the watch is there
    ("Rolex Submariner 126610LN homage", True),
    ("Rolex Submariner 124060 No Date", True),           # wrong reference
])
def test_title_verdict(sub, title, ruled_out):
    assert bool(hunt.title_verdict(title, sub)) is ruled_out


# --- warranty card vs service card ------------------------------------------

def test_card_date_from_description(sub):
    s = signals(sub, desc="Purchased from an authorized dealer in March 2022.")
    assert (s["card"]["year"], s["card"]["month"]) == (2022, 3)
    assert s["card"]["approx"] is False


def test_service_card_is_not_the_warranty_card(sub):
    s = signals(sub, desc="Serviced by Rolex in 2025, comes with the RSC "
                          "service warranty card.")
    assert s["card"] is None
    assert s["warranty_months"] is None


def test_warranty_card_survives_a_later_service(sub):
    s = signals(sub, desc="Warranty card dated 2021. Serviced by Rolex 2025, "
                          "with the service card.")
    assert s["card"]["year"] == 2021


def test_year_with_no_card_words_is_ignored(sub):
    assert signals(sub, desc="The 126610LN launched in 2020.")["card"] is None


def test_title_card_date_beats_description(sub):
    s = signals(sub, title="Rolex 126610LN 2023 card",
                desc="Purchased 2021 at an AD.")
    assert s["card"]["year"] == 2023 and s["card"]["where"] == "title"


def test_warranty_months_count_down_from_the_card(sub):
    s = signals(sub, desc=f"Warranty card dated January {date.today().year}.")
    assert 48 < s["warranty_months"] <= 60


# --- box & papers, polish, bracelet -----------------------------------------

@pytest.mark.parametrize("desc, full", [
    ("Comes with box and papers.", True),
    ("Full set, all stickers.", True),
    ("B&P included", True),
    ("Watch only, no box.", False),
])
def test_full_set(sub, desc, full):
    assert signals(sub, desc=desc)["full_set"] is full


def test_full_set_from_item_specifics(sub):
    s = signals(sub, aspects={"With Original Box/Packaging": "Yes",
                              "With Papers": "Yes"})
    assert s["full_set"] is True


@pytest.mark.parametrize("desc, good, bad", [
    ("Unpolished case, sharp lugs.", {"unpolished"}, set()),
    ("Has never been polished.", {"never polished"}, set()),
    ("Case has been polished.", set(), {"polished"}),
    ("Case Finish: Polished", set(), set()),          # a spec line, not a refinish
    ("Hasn't been polished.", set(), set()),
])
def test_polish(sub, desc, good, bad):
    s = signals(sub, desc=desc)
    assert {x["label"] for x in s["positive"]} == good
    assert {x["label"] for x in s["negative"]} == bad


@pytest.mark.parametrize("desc, status", [
    ("On the original Oyster bracelet.", "confirmed"),
    ("Watch head only, no bracelet.", "missing"),
    ("Fitted on a rubber strap.", "missing"),
    ("Comes on a rubber strap, original bracelet also included.", "confirmed"),
    ("", "unknown"),
])
def test_bracelet(sub, desc, status):
    assert signals(sub, desc=desc)["bracelet"]["status"] == status


# --- score ---------------------------------------------------------------------

def test_price_points_scale_across_the_market_band(sub):
    lo, hi = sub["benchmark_low"], sub["benchmark_high"]
    w = sub["weights"]["price"]
    pts = lambda price: part(hunt.score(listing(sub, price), sub)[1],
                             "Price vs market")["pts"]
    assert pts(lo) == w
    assert pts(hi) == 0
    assert pts((lo + hi) / 2) == pytest.approx(w / 2, abs=0.1)


def test_buy_it_now_under_the_deal_floor_gets_no_price_points(sub):
    _, bd = hunt.score(listing(sub, sub["deal_floor"] - 1), sub)
    price = part(bd, "Price vs market")
    assert price["pts"] == 0
    assert "deal OR suspicious" in price["note"]


def test_low_auction_bid_is_scored_at_the_market_middle(sub):
    lo, hi = sub["benchmark_low"], sub["benchmark_high"]
    _, bd = hunt.score(listing(sub, 3000, auction=True), sub)
    price = part(bd, "Price vs market")
    assert price["pts"] == pytest.approx(sub["weights"]["price"] / 2, abs=0.1)
    assert "auction" in price["note"]
    # Once the bid passes the middle it is priced like any other listing.
    high_bid = hunt.score(listing(sub, hi, auction=True), sub)[1]
    assert part(high_bid, "Price vs market")["pts"] == 0
    assert lo < (lo + hi) / 2 < hi


def test_cheap_auction_no_longer_outranks_a_documented_listing(sub):
    auction, _ = hunt.score(listing(sub, 3000, auction=True), sub)
    good, _ = hunt.score(listing(
        sub, sub["benchmark_low"], desc="Full set, box and papers. Unpolished. "
        f"Warranty card dated {date.today().year - 1}. Oyster bracelet."), sub)
    assert good > auction


def test_auction_is_never_flagged_deal_or_suspicious(sub):
    item = {"id": "test-auction", "title": "Rolex 126610LN", "price": 3000,
            "shipping": 0, "is_auction": True, "listed": "",
            "condition_id": 3000, "signals": signals(sub)}
    now = datetime.now(timezone.utc)
    assert hunt._decorate(item, sub, now)["deal_or_suspicious"] is False
    item["is_auction"] = False
    assert hunt._decorate(item, sub, now)["deal_or_suspicious"] is True


def test_missing_bracelet_is_priced_in(sub):
    _, bd = hunt.score(listing(sub, sub["benchmark_low"],
                               desc="Watch head only."), sub)
    assert "for a bracelet" in part(bd, "Price vs market")["note"]
    assert "no bracelet" in part(bd, "Condition")["note"]


@pytest.mark.parametrize("fb, pct, top, expect", [
    (0, 100.0, False, 0.0),
    (10_000, 97.0, False, 0.0),        # big but 97%: nothing
    (9_999, 100.0, True, 12.0),        # full marks, near enough
])
def test_seller_points(fb, pct, top, expect):
    pts, _ = hunt.seller_points({"feedback_score": fb, "feedback_pct": pct,
                                 "top_rated": top}, 12)
    assert pts == pytest.approx(expect, abs=0.1)
