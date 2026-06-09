# GemHunter docs — the ranking brain

These docs ARE the product's edge: my watch knowledge, structured so the scout can
rank listings the way I would. See the [mission](../README.md) for the big picture.

**Design axiom:** taste is the gate, undervaluation is the multiplier. A listing must
pass "do I know/love this?" before "is it a deal?" boosts it. The 3 gem modes
(🔧 serviceable project · 🎯 wishlist · 📉 undervalued/weird-good) all run one pipeline.

## Two layers

### 1. Operational — executed by the scorer
What the scout actually searches and ranks on:

| Doc | Role |
|-----|------|
| [watch-playbook.md](watch-playbook.md) | The scoring brain: scoring philosophy, authenticity/fake tells, **seller trust model**, condition red-lines, serviceability, movement/parts logic. |
| [chronograph-movements.md](chronograph-movements.md) | The caliber DB (core IP): column-wheel vs cam, era, grade, serviceability, **reference→caliber** map, exclusions, data sources. |
| [taste-and-targets.md](taste-and-targets.md) → *Search recipes / Weighting / Exclusions / Seller gate* | The operational targeting: wide-net **search recipes**, weighting ladder, hard excludes, sizing/material constraints. |

### 2. Taste reference — informs weights, not executed directly
Who I am as a collector, so the weights reflect *my* eye:

| Source | Role |
|--------|------|
| [taste-and-targets.md](taste-and-targets.md) → *Brand affinities / Revealed preference / Movement philosophy* | Brands I hunt, grail north-stars (Datograph), wear-log–revealed preferences, grade/feel preferences. |
| `E:\WATCHES` (not in repo) | Source data: full buy/sell inventory, service logs, wear logs. Extracted summaries feed the docs above. |

## Roadmap (mirrors the mission)

- **Phase 1** ✅ alerting plumbing live (real eBay).
- **Phase 2** ▶ the scoring brain — turn the firehose into a ranked top-~15%.
- **Phase 3** auction tracking → builds the forecaster dataset.
- **Parked:** final-price forecaster · Google Sheet control panel · profit tracking.
