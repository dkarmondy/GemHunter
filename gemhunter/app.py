"""Main loop: search (wide) -> score (gate→taste→multiplier) -> rank -> alert."""

from __future__ import annotations

import argparse
import sys
import time

from . import report, visual
from .config import Config, load_config
from .ebay import EbayClient, SampleEbayClient
from .notify import Notifier
from .scoring import score_listing
from .storage import Storage


def build_ebay_client(cfg: Config):
    if cfg.has_ebay_keys:
        return EbayClient(cfg.ebay_client_id, cfg.ebay_client_secret, cfg.marketplace)
    print("[i] No eBay keys found — using sample listings (dry-run).")
    return SampleEbayClient()


def run_once(cfg: Config, ebay, storage: Storage, notifier: Notifier) -> int:
    candidates, seen_new, rejected = [], 0, 0

    for search in cfg.searches:
        try:
            listings = ebay.search(search)
        except Exception as exc:                      # keep loop alive on a bad search
            print(f"[!] Search '{search.name}' failed: {exc}")
            continue
        for listing in listings:
            if not storage.is_new(listing.item_id):
                storage.record_observation(listing)
                continue                              # dedupe: never re-alert
            seen_new += 1
            result = score_listing(listing)
            storage.record_result(result)
            if result.rejected:
                rejected += 1
                continue
            candidates.append(result)

    keep = [r for r in candidates if r.score >= cfg.min_score]

    # Optional: enrich top candidates with item specifics (size/movement/…) and re-score.
    if cfg.enrich and keep:
        keep.sort(key=lambda r: r.score, reverse=True)
        for r in keep[: cfg.alert_limit]:
            ebay.enrich(r.listing)
        keep = [score_listing(r.listing) for r in keep]
        keep = [r for r in keep if not r.rejected and r.score >= cfg.min_score]

    keep.sort(key=lambda r: r.score, reverse=True)

    # Optional visual taste bonus: compare listing thumbnail to the anchor set.
    # A ranking nudge only (max +3), applied to the top candidates.
    if cfg.visual and keep and visual.is_available():
        for r in keep[: cfg.alert_limit * 2]:
            b = visual.bonus(visual.taste_margin(r.listing.image_url))
            if b:
                r.score += b
                r.opportunity = min(100.0, getattr(r, "opportunity", 0.0) + b * 4.0)
                r.reasons.append(f"looks{b:+g}")
        keep.sort(key=lambda r: r.score, reverse=True)

    for r in keep:
        storage.record_result(r)   # persist final (enriched + visual) scores

    alerted = keep[: cfg.alert_limit]
    for r in alerted:
        notifier.send_scored(r)

    print(f"[cycle] {seen_new} new · {rejected} filtered · "
          f"{len(keep)} gems ({len(alerted)} alerted)")
    return len(keep)


def main() -> None:
    # Windows consoles default to cp1252 and choke on watch symbols (Ω, é, …).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="GemHunter — eBay watch gem scout")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--db", default="gemhunter.db")
    parser.add_argument("--enrich", action="store_true",
                        help="fetch item specifics for candidates (uses getItem quota)")
    parser.add_argument("--report", action="store_true",
                        help="render gems.html + console summary from the db, then exit")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.enrich:
        cfg.enrich = True

    if args.report:
        report.print_summary(args.db, cfg.min_score)
        n = report.write_report(args.db, "gems.html", cfg.min_score)
        print(f"\nWrote gems.html ({n} gems). Open it in a browser.")
        return

    ebay = build_ebay_client(cfg)
    storage = Storage(args.db)
    notifier = Notifier(cfg.pushover_user_key, cfg.pushover_api_token)
    if not notifier.live:
        print("[i] No Pushover keys found — alerts print to console (dry-run).")

    try:
        if args.once:
            run_once(cfg, ebay, storage, notifier)
        else:
            print(f"[i] Polling every {cfg.poll_interval_seconds}s. Ctrl-C to stop.")
            while True:
                run_once(cfg, ebay, storage, notifier)
                report.write_report(args.db, "gems.html", cfg.min_score)
                time.sleep(cfg.poll_interval_seconds)
    finally:
        storage.close()
        report.write_report(args.db, "gems.html", cfg.min_score)


if __name__ == "__main__":
    main()
