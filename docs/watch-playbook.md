# The Watch Playbook

The heart of GemHunter: the user's hard-won knowledge for spotting **fakes/scams**
and **undervalued opportunities** on eBay pre-owned watches. Built collaboratively;
grounded in his real service logs and buy/sell history (`E:\WATCHES`).

The user is a **hobbyist watchmaker** — he buys broken watches, fully services them
himself, and holds or sells. ~28 movements fully serviced to date. This is why
"as-is / broken / for parts" is a **buy signal**, not a red flag — *if* he can
service the movement and source parts.

Most entries are human judgment first. The **auto-detectable** ones (text, price,
seller, movement caliber) become rules in the gem detector; the rest become
human-review guidance attached to alerts.

---

## Scoring philosophy — taste is the gate

GemHunter is a **collector-watchmaker's scout**: buy what I know and love; selling is
a byproduct. **My taste is the resale signal** — the unloved-but-"worth-more" buys go
bad. So the scorer is ordered, not a flat sum:

1. **Hard gates (pass/fail)** — exclusions (Russian/Chinese/Japanese movements, fashion/quartz, non-watch accessories), seller floor, scam/condition red-lines, age floor ~1960. Fail any → drop.
2. **Taste gate** — do I know/love this, or is it cool/weird-in-a-good-way? Swiss vintage/neo-vintage chronographs, my brands, right size/grade/feel. Below threshold → drop, *even if it's a steal.*
3. **Then multipliers** — undervaluation, serviceable movement (column-wheel/grade), as-is opportunity, off-beat rarity. These **boost things that already passed**, never rescue an unloved watch.

Each surfaced gem is tagged with a **mode**: 🔧 serviceable project · 🎯 wishlist · 📉 undervalued/weird-good.

---

## 0. The two acquisition strategies

### Path A — Minty & complete (condition play)
Buy exceptional, original-condition examples and hold; condition + completeness
appreciates, especially vintage / neo-vintage.
- **Traits:** super minty, **unpolished**, serviced, **box & papers / full set** (verified genuine), NOS ideal, **high-rated seller**.
- **Rolex lives here**, never Path B — a for-repair Rolex costs about as much as a clean one after service, so repairing it is uneconomic.
- **Proof:** Rolex Explorer **114270**, ~$3k years ago (NOS, minty, box & papers) → ~$8k now.

### Path B — As-is / for-repair (service play) ← PRIMARY FOCUS
Buy broken watches sellers don't understand, do a full service, unlock value others won't touch.
- **Why the edge exists:** pro chrono service is **$600–1,000+**, so sellers dump them "as-is, no warranty, broken" — often not knowing what they have. The deterrent IS the opportunity.
- **Hard requirement:** must be a **movement he can service AND source parts for** (see §2c). Biggest single filter on Path B.
- **Where value hides:** Swiss, **vintage & neo-vintage**, **off the beaten path** — obscure, special references most people miss.
- **Economics:** must clear eBay fees + taxes to profit; goal is profit or break-even on watches he loves.
- **Real flips:** Tiffany Mark T-57 (ETA 2892) $275→$500; Panerai PAM112 $335→$650.

---

## 1. Authenticity — is it real?  (cut fast on any of these)

- **Price far below the known floor** for that reference (esp. Patek, Rolex) → assume fake/scam. "A couple thousand under what you'd expect" + an off dial = done.
- **Obviously fake dial** — wrong font/printing/spacing, especially on Patek & Rolex. Trained eye on the photos kills these instantly.
- **Seller with poor reviews → assume fake, cut it.** Trusting the seller is paramount; a great-looking deal from a bad-feedback seller is not a deal.
- **Spec / detail mismatches (franken / misrepresented):** e.g. a Rolex listed as "new" with the **wrong / older clasp**; era-incorrect components; mismatched caseback/movement. *(User has a long list of model-specific tells — to expand.)*
- ⚠️ **Misspellings / crappy descriptions are NOT fake signals** — they're opportunity (see §3). Only cut on them if seller trust or the photos fail.

**Mostly human/photo judgment** (auto can only proxy via price + seller). Per-brand
checklists from published "how to spot a fake" guides; key tells the user uses:
- **Rolex (the trickiest — best-quality fakes):** rehaut engraving must line up — each letter aligned to an index/minute marker; **fakes are always off**. Check case thickness, **dial sharpness/printing**, and on a display caseback a **Miyota movement = instant fake**.
- **A movement photo goes a long way** — it both confirms authenticity and (its absence) flags the underpriced gems where the seller doesn't know.
- Most fakes are obvious; the dangerous ones are high-end Rolex. When in doubt + low price + weak seller → cut.

---

## 1B. Seller trust model

