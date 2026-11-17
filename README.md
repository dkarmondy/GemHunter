# GemHunter

> **My private eBay watch tools, served from a Raspberry Pi to my phone: a live
> listing lookup, and the National Rarities weekly auction drop ranked by my own
> taste.**

It's not a deal-finder or a flipping engine. It's **me, externalized**.

## What's in it

- **Listing lookup** (`/item`) — paste any eBay link; get the live listing back,
  summarised (specs, faults, Authenticity Guarantee read) plus the raw JSON.
- **National Rarities** (`/rarities`) — the whole `nationalrarities` consignment,
  ranked by my tiers (Breitling grails, modern IWC, Navitimer, Rolex projects…),
  with condition chips read from the seller's description. Hearts lift similar
  lots; a thumbs-down buries them.
- **Reference Hunt** (`/hunt`) — one exact reference across all of eBay,
  scored: price vs market, warranty left on the card, full set, unpolished,
  Authenticity Guarantee, seller. Tap the score for the breakdown; "What was
  detected" shows the text each badge came from. First preset: Submariner
  Date 126610LN; also Sea-Dweller 16600 and 116600.
- **Wild Cards** (`/wildcard`) — the Top 5 watches nobody is searching for:
  unknown makers with real substance, found by what the watch *is*
  (complication, craft, movement, edition size) and never by its name.
  See [Wild Cards](#wild-cards-how-it-finds-what-cant-be-searched-for).
- **Rarities digest** — a twice-daily Pushover note when new lots appear,
  leading with IWC and Navitimer.
- **Wild Cards digest** — four sweeps a day; pushes only when a new find
  clears `alert_score`.

The original always-on scout (saved searches → scoring → ranked feed + gem
alerts) was retired on 2026-10-21; it lives in git history before that date.

## Identity & guiding principle

GemHunter is a **collector-watchmaker's acquisition scout.** I buy watches to own,
enjoy, and service; **selling is a byproduct.**

The core axiom that makes it coherent:

> **Only buy what I know and love. My taste IS the resale signal.**
> Watches I find cool/weird-in-a-good-way are the ones that hold value and resell —
> the "worth more but I don't like it" buys always go bad.

→ **Taste is the gate; undervaluation is the multiplier.** A listing must first pass
"do I want this?" before "is it a deal?" boosts it. Never surface unloved-but-profitable.

## Quick start

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml       # marketplace + ZIP
cp .env.example .env                      # add eBay keys (+ Pushover for the digest)
python -m gemhunter.webapp --db data/gemhunter.db          # http://localhost:8080
python -m gemhunter.rarities --db data/gemhunter.db --dry-run   # print the digest
python -m gemhunter.wildcard --db data/gemhunter.db --dry-run   # sweep, print the finds
```

Deploying to the Pi: [deploy/DEPLOY.md](deploy/DEPLOY.md).

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

`tests/` pins the parsers (card date, box & papers, polish, bracelet, AG
class, taste tiers, condition chips) and the Reference Hunt score to real
listing wording. Add a case there whenever a listing is misread.
`tests/test_wildcard.py` runs whole Wild Cards sweeps against a pretend eBay;
its benchmark listing is the Forget rattrapante, which must come out on top.

## Docs (the watch knowledge)

See **[docs/README.md](docs/README.md)** for the index: the
[playbook](docs/watch-playbook.md), the [movements DB](docs/chronograph-movements.md),
and [taste-and-targets.md](docs/taste-and-targets.md). The Rarities ranking in
`gemhunter/rarities.py` and `gemhunter/knowledge.py` draws on them.

## Reference Hunt: adding a preset

Every reference the `/hunt` tab chases is a block in
[`presets/hunts.yaml`](presets/hunts.yaml). The file is re-read on every page
load, so a new preset shows up in the tab's dropdown after the Pi's next
`git pull`, with no restart. Copy an existing block and change:

| Key | What it does |
|---|---|
| `id`, `name`, `blurb` | Dropdown id (also `/hunt?preset=<id>`), label, one-line description. |
| `queries` | eBay keyword searches. Results are merged and de-duplicated by itemId. |
| `require` | Regexes the **title** must match, e.g. the reference number. |
| `exclude` | Regexes that drop a listing by title (sister refs, colourways). Added to `defaults.exclude_common` (box only, homage, replica…). |
| `price_min`, `price_max` | The search's price band, USD. |
| `benchmark_low`, `benchmark_high` | Market range. Price points are full at or under `low` and zero at or over `high`. A live auction is scored at the middle of the range until its bid passes it, so an opening bid never looks like a steal. |
| `deal_floor` | A Buy It Now under this gets "Deal OR suspicious" and no price points until you have looked at it. Auctions are exempt. |
| `min_card_year`, `max_card_year` | Years accepted as a warranty-card date. A year whose nearest keyword is a service word (serviced, RSC, overhaul) is a service card, not the warranty card, and is ignored. |
| `bracelet_primary`, `bracelet_cost` | On (Rolex default): a listing on a strap with no bracelet loses half its Condition points, and `bracelet_cost` is added to its price before the market comparison. A strap *plus* the bracelet is fine. Set `bracelet_primary: false` for brands where the bracelet is secondary. |
| `weights` *(optional)* | Override any of `defaults.weights` for this preset. `seller` is feedback count (log scale, 10,000 = full) × rating (97% → 0, 100% → full), plus 15% for Top Rated Plus. |

Anything in `defaults` (category, brand, conditions, warranty years,
"negotiable?" threshold, cache minutes, how many descriptions to read) can be
overridden per preset too. All patterns are case-insensitive Python regexes;
put them in single quotes. Check "Excluded by title" at the bottom of the tab
to see what each rule threw out.

Cost: one Browse search per query (two past 200 hits), cached for
`ttl_minutes`, plus one getItem per listing (up to `detail_top`), cached for the
life of the process. That fits easily inside eBay's 5,000 calls a day.

## Wild Cards: how it finds what can't be searched for

Searching for "unknown watches" leads down the same road as everyone else.
So this tab never searches for a watch. It does three things instead, all
driven by [`presets/wildcard.yaml`](presets/wildcard.yaml):

1. **The net: attributes, not names.** `queries` are searched across the whole
   Wristwatches category: complications (rattrapante, jump hour, perpetual
   calendar), craft words *and their misspellings* (guilloché, guiloche,
   gilloche: a seller who can't spell it doesn't know what they have),
   movement suppliers (Lemania, Frédéric Piguet, Venus, La Joux-Perret) and
   edition wording. Buy It Now runs inside `price_min`–`price_max`; auctions
   have no floor.
2. **The gate: drop every maker the market knows.** A title naming one of
   `known_brands` is dropped for free. Everything else is *measured*: eBay is
   asked how many live Wristwatches listings carry that Brand, and a maker over
   `known_over` is out whoever they are. `blocked_brands`, `blocked_words`,
   quartz, ladies, pre-`min_year` and `blocked_countries` follow the hard
   exclusions in [taste-and-targets.md](docs/taste-and-targets.md).
3. **The score: substance and obscurity**, out of 100, with the text every
   point came from ("Why it is here" on each card).

| Factor | Max | What earns it |
|---|---|---|
| Complications | 25 | Split-seconds 16, perpetual calendar 16, repeater 16, annual calendar 9, flyback / jump hour 7, chronograph 6, regulator / alarm 5, moonphase 4… |
| Obscurity | 20 | Full at 3 or fewer live listings for the maker, zero at `known_over`, log scale between. |
| Craft | 15 | Guilloché, enamel dial, gold rotor 6 each; hand engraved 5; hand made 4; blued hands, chronometer 3… |
| Scarcity | 12 | Stated run: ≤50 → 12, ≤100 → 10, ≤300 → 8, ≤1000 → 5. Pièce unique 10. Bare "limited edition" 4. |
| Movement | 10 | Frédéric Piguet 7; Vaucher, Minerva, column-wheel Valjoux 6; Lemania, Venus, micro-rotor 5; 7750 family 2. |
| Price | 10 | Cheaper inside the band scores more. A live auction gets half: a bid is not a price. |
| Usability | 8 | Automatic, bracelet, 36–42mm, box & papers 2 each; date, serviced 1. |

A signal found only in the description scores `description_factor` (half) of
one in the title, and one found only in item specifics scores
`specifics_factor` (half): those are checkboxes, and the first live sweep
found "Perpetual Calendar" ticked on triple-date 7751s. A complication worth
9 points or more must be in the seller's own words; a ticked box alone does
not count. A listing needs `min_substance` points
from complications + craft + movement + scarcity to be shown at all, so an
obscure maker with nothing to it never makes the list. The point tables are
in `gemhunter/wildcard.py`; the weights, lists and thresholds are in the YAML
and take effect on the next sweep.

**It is a monitor, not a search.** These watches surface a few at a time, so
every sweep is written to SQLite. The tab shows the best of what is live
right now. "Makers found so far" at the bottom is the private list of
unknowns this builds up over time.

**It refines itself two ways.**

- **Hearts and passes teach the ranking.** Each card is reduced to its maker,
  the things it was scored for, and the words of its title. A heart adds
  weight to each, a pass takes it away, so what is on both piles
  ("chronograph") cancels out and what separates them carries. Every card
  then gets a **Your taste** row of up to ±`taste_max` points, naming what
  moved it. Votes re-rank the tab at once, and keep counting after the
  listing has sold. Pass also hides the listing; **I know this maker**
  removes a maker for good.
- **Found makers are followed.** Every maker that has put a card on the tab
  is searched *by name* on each later sweep (`follow_max` makers, one call
  each, hearted makers first, then the rarest). That catches their watches
  when the title has none of the net's words. They still need
  `min_substance` and the Buy It Now floor, unless you have hearted one of
  that maker's watches: then everything of theirs is shown.

Cost per sweep: two searches per query (up to `pages` each, about 140 calls at
most), one getItem per listing never read before (capped at
`detail_per_sweep`), one search per followed maker, one count per maker not
measured in `brand_ttl_days`. A
listing's page is read once and kept. The first sweeps work through the
backlog; after that a sweep is mostly the searches.
