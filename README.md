# GemHunter

> **My personal eBay watch-scout: an always-on assistant that filters the firehose
> of listings down to the handful worth my expert eye — undervalued Swiss
> vintage/neo-vintage chronographs I can service or collect — ranked by my own
> knowledge, so I never miss one and never wade through junk.**

It's not a deal-finder or a flipping engine. It's **me, externalized** — it replaces
my manual daily eBay trawl with a ranked, personalized feed.

## Identity & guiding principle

GemHunter is a **collector-watchmaker's acquisition scout.** I buy watches to own,
enjoy, and service; **selling is a byproduct.**

The core axiom that makes it coherent:

> **Only buy what I know and love. My taste IS the resale signal.**
> Watches I find cool/weird-in-a-good-way are the ones that hold value and resell —
> the "worth more but I don't like it" buys always go bad.

→ **Taste is the gate; undervaluation is the multiplier.** A listing must first pass
"do I want this?" before "is it a deal?" boosts it. Never surface unloved-but-profitable.

## What counts as a "gem" — 3 modes, one pipeline

Every candidate runs through the same find → score → rank → alert pipeline, tagged by mode:

- 🔧 **Serviceable project** — undervalued broken/as-is watch with a movement I can service (my main edge).
- 🎯 **Wishlist acquisition** — a specific piece I love appearing at a fair-or-better price.
- 📉 **Undervalued / weird-in-a-good-way** — off-beat, special, mispriced — but only if it passes the taste gate.

## How it works

```
wide-net eBay searches (Swiss vintage/neo-vintage chronographs, auctions + as-is)
        │
        ▼
   the ranking brain  ──►  excludes · movement/caliber value · seller trust ·
   (my knowledge)          condition · serviceability · taste/size/grade · price
        │
        ▼
   rank → keep only the top ~15% → phone alert (with photos) for my eye
```

The **ranking brain** is the moat — it's what makes the feed *mine*. It lives in `docs/`
(see the [docs index](docs/README.md)).

## Status & roadmap

- **Phase 1 — alerting MVP** ✅ plumbing live: real eBay Browse API (OAuth, BIN + auction prices), dedupe, Pushover. *Next:* the scoring layer + real searches.
- **Phase 2 — the scoring brain** ▶ next: encode the playbook (excludes → movement → seller → condition → taste) so runs produce a *ranked* gem list, not a raw dump.
- **Phase 3 — auction tracking** — record bid/time snapshots → builds the dataset.

**Parked (named, not half-built):**
- **Final-price forecaster** — predict where auctions close. Data-starved until Phase 3 collects history; revisit then.
- **Google Sheet control panel** — edit searches from my phone. Nice-to-have; local config is fine for now.
- **Profit / net-gain tracking** — selling is a byproduct, so this is low priority.

## Quick start

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml      # edit searches
cp .env.example .env                     # add eBay + Pushover keys (works in dry-run without them)
python main.py --once                    # one cycle
python main.py                           # poll forever
```

## Docs (the ranking brain)

See **[docs/README.md](docs/README.md)** for the index. Two layers:
- **Operational** (executed): [playbook](docs/watch-playbook.md) · [movements](docs/chronograph-movements.md) · targets/searches in [taste-and-targets.md](docs/taste-and-targets.md).
- **Taste reference** (informs weights): brand affinities, wishlist, wear-revealed preferences.