Seller signal is **two-dimensional** — both a fake-risk gate AND a value signal, and
they pull in opposite directions:
- **Trusted watch sellers** (dealers, collectors, auction houses) = **safe but fairly priced** → fewer gems.
- **Incidental / estate / "grandpa's watch" sellers** = **low seller-trust but highest gem potential** — they don't know what they have, list it dirty and cheap. **This is where the value is.** Derisk with photo authentication (§1), not seller reputation.

→ The tool **classifies** seller type to inform ranking; it does **not** simply gate to
high-feedback watch dealers (that would filter out the gems).

**Feedback gate (count × %, they interact):**
- **< 20 sales → skip** (too unproven).
- Low/mid count → require **~100%**.
- High volume (≥ ~1,000 sales) → **95% is acceptable** — volume naturally accrues some unhappy buyers, mostly on low-value items.
- **< 90% → always cut.**

**What they sell matters more than the count** (clientele drives feedback):
- ✅ **Watch-only / watch-mostly**, or niche-specialist (technical/musical instruments, audio, car gear) → trust higher even at lower %. Their buyers know watches, read descriptions, leave fair feedback.
- ⛔ **Fashion / common low-end watch** volume → clueless clientele → complaints → dragged-down feedback → steer away (also higher fake risk). *(Tiffany example: 4 new non-watch buyers ignored the clearly-noted small bracelet / non-original crown, then cancelled.)*
- ⛔ **Cheap-junk volume** (Amazon/Chinese $5–20 random goods) → "how did they even get these watches?" → avoid.
- ✅ Known-good channels: **National Rarities** (pawn/jewelry aggregator — bought many), thrift/estate stores (Deseret Industries), auction houses, and **estate sales** (highly valued = the source of cheap diamonds-in-the-rough).

**Auto-detectability:** feedback count & % come straight from the Browse API (easy gate).
Seller *type* needs a look at the seller's other listings — Browse API supports
`filter=sellers:{username}`, so we can pull their active inventory, compute the category
mix, and classify (watch-focused vs junk-volume vs incidental). One extra cached call per
candidate seller. Worth building.

---

## 2. Condition & repairability ← core of Path B

Separate **undervalued-but-repairable** from **unrepairable junk.**

### 2a. Hard disqualifiers — junk, walk away
- **Rust in the movement — hard no.** "I don't buy any watches with rust." Cut on sight.
- **Water-damage tells:** rust around the crown at **3 o'clock on the dial**, dial warping/staining, heavy patina. Water got in → assume the worst on an as-is vintage piece.
- **Missing chronograph parts you can't source:** missing **pushers** or **hands** → vintage parts (e.g. '70s Heuer hands) are unobtainable → skip. *(A missing **crown** is fine — easy to replace.)*
- **Capped / plated / gold-filled cases** — plating wears through and becomes a problem. Want **solid steel** or **solid gold**.
- **Pop-off / snap casebacks on vintage** — not water resistant, water gets in easily. Prefer **screw-down caseback + screw-down crown**.
- **Radium dials** — burn marks / lines that can't be cleaned up. Avoid.
- **Redialed / refinished dial** — most of a vintage watch's value is in the dial; an original dial is paramount.

### 2b. Green flags — a Path B gem
- Movement is on the **serviceable list** (§2c) and the fault reads as "just needs a service."
- "**As-is / broken / untested / for parts**" + sourceable movement + reputable seller.
- **Box & papers** (genuine), **unpolished**, sharp lugs.
- **Solid steel case, screw-down crown + caseback.**
- **Original dial (low patina), original hands, original pushers.**
- Current sweet spot: **column-wheel chronograph, black dial, >35mm, solid stainless, screw-down back** → high demand, liquid, holds value (see targets doc).

### 2c. Movements — serviceability & parts (from his actual service logs)

| Movement | Status | Evidence / notes |
|----------|--------|------------------|
| **Valjoux 7750** | ✅ CORE | serviced many: LeJour ×2, IWC 3706 (modified), PD×IWC, Bulova; parts cheap & common |
| **Valjoux 92** | ✅ proven | Norexa chrono |
| **Valjoux 72** | ✅ proven (pricier) | Gigandet; findable but costs more |
| **Valjoux 730** | ✅ proven | Wakmann triple-calendar |
| **Venus 175** | ✅ proven (fiddly) | Breitling Chronomat — **column-wheel**; sourced hammer spring/balance/mainspring, but finicky & pricey |
| **Landeron 148** | ✅ proven | Bovet chrono |
| **ETA 2892-A2** | ✅ proven | Omega SM300, Tiffany (flip); parts available |
| **ETA 2824-2** | ✅ proven | parts/stems easy |
| **AS 1130** | ✅ proven | Technos diver |
| **Vulcain 120 (Cricket)** | ✅ proven | sourced baseplate |
| **Vintage JLC K881/883** | ⚠️ doable, parts HARD | serviced, but couldn't source a mainspring |
| **Vintage Patek / Lemania 2310** | ⛔ parts scarce | route Patek to Path A (minty) |
| **Certain Vacheron** | ⛔ parts scarce | confirm which calibers |
| **Rolex (any)** | ⛔ don't repair-buy | economics — Path A only |

