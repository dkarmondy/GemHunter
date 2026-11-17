"""Wild Cards: the gates, the substance parser, the score and a whole sweep.

The benchmark is the watch the tab was built to find: a Forget split-seconds
chronograph, one of about 250, guilloché silver dial, 21k rotor. If a listing
like that does not come out on top, the tab is wrong.
"""

import json

import pytest

from gemhunter import wildcard
from gemhunter.storage import Storage


@pytest.fixture(scope="module")
def cfg():
    """The real presets/wildcard.yaml, so the tests track the file."""
    return wildcard.load_config_file()


def labels(signals, group=None):
    return {s["label"] for s in signals if group is None or s["group"] == group}


FORGET_TITLE = "Forget Rattrapante Chronograph Automatic Date 40mm Steel Bracelet"
FORGET_DESC = ("Double split chronograph, one out of approx 250 made. Hand made "
               "solid silver dial with gioshe finish, hand blued hands, "
               "21k gold rotor visible through the display back.")
FORGET_ASPECTS = {"Brand": "Forget", "Movement": "Mechanical (Automatic)",
                  "Case Size": "40 mm", "Features": "Chronograph, Date Indicator",
                  "Country/Region of Manufacture": "Switzerland"}


# --- config ------------------------------------------------------------------

def test_config_loads_and_weights_total_100(cfg):
    assert cfg["queries"], "presets/wildcard.yaml has no queries"
    assert all(q.get("q") for q in cfg["queries"])
    assert sum(cfg["weights"].values()) == 100
    assert cfg["price_min"] < cfg["price_max"]


def test_no_query_names_a_maker_the_gate_would_drop(cfg):
    # The net is attributes only. A query naming a known maker would be the
    # common road this tab exists to stay off.
    for q in cfg["queries"]:
        assert wildcard.title_verdict(q["q"], cfg) == "", q["q"]


# --- title gate --------------------------------------------------------------

@pytest.mark.parametrize("title, why", [
    ("Rolex Daytona 116520 Chronograph", "known maker: rolex"),
    ("Mens A. Lange & Sohne Datograph", "known maker: a. lange"),
    ("MB&F Legacy Machine", "known maker: mb&f"),
    ("Poljot Chronograph 3133", "blocked maker: poljot"),
    ("Guilloche Dial Ladies Automatic", "blocked word: ladies"),
    ("Women's Enamel Dial Watch", "blocked word: women's"),
    ("Swiss Chronograph Quartz Limited", "blocked word: quartz"),
])
def test_title_gate_drops(cfg, title, why):
    assert wildcard.title_verdict(title, cfg) == why


@pytest.mark.parametrize("title", [
    FORGET_TITLE,
    "Paul Picot Technicum Rattrapante Limited 045/250",
    # Whole words only: Colorado is not Rado, Senior is not Oris.
    "Colorado Estate Find Jump Hour Automatic",
    "Dubey & Schaldenbrand Aerodyn Chronometer",
])
def test_title_gate_keeps(cfg, title):
    assert wildcard.title_verdict(title, cfg) == ""


# --- substance ---------------------------------------------------------------

@pytest.mark.parametrize("spelling", [
    "guilloché", "guilloche", "guiloche", "guillioche", "gilloche",
    "guilloshe", "gioshe", "Guilloched", "engine-turned", "engine turned",
])
def test_guilloche_is_found_however_it_is_spelled(cfg, spelling):
    sig = wildcard.parse_substance(f"Swiss automatic {spelling} dial", "", {}, cfg)
    assert "Guilloché" in labels(sig, "craft")


def test_guilloche_does_not_match_ordinary_words(cfg):
    sig = wildcard.parse_substance("Gilt dial, original guild case, gold filled", "", {}, cfg)
    assert "Guilloché" not in labels(sig)


