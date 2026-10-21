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
