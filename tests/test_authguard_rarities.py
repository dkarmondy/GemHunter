"""Authenticity Guarantee classes and the National Rarities taste/condition reads."""

import pytest

from gemhunter import authguard as ag
from gemhunter import rarities as nr


# --- Authenticity Guarantee ----------------------------------------------------

def item(condition_id=3000, ag_on=True, **text):
    d = {"conditionId": str(condition_id),
         "qualifiedPrograms": [ag.AG_PROGRAM] if ag_on else []}
    d.update(text)
    return d


@pytest.mark.parametrize("payload, klass", [
    (item(conditionDescription="Currently not running, needs service."),
     ag.AG_ARBITRAGE),
    (item(conditionDescription="Running well, light wear on the bezel."),
     ag.AG_CLEAN),
    (item(), ag.AG_UNKNOWN),                                 # nothing to read
    (item(7000, conditionDescription="For parts."), ag.HONEST_BROKEN),
    (item(ag_on=False, conditionDescription="Not running."), ag.NO_AG_OTHER),
])
def test_classify(payload, klass):
    assert ag.classify(payload)["auth_arbitrage_class"] == klass


def test_for_parts_never_has_ag_even_if_flagged():
    assert ag.classify(item(7000))["has_authenticity_guarantee"] is False


@pytest.mark.parametrize("text", [
    "Sold as is.", "sold as-is", "Doesn’t run", "needs a service",
    "not a running watch",
])
def test_as_is_wording_variants(text):
    assert ag.as_is_terms(text), text


# --- National Rarities taste ---------------------------------------------------

def test_tier_order():
    grail = nr.taste("Breitling Navitimer Perpetual Calendar Chronograph 42mm #W1")
    iwc = nr.taste("IWC Portugieser Chronograph 41mm #W2")
    navi = nr.taste("Breitling Navitimer A23322 41mm #W3")
    rolex_project = nr.taste("Rolex Submariner 16610 Needs Service 40mm #W4")
    omega = nr.taste("Omega Speedmaster Professional 42mm #W5")
    assert grail > iwc > navi > rolex_project > omega


def test_stock_code_marks_accessories():
    assert nr.is_accessory("rolex datejust 16030 engine turned bezel #w314298") is False
    assert nr.is_accessory("lot of three rolex dials #wb5702") is True
    assert nr.taste("Rolex Dial Lot #WB5702") == nr.W_ACCESSORY


@pytest.mark.parametrize("title, reason", [
    ("Ladies Rolex Datejust 26mm #W1", True),
    ("Breitling Quartz Chronograph #W2", True),
    ("IWC Pilot Chronograph 43mm #W3", False),
])
def test_excluded(title, reason):
    assert bool(nr.excluded(title)) is reason


def test_case_mm_ignores_lug_widths():
    assert nr.case_mm("rolex gmt 40mm 20mm bracelet") == 40.0
    assert nr.case_mm("lug width 18mm") is None


def test_condition_flags_skip_the_house_boilerplate():
    text = ("Currently running. Crown is loose. As an estate watch it will "
            "likely require a service and is not running to spec.")
    labels = [f["label"] for f in nr.condition_flags(text)]
    assert labels == ["CROWN FAULT", "RUNNING"]


def test_running_chip_dropped_next_to_a_fault():
    labels = [f["label"] for f in
              nr.condition_flags("Currently running but the crystal is cracked.")]
    assert labels == ["CRACKED"]