def test_the_forget_reads_as_everything_it_is(cfg):
    sig = wildcard.parse_substance(FORGET_TITLE, FORGET_DESC, FORGET_ASPECTS, cfg)
    assert {"Split-seconds chronograph", "Chronograph"} <= labels(sig, "complications")
    assert {"Guilloché", "Gold rotor", "Blued hands", "Hand made",
            "Silver dial", "Display back"} <= labels(sig, "craft")
    assert "Edition of 250" in labels(sig, "scarcity")
    assert {"Automatic", "Bracelet", "40mm", "Date"} <= labels(sig, "usability")


def test_a_signal_only_in_the_description_scores_less(cfg):
    in_title = wildcard.parse_substance("Rattrapante automatic", "", {}, cfg)
    in_desc = wildcard.parse_substance("Swiss automatic", "A rattrapante.", {}, cfg)
    pts = lambda sig: next(s["pts"] for s in sig if s["label"] == "Split-seconds chronograph")
    assert pts(in_desc) == pts(in_title) * cfg["description_factor"]


def test_tourbillon_only_counts_when_the_listing_says_swiss(cfg):
    assert "Tourbillon" not in labels(
        wildcard.parse_substance("Flying tourbillon automatic", "", {}, cfg))
    assert "Tourbillon" in labels(
        wildcard.parse_substance("Swiss made tourbillon automatic", "", {}, cfg))


@pytest.mark.parametrize("text, size", [
    ("Limited Edition No. 045/250", 250),
    ("limited to 99 pieces", 99),
    ("Limited edition of only 50", 50),
    ("one out of approx 250", 250),
    ("1 of 30 made", 30),
    ("only 500 ever produced", 500),
    ("Chronograph 12/100 automatic", 100),
])
def test_edition_size(text, size):
    found = wildcard.edition_size([("title", text)])
    assert found and found[0] == size, text


@pytest.mark.parametrize("text", [
    "Box and papers dated 10/2019",
    "Limited edition 2019 release",
    "Serviced 3/2024, runs well",
    "Reference 3706 chronograph",
])
def test_edition_size_ignores_dates_and_references(text):
    assert wildcard.edition_size([("title", text)]) is None


def test_negated_signals_do_not_count(cfg):
    sig = wildcard.parse_substance("Swiss automatic, not a chronograph, no date", "", {}, cfg)
    assert "Chronograph" not in labels(sig)
    # "finish" is not a negation here, whatever Reference Hunt does with it.
    sig = wildcard.parse_substance("Silver dial, guilloche finish, hand blued hands", "", {}, cfg)
    assert {"Guilloché", "Blued hands"} <= labels(sig)


def test_a_stated_run_replaces_the_bare_words(cfg):
    sig = wildcard.parse_substance("Limited edition 12/100 chronograph", "", {}, cfg)
    assert labels(sig, "scarcity") == {"Edition of 100"}


# --- obscurity ---------------------------------------------------------------

def test_obscurity_runs_from_full_to_nothing():
    f = wildcard.obscurity_fraction
    assert f(1, 400) == f(3, 400) == 1.0
    assert f(400, 400) == f(9000, 400) == 0.0
    assert 1.0 > f(10, 400) > f(50, 400) > f(200, 400) > 0.0
    assert f(None, 400) == 0.5


# --- detail gate -------------------------------------------------------------

def detail(**over):
    d = {"brand": "Forget", "text": FORGET_DESC, "aspects": dict(FORGET_ASPECTS),
         "listed": "", "feedback_pct": 100.0, "feedback_score": 400,
         "ag": False, "as_is_excerpt": ""}
    d.update(over)
    return d


def test_detail_gate(cfg):
    assert wildcard.detail_verdict(detail(), cfg) == ""
    assert wildcard.detail_verdict(detail(brand="Unbranded"), cfg) == "no maker named"
    # The title hid it; the Brand field did not.
    assert wildcard.detail_verdict(detail(brand="Breitling"), cfg) == "known maker: breitling"
    assert wildcard.detail_verdict(
        detail(aspects={"Movement": "Quartz"}), cfg) == "quartz"
    assert wildcard.detail_verdict(
        detail(aspects={"Country/Region of Manufacture": "China"}), cfg) == "made in china"
    assert wildcard.detail_verdict(
        detail(aspects={"Year Manufactured": "1940-1949"}), cfg) == "pre-1960"
    assert wildcard.detail_verdict(detail(feedback_pct=88.0), cfg) == "seller under 90%"


