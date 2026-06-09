"""Render the saved gems as a browsable HTML page (two sections) + console summary."""

from __future__ import annotations

import html
import time
from collections import Counter
from pathlib import Path

from .storage import Storage

STREAMS = [
    ("repair", "🔧 For parts / repair", "#b45309"),
    ("collector", "🎯 Box & papers — nice / full set", "#15803d"),
]


def _card(r: dict, color: str) -> str:
    kind = "Auction" if r["buying_option"] == "AUCTION" else "BIN"
    bids = f" · {r['bid_count']} bids" if r["buying_option"] == "AUCTION" and r["bid_count"] else ""
    seller = f"{r['seller_pct']:.0f}% ({r['seller_score']})" if r["seller_pct"] else "—"
    img = r["image_url"] or ""
    return f"""
    <a class="card" href="{html.escape(r['url'])}" target="_blank">
      <div class="thumb">{'<img src=' + html.escape(img) + '>' if img else ''}</div>
      <div class="body">
        <div class="row1">
          <span class="score" style="background:{color}">{r['score']:.0f}</span>
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
    when = time.strftime("%Y-%m-%d %H:%M")
    sections, total = [], 0
    for stream, title, color in STREAMS:
        rows = s.top_gems(min_score, limit, stream=stream)
        total += len(rows)
        cards = "".join(_card(r, color) for r in rows) or '<p class="empty">— none yet —</p>'
        sections.append(
            f'<h2 style="border-color:{color}">{title} '
            f'<span class="count">{len(rows)}</span></h2><div class="grid">{cards}</div>')
    s.close()

    doc = f"""<!doctype html><meta charset="utf-8">
<title>GemHunter — saved gems</title>
<style>
 body{{font:14px/1.4 system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}}
 h1{{font-size:18px;margin:0 0 2px}} .sub{{color:#94a3b8;margin-bottom:20px}}
 h2{{font-size:15px;margin:26px 0 12px;padding-left:10px;border-left:4px solid}}
 .count{{color:#94a3b8;font-weight:400;font-size:13px}}
 .empty{{color:#64748b;margin:0 0 0 12px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}}
 .card{{display:flex;gap:12px;background:#1e293b;border-radius:10px;overflow:hidden;
        text-decoration:none;color:inherit;border:1px solid #334155}}
 .card:hover{{border-color:#64748b}}
 .thumb{{width:96px;min-width:96px;background:#0b1220;display:flex;align-items:center}}
 .thumb img{{width:96px;height:96px;object-fit:cover}}
 .body{{padding:10px 12px 10px 0;min-width:0}}
 .row1{{display:flex;align-items:center;gap:8px;margin-bottom:4px}}
 .score{{font-weight:700;font-size:15px;border-radius:6px;padding:1px 8px;color:#fff}}
 .price{{margin-left:auto;font-weight:600}} .price small{{color:#94a3b8;font-weight:400}}
 .title{{font-weight:600;margin-bottom:3px}}
 .reasons{{color:#7dd3fc;font-size:12px;margin-bottom:3px}}
 .seller{{color:#94a3b8;font-size:12px}}
</style>
<h1>GemHunter — {total} saved gems</h1>
<div class="sub">score ≥ {min_score:g} · updated {when}</div>
{''.join(sections)}"""
    Path(out_path).write_text(doc, encoding="utf-8")
    return total


def print_summary(db_path: str, min_score: float = 0.0, top: int = 10) -> None:
    s = Storage(db_path)
    st = s.stats()
    print(f"\nDataset: {st['total']} listings seen · {st['rejected']} filtered · "
          f"{st['gems']} gems")
    for stream, title, _ in STREAMS:
        rows = s.top_gems(min_score, 1000, stream=stream)
        buckets = Counter(int(r["score"] // 2) * 2 for r in rows)
        print(f"\n{title}  —  {len(rows)} gems (score ≥ {min_score:g})")
        for b in sorted(buckets, reverse=True):
            print(f"  {b:>3}-{b+1}: {'█' * buckets[b]} {buckets[b]}")
        for r in rows[:top]:
            print(f"    {r['score']:>4.0f} ${r['price']:>8,.0f}  {(r['title'] or '')[:56]}")
    s.close()