> **Rule:** parts availability > everything on Path B. Prioritize listings whose
> caliber is in the ✅ list; flag ⚠️; skip ⛔. **Also skip Valjoux 7733/7734/7736**
> despite being serviceable — personal dislike (cam-actuated, noisy).

### 2d. Serviceability & era — effort/risk modifiers

Whether a *desirable* caliber is actually worth bench time. These are largely
caliber/era-intrinsic, so they can be scored:

- **Age floor ~1960 — don't surface older.** Applies to the *watch's* age, not the caliber's intro year (a 1968 Valjoux 72 is fine; a 1948 one isn't). Pre-'60s = fragile parts that break, old tech, hard to regulate well and reassemble.
- **Shock protection (Incabloc or equivalent) — valued, want it.** Pre-shock-protection movements have fragile balance staffs → high risk. Lack of it is a serviceability red flag.
- **Movable (adjustable) stud carrier — preferred.** Much nicer/finer to regulate. Example: Gigandet Valjoux **72 = fixed** (harder) vs Wakmann **730 = movable** (preferred).
- **Hairspring type:** flat = robust/easier; **Breguet overcoil = higher grade but fragile/hard** (bends and won't recover — his Chronomat). Older hairsprings fragile in general.
- **Net:** a pre-1960, no-shock-protection, overcoil, fixed-stud movement is a **high-effort or skip** even if the caliber is desirable. The pre-'60s CW greats (Angelus, Minerva, early Venus, Longines 13ZN) are **admired but age-capped** — surface only if truly exceptional.

### His repair track record (value anchors — real figures)

| Watch | Ref | Movement | Paid (total) | Now / sold |
|-------|-----|----------|--------------|-----------|
| Rolex Explorer (Path A) | 114270 | — | $4,630 | ~$8k |
| IWC Fliegerchronograph | 3706 | Valjoux 7750 (mod) | $3,089 | $5–6k |
| Breitling Navitimer (blue) | A23322 | Valjoux/ETA 7753 | $3,235 (broken) | — |
| Gigandet | — | Valjoux 72 | $1,975 (broken) | — |
| Wakmann triple-calendar | 72.1309.70 | Valjoux 730 | $1,567 (broken) | — |
| PD × IWC | 3701 | Valjoux 7750 (IWC 790) | $1,082 | — |
| Bulova chrono | 741610 | Valjoux 7750 | $410 ("great price") | — |
| Tiffany Mark T-57 | — | ETA 2892-A2 | $275 (broken) | **sold $500** |
| Panerai Luminor | PAM112 | — | $335 (broken) | **sold $650** |

---

## 3. Undervaluation — why is this mispriced in my favor?

The good mistakes sellers make → less competition → he buys cheap (when photos verify legit & seller is reputable):
- **Misspelled brand/model** in the title (fewer buyers find it). VALUE↑
- **Crappy / vague description**, **no reference number**, **wrong category**. VALUE↑
- **Seller doesn't know what they have** — "as-is, untested" on a sourceable movement (overlaps §2b). VALUE↑

---

## 4. What to surface (forward targets)

In priority order, the alerter should hunt for:
1. **Vintage Patek needing repair** — the holy grail; very rare, parts hard, but surface it whenever it appears (top of the weighted list).
2. **Undervalued column-wheel chronographs** — black dial, >35mm, solid steel, screw-down back; serviceable caliber.
3. **As-is / for-repair with a sourceable movement**, undervalued (Path B core).
4. **Minty pre-owned & brand-new undervalued** (Path A), incl. the active Rolex Submariner / Daytona 116520 hunt.

### Channel & role (important)
- **Auctions are the primary channel** — the best BIN gems get sniped before he can act, but auctions run ~7 days and give time to evaluate. → prioritize **auction discovery + a "closing soon" reminder**, and the **final-price forecaster** rises in value.
- **Gems hide in bad listings** — poor photos, weak/garbled descriptions, **no movement named**, dirty/scratched. Roughly the **top ~15%** of as-is/for-repair is worth anything; the rest is junk. The diamond is under the dirt, a missing crown, a scratched dial.
- **Therefore the tool is a funnel, not an autobuyer.** It can't filter on the very attributes that make a gem (caliber, reference) because the gem listings omit them. So: cast a **wide net** (brand/era/chrono terms), rank by what's knowable (the [movement/reference DB](chronograph-movements.md) + price + seller trust), and **surface candidates with their photos** for the user's expert eye.

Full brand/model priorities, weighting, and the dated wishlists live in
[taste-and-targets.md](taste-and-targets.md). The movement/reference value database
(the core IP) lives in [chronograph-movements.md](chronograph-movements.md).