# --- a whole sweep, against a pretend eBay -----------------------------------

def summary(iid, title, price, auction=False):
    s = {"itemId": iid, "title": title, "itemWebUrl": f"https://ebay.test/{iid}",
         "buyingOptions": ["AUCTION"] if auction else ["FIXED_PRICE"],
         "price": {"value": str(price), "currency": "USD"},
         "seller": {"username": "estate", "feedbackScore": 400,
                    "feedbackPercentage": "100.0"},
         "itemLocation": {"country": "US"}, "conditionId": "3000",
         "condition": "Pre-owned"}
    if auction:
        s["currentBidPrice"] = {"value": str(price), "currency": "USD"}
        s["bidCount"] = 2
    return s


def item(brand, desc, aspects=None):
    return {"brand": brand, "description": desc,
            "localizedAspects": [{"name": k, "value": v}
                                 for k, v in ({"Brand": brand, **(aspects or {})}).items()],
            "seller": {"feedbackScore": 400, "feedbackPercentage": "100.0"}}


class PretendEbay:
    """Five listings and the live-listing counts of the makers behind them."""

    HITS = {
        "rattrapante": [
            summary("forget", FORGET_TITLE, 3200),
            summary("rolex", "Rolex Daytona rattrapante conversion", 9000),
            summary("hidden", "Navitimer style rattrapante chronograph automatic", 4000),
        ],
        "guiloche": [
            summary("forget", FORGET_TITLE, 3200),
            summary("plain", "Helvetia automatic guiloche dial", 1500),
            summary("picot", "Paul Picot Technicum Rattrapante Limited 045/250 "
                             "Automatic Chronograph", 2600, auction=True),
        ],
    }
    ITEMS = {
        "forget": item("Forget", FORGET_DESC, FORGET_ASPECTS),
        "hidden": item("Breitling", "A chronograph."),
        "plain": item("Helvetia", "Nice watch."),
        "picot": item("Paul Picot", "Split seconds, COSC chronometer."),
    }
    LIVE = {"Forget": 2, "Helvetia": 60, "Paul Picot": 45}

    def __init__(self):
        self.item_calls, self.count_calls = [], []

    def search_summaries(self, query, category, filters, aspect_filter, max_pages):
        auction = any("AUCTION}" in f and "FIXED" not in f for f in filters)
        return [s for s in self.HITS.get(query, [])
                if ("AUCTION" in s["buyingOptions"]) == auction]

    def get_item(self, item_id):
        self.item_calls.append(item_id)
        return self.ITEMS[item_id]

    def count(self, query="", category="", filters=None, aspect_filter=""):
        brand = aspect_filter.split("Brand:{")[1].rstrip("}")
        self.count_calls.append(brand)
        return self.LIVE[brand]


@pytest.fixture
def swept(cfg, tmp_path):
    small = {**cfg, "queries": [{"q": "rattrapante"}, {"q": "guiloche"}]}
    client, storage = PretendEbay(), Storage(tmp_path / "wild.db")
    stats = wildcard.sweep(client, storage, small, now=1_000_000.0)
    yield small, client, storage, stats
    storage.close()


def test_sweep_keeps_the_unknowns_and_says_why_the_rest_went(swept):
    cfg, client, storage, stats = swept
    cards = wildcard.live_cards(storage, cfg, now=1_000_000.0)["items"]
    assert [c["id"] for c in cards] == ["forget", "picot"]
    assert stats["reasons"] == {"known maker": 2, "not enough substance": 1}
    # The Rolex was dropped on its title and never cost a getItem.
    assert "rolex" not in client.item_calls


