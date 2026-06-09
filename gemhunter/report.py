"""Render the saved gems as a browsable HTML page + a console summary."""

from __future__ import annotations

import html
import time
from collections import Counter
from pathlib import Path

from .storage import Storage

MODE_BADGE = {
    "project": ("🔧 project", "#b45309"),
    "wishlist": ("🎯 wishlist", "#15803d"),
    "undervalued": ("📉 undervalued", "#1d4ed8"),
}


def _card(r: dict) -> str:
    label, color = MODE_BADGE.get(r["mode"] or "", (r["mode"] or "•", "#475569"))
    kind = "Auction" if r["buying_option"] == "AUCTION" else "BIN"
    bids = f" · {r['bid_count']} bids" if r["buying_option"] == "AUCTION" and r["bid_count"] else ""
    seller = (f"{r['seller_pct']:.0f}% ({r['seller_score']})"
              if r["seller_pct"] else "—")
    img = r["image_url"] or ""
    return f"""
    <a class="card" href="{html.escape(r['url'])}" target="_blank">
      <div class="thumb">{'<img src=' + html.escape(img) + '>' if img else ''}</div>
      <div class="body">
        <div class="row1">
          <span class="score">{r['score']:.0f}</span>
          <span class="badge" style="background:{color}">{label}</span>
          <span class="price">${r['price']:,.0f} <small>{kind}{bids}</small></span>
        </div>
        <div class="title">{html.escape(r['title'] or '')}</div>
        <div class="reasons">{html.escape(r['reasons'] or '')}</div>
        <div class="seller">seller {seller} · {html.escape(r['search_name'] or '')}</div>
      </div>
    </a>"""


def write_report(db_path: str, out_path: str = "gems.html",
                 min_score: float = 0.0, limit: int = 300) -> int:
    s = Storage(db_path)
    rows = s.top_gems(min_score, limit)
    s.close()
    when = time.strftime("%Y-%m-%d %H:%M")
    cards = "".join(_card(r) for r in rows)
    doc = f"""<!doctype html><meta charset="utf-8">
<title>GemHunter — saved gems</title>
<style>
 body{{font:14px/1.4 system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}}
 h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#94a3b8;margin-bottom:18px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}}
 .card{{display:flex;gap:12px;background:#1e293b;border-radius:10px;overflow:hidden;
        text-decoration:none;color:inherit;border:1px solid #334155}}
 .card:hover{{border-color:#64748b}}
 .thumb{{width:96px;min-width:96px;background:#0b1220;display:flex;align-items:center}}
 .thumb img{{width:96px;height:96px;object-fit:cover}}
 .body{{padding:10px 12px 10px 0;min-width:0}}
 .row1{{display:flex;align-items:center;gap:8px;margin-bottom:4px}}
 .score{{font-weight:700;font-size:16px;background:#0b1220;border-radius:6px;padding:1px 8px}}
 .badge{{font-size:11px;padding:1px 7px;border-radius:10px;color:#fff}}
 .price{{margin-left:auto;font-weight:600}} .price small{{color:#94a3b8;font-weight:400}}
 .title{{font-weight:600;margin-bottom:3px}}
 .reasons{{color:#7dd3fc;font-size:12px;margin-bottom:3px}}
 .seller{{color:#94a3b8;font-size:12px}}
</style>
<h1>GemHunter — {len(rows)} saved gems</h1>
<div class="sub">score ≥ {min_score:g} · updated {when}</div>
<div class="grid">{cards}</div>"""
    Path(out_path).write_text(doc, encoding="utf-8")
    return len(rows)


def print_summary(db_path: str, min_score: float = 0.0, top: int = 15) -> None:
    s = Storage(db_path)
    st = s.stats()
    rows = s.top_gems(min_score, 1000)
    s.close()
    modes = Counter(r["mode"] for r in rows)
    buckets = Counter(int(r["score"] // 2) * 2 for r in rows)  # 2-pt buckets
    print(f"\nDataset: {st['total']} listings seen · {st['rejected']} filtered · "
          f"{st['gems']} gems")
    print(f"Gems with score ≥ {min_score:g}: {len(rows)}  "
          f"({', '.join(f'{m}:{n}' for m, n in modes.most_common())})")
    print("Score distribution:")
    for b in sorted(buckets, reverse=True):
        print(f"  {b:>3}-{b+1}: {'█' * buckets[b]} {buckets[b]}")
    print(f"\nTop {top}:")
    for r in rows[:top]:
        print(f"  {r['score']:>4.0f} [{r['mode'][:4]}] ${r['price']:>7,.0f}  "
              f"{(r['title'] or '')[:58]}")
