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
  Date 126610LN; also Sea-Dweller 16600.
- **Rarities digest** — a twice-daily Pushover note when new lots appear,
  leading with IWC and Navitimer.

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
```

Deploying to the Pi: [deploy/DEPLOY.md](deploy/DEPLOY.md).

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
| `benchmark_low`, `benchmark_high` | Market range. Price points are full at or under `low` and zero at or over `high`. |
| `deal_floor` | Under this, the card gets "Deal OR suspicious". |
| `min_card_year`, `max_card_year` | Years accepted as a warranty-card date. |
| `weights` *(optional)* | Override any of `defaults.weights` for this preset. |

Anything in `defaults` (category, brand, conditions, warranty years,
"negotiable?" threshold, cache minutes, how many descriptions to read) can be
overridden per preset too. All patterns are case-insensitive Python regexes;
put them in single quotes. Check "Excluded by title" at the bottom of the tab
to see what each rule threw out.

Cost: one Browse search per query (two past 200 hits), cached for
`ttl_minutes`, plus one getItem per listing (up to `detail_top`), cached for the
life of the process. That fits easily inside eBay's 5,000 calls a day.