def test_the_forget_comes_out_on_top_with_its_evidence(swept):
    cfg, _, storage, _ = swept
    top = wildcard.live_cards(storage, cfg, now=1_000_000.0)["items"][0]
    assert top["brand"] == "Forget" and top["brand_live"] == 2
    assert top["via"] == ["rattrapante", "guiloche"]
    assert top["score"] > 65
    rows = {r["label"]: r for r in top["breakdown"]}
    assert rows["Obscurity"]["pts"] == cfg["weights"]["obscurity"]
    assert rows["Complications"]["pts"] > 0 and rows["Scarcity"]["pts"] > 0
    assert round(sum(r["pts"] for r in top["breakdown"]), 1) == top["score"]
    assert all(s["snippet"] for s in top["signals"])


def test_a_second_sweep_reads_no_listing_and_counts_no_maker_twice(swept):
    cfg, client, storage, _ = swept
    client.item_calls.clear(), client.count_calls.clear()
    stats = wildcard.sweep(client, storage, cfg, now=1_000_600.0)
    assert client.item_calls == [] and client.count_calls == []
    assert stats["new"] == []           # nothing is new the second time
    assert stats["shown"] == 2


def test_first_sweep_reports_its_finds_as_new_best_first(swept):
    assert [c["id"] for c in swept[3]["new"]] == ["forget", "picot"]


def test_pass_hides_a_listing_and_restore_brings_it_back(swept):
    cfg, _, storage, _ = swept
    storage.wildcard_set_pass("forget", True)
    live = wildcard.live_cards(storage, cfg, now=1_000_000.0)
    assert [c["id"] for c in live["items"]] == ["picot"]
    assert [c["id"] for c in live["passed"]] == ["forget"]
    storage.wildcard_set_pass("forget", False)
    assert len(wildcard.live_cards(storage, cfg, now=1_000_000.0)["items"]) == 2


def test_blocking_a_maker_removes_it_now_and_on_later_sweeps(swept):
    cfg, client, storage, _ = swept
    storage.wildcard_block_brand("paul picot", "Paul Picot", True)
    assert [c["id"] for c in wildcard.live_cards(storage, cfg, now=1_000_000.0)["items"]] == ["forget"]
    stats = wildcard.sweep(client, storage, cfg, now=1_000_600.0)
    assert stats["reasons"].get("maker you blocked") == 1
    storage.wildcard_block_brand("paul picot", "Paul Picot", False)
    wildcard.sweep(client, storage, cfg, now=1_001_200.0)
    assert len(wildcard.live_cards(storage, cfg, now=1_001_200.0)["items"]) == 2


def test_a_listing_that_sells_drops_off_the_tab(swept):
    cfg, client, storage, _ = swept
    client.HITS = {"rattrapante": [summary("forget", FORGET_TITLE, 3200)]}
    wildcard.sweep(client, storage, cfg, now=1_000_600.0)
    assert [c["id"] for c in wildcard.live_cards(storage, cfg, now=1_000_600.0)["items"]] == ["forget"]


def test_makers_list_grows_rarest_first(swept):
    makers = swept[2].wildcard_makers()
    assert [(m["name"], m["live_count"]) for m in makers] == [("Forget", 2), ("Paul Picot", 45)]


def test_a_maker_with_hundreds_of_listings_is_not_a_wild_card(cfg, tmp_path):
    client, storage = PretendEbay(), Storage(tmp_path / "w.db")
    client.LIVE = {**client.LIVE, "Forget": cfg["known_over"] + 1}
    try:
        stats = wildcard.sweep(client, storage,
                               {**cfg, "queries": [{"q": "rattrapante"}]}, now=1_000_000.0)
    finally:
        storage.close()
    assert stats["shown"] == 0
    assert stats["reasons"]["known maker"] == 3


def test_digest_leads_with_the_best_new_card(swept):
    title, message, url = wildcard.build_digest(swept[3]["new"])
    assert title.startswith("Wild Cards: 2 new")
    assert message.index("Forget") < message.index("Paul Picot")
    assert url == "https://ebay.test/forget"
    assert json.dumps(swept[3]["new"])      # cards are plain JSON
