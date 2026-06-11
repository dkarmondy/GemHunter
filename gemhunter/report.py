"""Render the saved gems as a tabbed, single-page HTML app + console summary."""

from __future__ import annotations

import html
from collections import Counter
from datetime import datetime
from pathlib import Path

from .storage import Storage

try:
    from zoneinfo import ZoneInfo
    MOUNTAIN = ZoneInfo("America/Denver")   # auto-handles MST/MDT
except Exception:                            # py<3.9 or missing tzdata
    MOUNTAIN = None

# (stream, tab label, accent color, max items or None, min score for THIS section)
# Repair keeps a lower bar — the best as-is finds omit the caliber and score lower.
STREAMS = [
    ("repair", "🔧 For Parts", "#f59e0b", None, 6),
    ("chrono", "⏱ Chronos", "#3b82f6", None, 10),
    ("taste", "💎 Taste", "#a855f7", None, 10),
    ("rolex", "🎯 Box &amp; Papers Rolex", "#22c55e", 10, 10),
    ("patek", "👑 Patek", "#eab308", None, 10),
]


def _now_str() -> str:
    now = datetime.now(MOUNTAIN) if MOUNTAIN else datetime.now().astimezone()
    hour12 = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    return f"{now.month}/{now.day}/{now.year} {hour12}:{now.minute:02d} {ampm} {now.strftime('%Z')}"


def _card(r: dict, color: str) -> str:
    kind = "Auction" if r["buying_option"] == "AUCTION" else "BIN"
    bids = f" · {r['bid_count']} bids" if r["buying_option"] == "AUCTION" and r["bid_count"] else ""
    seller = f"{r['seller_pct']:.0f}% ({r['seller_score']:,})" if r["seller_pct"] else "—"
    img = r["image_url"] or ""
    thumb = f'<img src="{html.escape(img)}" loading="lazy">' if img else ""
    return f"""
    <a class="card" href="{html.escape(r['url'])}" target="_blank" rel="noopener">
      <div class="thumb">{thumb}</div>
      <div class="body">
        <div class="row1">
          <span class="score" style="background:{color}">{r['score']:.0f}</span>
          <span class="price">${r['price']:,.0f}<small> {kind}{bids}</small></span>
        </div>
        <div class="title">{html.escape(r['title'] or '')}</div>
        <div class="reasons">{html.escape(r['reasons'] or '')}</div>
        <div class="seller">seller {seller} · {html.escape(r['search_name'] or '')}</div>
      </div>
    </a>"""


STYLE = """
*{box-sizing:border-box}
body{font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
     background:#0b1220;color:#e2e8f0;margin:0;padding:0 16px 48px}
.wrap{max-width:760px;margin:0 auto}
header{padding:26px 0 14px}
h1{font-size:38px;font-weight:800;letter-spacing:-1px;margin:0;
   background:linear-gradient(90deg,#22c55e,#3b82f6,#a855f7);
   -webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:#94a3b8;font-size:13px;margin-top:4px}
.tabs{position:sticky;top:0;z-index:10;display:flex;gap:8px;overflow-x:auto;
      padding:12px 0;background:#0b1220;border-bottom:1px solid #1e293b;
      -webkit-overflow-scrolling:touch}
.tab{flex:0 0 auto;background:#1e293b;border:1px solid #334155;color:#cbd5e1;
     padding:9px 15px;border-radius:999px;font-size:14px;font-weight:600;
     cursor:pointer;white-space:nowrap}
.tab .badge{margin-left:5px;opacity:.7;font-weight:500}
.tab.active{background:var(--c);border-color:var(--c);color:#0b1220}
.tab.active .badge{opacity:.85}
.panel{display:none;padding-top:16px}
.panel.active{display:block}
.empty{color:#64748b;padding:24px 4px}
.grid{display:flex;flex-direction:column;gap:11px}
.card{display:flex;gap:13px;background:#131c2e;border:1px solid #233047;
      border-radius:13px;overflow:hidden;text-decoration:none;color:inherit;
      transition:border-color .12s,transform .12s}
.card:hover{border-color:#475569;transform:translateY(-1px)}
.thumb{width:110px;min-width:110px;background:#0b1220;display:flex;align-items:center;
       justify-content:center}
.thumb img{width:110px;height:110px;object-fit:cover}
.body{padding:11px 13px 11px 0;min-width:0;flex:1}
.row1{display:flex;align-items:center;gap:9px;margin-bottom:5px}
.score{font-weight:800;font-size:15px;border-radius:7px;padding:2px 9px;color:#0b1220}
.price{margin-left:auto;font-weight:700;font-size:15px}
.price small{color:#94a3b8;font-weight:400}
.title{font-weight:600;margin-bottom:4px}
.reasons{color:#7dd3fc;font-size:12.5px;margin-bottom:3px}
.seller{color:#94a3b8;font-size:12.5px}
"""

SCRIPT = """
document.querySelectorAll('.tab').forEach(function(b){
  b.onclick=function(){
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
    b.classList.add('active');
    document.getElementById('p-'+b.dataset.tab).classList.add('active');
  };
});
"""


def write_report(db_path: str, out_path: str = "gems.html",
                 min_score: float = 0.0, limit: int = 300) -> int:
    s = Storage(db_path)
    tabs, panels, total, default = [], [], 0, None
    for stream, label, color, cap, smin in STREAMS:
        rows = s.top_gems(max(min_score, smin), cap or limit, stream=stream)
        total += len(rows)
        if default is None and rows:
            default = stream
        cards = "".join(_card(r, color) for r in rows) or '<p class="empty">— none yet —</p>'
        tabs.append((stream, label, color, len(rows)))
        panels.append((stream, cards))
    s.close()
    default = default or STREAMS[0][0]

    tabs_html = "".join(
        f'<button class="tab{" active" if st == default else ""}" data-tab="{st}" '
        f'style="--c:{c}">{label} <span class="badge">{n}</span></button>'
        for st, label, c, n in tabs)
    panels_html = "".join(
        f'<section id="p-{st}" class="panel{" active" if st == default else ""}">'
        f'<div class="grid">{cards}</div></section>'
        for st, cards in panels)

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GemHunter</title><style>{STYLE}</style></head><body><div class="wrap">
<header><h1>GemHunter</h1>
<div class="sub">{total} gems · updated {_now_str()}</div></header>
<nav class="tabs">{tabs_html}</nav>
<main>{panels_html}</main>
</div><script>{SCRIPT}</script></body></html>"""
    Path(out_path).write_text(doc, encoding="utf-8")
    return total


def print_summary(db_path: str, min_score: float = 0.0, top: int = 8) -> None:
    s = Storage(db_path)
    st = s.stats()
    print(f"\nDataset: {st['total']} listings seen · {st['rejected']} filtered · "
          f"{st['gems']} gems")
    for stream, label, _, cap, smin in STREAMS:
        rows = s.top_gems(max(min_score, smin), cap or 1000, stream=stream)
        print(f"\n{label.replace('&amp;','&')}  —  {len(rows)} (score ≥ {max(min_score, smin):g})")
        for r in rows[:top]:
            print(f"    {r['score']:>4.0f} ${r['price']:>8,.0f}  {(r['title'] or '')[:54]}")
    s.close()
