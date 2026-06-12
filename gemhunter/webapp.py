"""Private mobile web app for GemHunter.

The Pi serves this over Tailscale. No framework, no build step, no public
surface area: just SQLite, a small JSON API, and a mobile-first app shell.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .storage import Storage

try:
    from zoneinfo import ZoneInfo
    MOUNTAIN = ZoneInfo("America/Denver")
except Exception:
    MOUNTAIN = None


COLLECTIONS = [
    {"id": "repair", "label": "For Parts", "short": "Parts", "icon": "&#128295;", "color": "#f59e0b", "min": 6, "limit": 100,
     "deck": "Serviceable projects worth your bench time."},
    {"id": "chrono", "label": "Chronos", "short": "Chronos", "icon": "&#9201;", "color": "#38bdf8", "min": 10, "limit": 100,
     "deck": "Mechanical chronographs with movement signal."},
    {"id": "taste", "label": "Taste", "short": "Taste", "icon": "&#128142;", "color": "#a78bfa", "min": 10, "limit": 100,
     "deck": "Non-chrono pieces that fit your collecting lane."},
    {"id": "iwc", "label": "IWC Golden Era", "short": "IWC", "icon": "&#9992;&#65039;", "color": "#2dd4bf", "min": 10, "limit": 100,
     "deck": "Pre-Richemont IWC, ~1980–2005: Mark XII, Doppel 3713, Big Pilot 5002, UTC 3251."},
    {"id": "rolex", "label": "Box & Papers Rolex", "short": "Rolex", "icon": "&#127919;", "color": "#34d399", "min": 10, "limit": 10,
     "deck": "Submariner, GMT, and Daytona full-set candidates."},
    {"id": "patek", "label": "Patek", "short": "Patek", "icon": "&#128081;", "color": "#facc15", "min": 10, "limit": 100,
     "deck": "Calatrava, annual calendar, complications, and grail Patek."},
]

RARE_COLLECTION = {"id": "rare", "label": "Rare Watch Radar", "short": "Rare", "icon": "&#9670;", "color": "#fb7185", "min": 10, "limit": 100,
                   "deck": "Elusive references: JLC Deep Sea Alarm, Rolex Kew Observatory, and Movado Tempograf."}
STREAMS = COLLECTIONS + [RARE_COLLECTION]

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{2,}")
STOP = {
    "watch", "watches", "mens", "men", "with", "and", "the", "for", "from",
    "dial", "case", "date", "automatic", "manual", "vintage", "pre", "owned",
    "papers", "paper", "box", "full", "set", "stainless", "steel", "gold",
}


def _now_str() -> str:
    now = datetime.now(MOUNTAIN) if MOUNTAIN else datetime.now().astimezone()
    hour12 = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    return f"{now.month}/{now.day}/{now.year} {hour12}:{now.minute:02d} {ampm} {now.strftime('%Z')}"


def _tokens(text: str) -> set[str]:
    return {t for t in TOKEN_RE.findall((text or "").lower()) if t not in STOP}


def _preference_profile(rows: list[dict]) -> tuple[Counter, Counter]:
    likes, dislikes = Counter(), Counter()
    for row in rows:
        bag = _tokens(f"{row.get('title', '')} {row.get('search_name', '')} {row.get('reasons', '')} {row.get('feedback_reason', '')}")
        if row.get("saved"):
            likes.update(bag)
        if row.get("hidden"):
            dislikes.update(bag)
    return likes, dislikes


def _apply_learning(items: list[dict], likes: Counter, dislikes: Counter) -> list[dict]:
    tuned = []
    for item in items:
        bag = _tokens(f"{item.get('title', '')} {item.get('search_name', '')} {item.get('reasons', '')}")
        like = sum(min(likes[t], 3) for t in bag)
        dislike = sum(min(dislikes[t], 3) for t in bag)
        boost = min(4.0, like * 0.18) - min(4.0, dislike * 0.22)
        item = dict(item)
        item["preference_boost"] = round(boost, 2)
        item["smart_score"] = round(float(item.get("score") or 0) + boost, 2)
        if not item.get("opportunity"):
            item["opportunity"] = round(min(100.0, float(item.get("score") or 0) * 5.2 + boost), 1)
        if not item.get("confidence"):
            pct = float(item.get("seller_pct") or 0)
            count = float(item.get("seller_score") or 0)
            item["confidence"] = round(min(100.0, 35 + min(28.0, count / 40.0) + (20 if pct >= 99 else 8 if pct >= 95 else 0)), 1)
        tuned.append(item)
    tuned.sort(key=lambda r: (r.get("saved", 0), r["smart_score"], r.get("score") or 0, r.get("last_seen") or 0), reverse=True)
    return tuned


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="GemHunter">
  <meta name="theme-color" content="#08111f">
  <title>GemHunter</title>
  <style>
    :root{
      --bg:#08111f; --panel:#101a2d; --panel2:#142039; --line:#263652;
      --text:#e6edf7; --muted:#8ea0ba; --soft:#c8d4e6; --danger:#fb7185;
      --shadow:0 18px 45px rgba(0,0,0,.28);
    }
    *{box-sizing:border-box}
    html{background:var(--bg)}
    body{margin:0;background:
      radial-gradient(circle at 20% -10%,rgba(56,189,248,.18),transparent 34%),
      radial-gradient(circle at 100% 10%,rgba(250,204,21,.12),transparent 28%),
      var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      padding:0 14px calc(82px + env(safe-area-inset-bottom));}
    .app{max-width:720px;margin:0 auto;min-height:100vh}
    .top{position:sticky;top:0;z-index:30;margin:0 -14px;padding:16px 14px 10px;
      background:linear-gradient(180deg,rgba(8,17,31,.96),rgba(8,17,31,.88) 70%,rgba(8,17,31,0));
      backdrop-filter:blur(18px)}
    .toprow{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
    .eyebrow{font-size:12px;font-weight:750;color:#9fb0c9;letter-spacing:.02em}
    h1{font-size:42px;line-height:.95;margin:4px 0 0;font-weight:900;letter-spacing:-1.7px;
      background:linear-gradient(90deg,#f8fafc,#7dd3fc 48%,#facc15);-webkit-background-clip:text;background-clip:text;color:transparent;cursor:pointer}
    .status{font-size:12px;color:var(--muted);margin-top:8px}
    .topActions{display:flex;gap:8px;padding-top:4px}
    .iconBtn{border:1px solid rgba(148,163,184,.22);background:rgba(15,23,42,.72);color:var(--text);
      width:40px;height:40px;border-radius:13px;font-size:18px;font-weight:800}
    .modeDock{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-top:16px}
    .mode{border:1px solid transparent;background:rgba(15,23,42,.62);color:var(--muted);
      border-radius:16px;padding:9px 4px 8px;text-align:center;font-size:11px;font-weight:800}
    .mode .ico{display:block;font-size:18px;line-height:1.1;margin-bottom:2px}
    .mode.active{background:#e5edf7;color:#07111f;box-shadow:0 10px 30px rgba(226,232,240,.12)}
    .view{display:none}.view.active{display:block}
    .heroCard{margin:14px 0 12px;background:linear-gradient(145deg,rgba(20,32,57,.94),rgba(12,21,38,.96));
      border:1px solid rgba(148,163,184,.18);box-shadow:var(--shadow);border-radius:24px;padding:16px}
    .collectionHead{display:flex;align-items:center;justify-content:space-between;gap:14px}
    .collectionTitle{display:flex;align-items:center;gap:11px;min-width:0}
    .orb{width:42px;height:42px;border-radius:15px;display:grid;place-items:center;color:#07111f;font-size:21px;font-weight:900;background:var(--accent)}
    h2{font-size:23px;line-height:1.05;margin:0;font-weight:900;letter-spacing:-.5px}
    .deck{margin:5px 0 0;color:var(--muted);font-size:13px}
    .countPill{border:1px solid rgba(148,163,184,.18);background:#0b1425;color:var(--soft);
      border-radius:999px;padding:7px 10px;font-size:12px;font-weight:850;white-space:nowrap}
    .rail{display:flex;gap:9px;overflow-x:auto;margin:14px -16px 0;padding:0 16px 2px;scrollbar-width:none}
    .rail::-webkit-scrollbar{display:none}
    .collection{flex:0 0 112px;border:1px solid rgba(148,163,184,.16);background:#0d1728;color:var(--soft);
      border-radius:18px;padding:11px;text-align:left}
    .collection.active{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent);background:linear-gradient(160deg,rgba(255,255,255,.08),rgba(255,255,255,.02))}
    .collection .topline{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
    .collection .ci{font-size:19px}.collection .num{font-size:12px;color:var(--muted);font-weight:800}
    .collection .name{font-size:13px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .filters{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:13px}
    .filter{background:#0b1425;border:1px solid rgba(148,163,184,.15);border-radius:15px;padding:9px}
    .filter label{display:block;color:var(--muted);font-size:11px;font-weight:800;margin-bottom:4px}
    .filter input,.filter select{width:100%;border:0;background:transparent;color:var(--text);font:700 14px system-ui;outline:none}
    .wide{grid-column:1/-1;display:flex;gap:8px}
    .chip{border:1px solid rgba(148,163,184,.18);background:#0b1425;color:var(--soft);border-radius:999px;padding:8px 11px;font-weight:850;font-size:12px}
    .chip.active{background:var(--accent);color:#07111f;border-color:var(--accent)}
    .feed{display:flex;flex-direction:column;gap:12px;touch-action:pan-y;margin-top:12px}
    .inspectSection{margin-top:14px}
    .inspectSection h3{font-size:18px;margin:0 0 9px;font-weight:950;color:#f8fafc}
    .card{position:relative;display:grid;grid-template-columns:118px 1fr;gap:13px;background:rgba(16,26,45,.96);
      border:1px solid rgba(148,163,184,.16);border-radius:22px;overflow:hidden;color:inherit;text-decoration:none;box-shadow:0 10px 32px rgba(0,0,0,.18)}
    .card.saved{border-color:#facc15;box-shadow:0 0 0 1px rgba(250,204,21,.28),0 10px 32px rgba(0,0,0,.18)}
    .media{background:#08111f;display:flex;flex-direction:column;min-height:100%}
    .thumb{height:112px;background:#08111f;display:grid;place-items:center}
    .thumb img{width:118px;height:112px;object-fit:cover}
    .body{padding:12px 12px 12px 0;min-width:0}
    .meta{display:flex;align-items:center;gap:7px;margin-bottom:6px}
    .score{border-radius:9px;background:var(--accent);color:#07111f;font-weight:950;padding:3px 8px;font-size:13px}
    .learn{border-radius:999px;background:rgba(34,197,94,.12);color:#86efac;border:1px solid rgba(134,239,172,.25);font-size:11px;font-weight:850;padding:3px 7px}
    .price{margin-left:auto;white-space:nowrap;font-size:15px;font-weight:950;text-align:right;line-height:1.05}
    .price b{display:block;font-size:16px}
    .price small{display:block;text-align:right;color:var(--muted);font-size:11px;font-weight:700}
    .costLine{display:block;margin-top:2px;color:#aebed4;font-size:10px;font-weight:800}
    .costWarn{color:#fbbf24}
    .loc{display:block;text-align:right;margin-top:2px;font-size:10px;font-weight:900;letter-spacing:0;color:#cbd5e1}
    .loc.pref{color:#bae6fd}
    .loc.warn{color:#fecaca}
    .loc.humid{color:#fde68a}
    .title{display:block;color:var(--text);font-size:14px;font-weight:850;line-height:1.22;text-decoration:none;margin-bottom:7px}
    .reasons{color:#8bdcff;font-size:12px;font-weight:650;margin-bottom:5px}
    .judgment{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin:8px 0}
    .meter{background:#0b1425;border:1px solid rgba(148,163,184,.14);border-radius:12px;padding:7px}
    .meter b{display:block;color:var(--text);font-size:16px;line-height:1;font-weight:950}
    .meter span{display:block;color:var(--muted);font-size:10px;font-weight:850;text-transform:uppercase;margin-top:3px}
    .risks{display:flex;flex-wrap:wrap;gap:5px;margin:6px 0}
    .risk{border:1px solid rgba(251,113,133,.24);background:rgba(251,113,133,.1);color:#fecdd3;border-radius:999px;padding:3px 7px;font-size:10px;font-weight:850}
    .factStack{display:flex;flex-wrap:wrap;gap:5px;padding:7px;background:#08111f;border-top:1px solid rgba(148,163,184,.1)}
    .fact{border:1px solid rgba(148,163,184,.14);background:rgba(226,237,247,.06);color:#c8d4e6;border-radius:999px;padding:3px 6px;font-size:10px;font-weight:850;line-height:1}
    .fact.warn{border-color:rgba(251,191,36,.3);background:rgba(251,191,36,.1);color:#fde68a}
    .fact.good{border-color:rgba(52,211,153,.25);background:rgba(52,211,153,.1);color:#bbf7d0}
    .groupNote{border:1px solid rgba(148,163,184,.16);background:rgba(226,237,247,.06);color:#dbe7f5;border-radius:12px;padding:7px 8px;font-size:11px;font-weight:800;margin:6px 0}
    .actionNote{color:#c8d4e6;font-size:12px;line-height:1.25;margin:6px 0 3px}
    .seller{color:var(--muted);font-size:12px}
    .actions{display:flex;gap:8px;margin-top:10px}
    .actions button{border:1px solid rgba(148,163,184,.18);background:#0b1425;color:var(--soft);border-radius:12px;padding:8px 10px;font-weight:900}
    .heart.saved{background:#facc15;color:#07111f;border-color:#facc15}
    .less{color:#f8fafc;display:inline-flex;align-items:center;justify-content:center;min-width:38px}
    .less svg{width:17px;height:17px;display:block;stroke:currentColor}
    .empty{border:1px dashed rgba(148,163,184,.2);border-radius:22px;padding:26px 16px;color:var(--muted);text-align:center;background:rgba(15,23,42,.35)}
    .placeholder{margin-top:14px;border:1px solid rgba(148,163,184,.16);border-radius:24px;padding:22px;background:rgba(16,26,45,.86);box-shadow:var(--shadow)}
    .placeholder h2{font-size:26px}.placeholder p{color:var(--muted)}
    .aboutBackdrop{position:fixed;inset:0;display:none;align-items:flex-end;justify-content:center;background:rgba(2,6,23,.66);backdrop-filter:blur(10px);z-index:80;padding:18px 12px}
    .aboutBackdrop.open{display:flex}
    .aboutSheet{width:min(720px,100%);max-height:min(86vh,760px);overflow:auto;background:linear-gradient(160deg,#111c30,#091222);border:1px solid rgba(148,163,184,.22);border-radius:26px 26px 22px 22px;box-shadow:0 26px 80px rgba(0,0,0,.52);padding:18px}
    .aboutTop{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:14px}
    .aboutKicker{color:#93c5fd;font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.08em}
    .aboutTitle{font-size:31px;line-height:1;margin:4px 0 8px;font-weight:950;letter-spacing:-.8px}
    .aboutLead{margin:0;color:#dbeafe;font-size:15px;line-height:1.36;font-weight:700}
    .aboutClose{border:1px solid rgba(148,163,184,.22);background:#0b1425;color:#e6edf7;border-radius:14px;width:38px;height:38px;font-size:21px;line-height:1;font-weight:800}
    .aboutGrid{display:grid;gap:10px}
    .aboutBlock{border:1px solid rgba(148,163,184,.16);background:rgba(15,23,42,.62);border-radius:18px;padding:13px}
    .aboutBlock h3{font-size:15px;margin:0 0 7px;font-weight:950;color:#f8fafc}
    .aboutBlock p{margin:0;color:#aebed4;font-size:13px;line-height:1.4}
    .aboutBlock b{color:#e6edf7}
    .aboutList{display:grid;gap:7px;margin:0;padding:0;list-style:none}
    .aboutList li{color:#aebed4;font-size:13px;line-height:1.35}
    .aboutList strong{color:#e6edf7}
    .aboutFormula{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
    .aboutPill{border:1px solid rgba(125,211,252,.22);background:rgba(14,165,233,.11);color:#bae6fd;border-radius:999px;padding:7px 10px;font-size:12px;font-weight:900}
    .aboutArrow{color:#64748b;font-weight:950}
    .toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:#e6edf7;color:#07111f;padding:9px 13px;border-radius:999px;font-weight:900;opacity:0;transition:opacity .18s;z-index:50}
    .toast.show{opacity:1}
    @media (max-width:390px){body{padding-left:10px;padding-right:10px}.top{margin-left:-10px;margin-right:-10px;padding-left:10px;padding-right:10px}.card{grid-template-columns:104px 1fr}.thumb,.thumb img{width:104px;height:112px}h1{font-size:38px}.mode{font-size:10px}.aboutTitle{font-size:28px}.aboutSheet{padding:16px}}
  </style>
</head>
<body>
<div class="app" id="appSurface">
  <header class="top">
    <div class="toprow">
      <div>
        <div class="eyebrow">Private watch scout</div>
        <h1 onclick="openAbout()" onkeydown="titleKey(event)" role="button" tabindex="0" aria-label="About GemHunter">GemHunter</h1>
        <div class="status" id="updated">updated __UPDATED__</div>
      </div>
      <div class="topActions">
        <button class="iconBtn" onclick="openAbout()" aria-label="About GemHunter">i</button>
        <button class="iconBtn" onclick="hardRefresh()" aria-label="Refresh">↻</button>
      </div>
    </div>
    <nav class="modeDock" id="modeDock"></nav>
  </header>

  <section id="discoverView" class="view active">
    <div class="heroCard" id="heroCard">
      <div class="collectionHead">
        <div class="collectionTitle">
          <div class="orb" id="activeOrb"></div>
          <div><h2 id="activeTitle"></h2><p class="deck" id="activeDeck"></p></div>
        </div>
        <div class="countPill" id="shownCount">0 shown</div>
      </div>
      <div class="rail" id="collectionRail"></div>
      <div class="filters">
        <div class="filter"><label>Max price</label><input id="maxPrice" inputmode="numeric" placeholder="Any"></div>
        <div class="filter"><label>Min seller</label><input id="minSeller" inputmode="numeric" placeholder="Any %"></div>
        <div class="filter"><label>Min score</label><input id="minScore" inputmode="numeric" placeholder="Default"></div>
        <div class="filter"><label>Sort</label><select id="sortBy"><option value="smart">For you</option><option value="priceAsc">Price ↑</option><option value="priceDesc">Price ↓</option><option value="seller">Seller</option></select></div>
        <div class="wide"><button class="chip" id="auctionOnly">Auctions</button><button class="chip" id="savedOnly">Saved</button><button class="chip" onclick="clearFilters()">Clear</button></div>
      </div>
    </div>
    <main class="feed" id="feed"></main>
  </section>

  <section id="inspectView" class="view">
    <div class="placeholder"><h2>Inspect now</h2><p>The shortest possible list: rare hits, best repair projects, safe collector buys, chronos, and possible relists.</p></div>
    <main id="inspectFeed"></main>
  </section>

  <section id="savedView" class="view">
    <div class="placeholder"><h2>Saved watches</h2><p>Your hearted watches live here. Hearts also nudge similar future listings higher in the Discover feed.</p></div>
    <main class="feed" id="savedFeed"></main>
  </section>
  <section id="compsView" class="view"><div class="placeholder"><h2>Comps lab</h2><p>Reserved for Marketplace Insights: sold-price curves, reference medians, and underpriced alerts.</p></div></section>
  <section id="rareView" class="view">
    <div class="placeholder"><h2>Rare watch radar</h2><p>Elusive references worth seeing on sight: vintage JLC Deep Sea Alarm, Rolex Kew Observatory trial watches, and Movado Tempograf.</p></div>
    <main class="feed" id="rareFeed"></main>
  </section>
  <section id="catalogView" class="view"><div class="placeholder"><h2>Catalog matches</h2><p>Reserved for Sotheby's and auction-catalog watches: if one appears on eBay, it should light up here.</p></div></section>
</div>
<div class="aboutBackdrop" id="aboutBackdrop" onclick="backdropClose(event)" role="dialog" aria-modal="true" aria-labelledby="aboutTitle">
  <section class="aboutSheet">
    <div class="aboutTop">
      <div>
        <div class="aboutKicker">Read me</div>
        <div class="aboutTitle" id="aboutTitle">What GemHunter Is</div>
        <p class="aboutLead">GemHunter is a private watch scout built to codify one collector-watchmaker's taste: the watches worth owning, servicing, studying, and maybe selling later.</p>
      </div>
      <button class="aboutClose" onclick="closeAbout()" aria-label="Close about">×</button>
    </div>
    <div class="aboutGrid">
      <div class="aboutBlock">
        <h3>The Principle</h3>
        <p><b>Taste is the gate. Undervaluation is the multiplier.</b> A watch has to be something I know, love, or find weird in a good way before a low price matters. The app is not trying to find every profitable object. It is trying to find the watches my eye would stop on.</p>
        <div class="aboutFormula"><span class="aboutPill">taste</span><span class="aboutArrow">then</span><span class="aboutPill">trust</span><span class="aboutArrow">then</span><span class="aboutPill">condition</span><span class="aboutArrow">then</span><span class="aboutPill">opportunity</span></div>
      </div>
      <div class="aboutBlock">
        <h3>What Gets Surfaced</h3>
        <ul class="aboutList">
          <li><strong>For Parts:</strong> broken or as-is watches that may be serviceable, especially Swiss chronographs with movements I can repair and source parts for.</li>
          <li><strong>Chronos:</strong> mechanical chronographs with movement signal, case size, originality, and seller context.</li>
          <li><strong>Rolex / Patek / IWC:</strong> focused lanes for pieces I actively care about, with extra attention to full sets, originality, seller quality, and era-correct details.</li>
          <li><strong>Rare Radar:</strong> elusive references worth seeing immediately, even if they are too scarce for ordinary scoring.</li>
        </ul>
      </div>
      <div class="aboutBlock">
        <h3>How The Score Thinks</h3>
        <p>Listings pass through hard cuts first: quartz, smartwatches, fashion brands, parts-only listings, redials, replicas, weak sellers, unwanted countries, disliked calibers, and obvious mismatch signals. The survivors get ranked by brand, model, caliber, size, box/papers, authenticity guarantee, seller trust, import risk, moisture risk, and repairability.</p>
      </div>
      <div class="aboutBlock">
        <h3>Opportunity & Confidence</h3>
        <ul class="aboutList">
          <li><strong>Opportunity</strong> means “how worth inspecting this is for my taste,” not “guaranteed under market.” It rises with strong brand/model/caliber signals, serviceability, rarity, full-set/originality, and repair upside. It falls with risk tags.</li>
          <li><strong>Confidence</strong> means “how much the listing context supports trusting the signal.” It rises with seller feedback, seller volume, box/papers, authenticity guarantee, and cleaner listing data. It falls for missing movement info, weak sellers, import/moisture risk, or other warning tags.</li>
          <li><strong>Important:</strong> until sold-comps data is wired in, these numbers do not know whether a $179,950 Patek Cubitus is fairly priced. A 100/100 means high signal and low obvious listing risk. It does not mean buy this, and it does not mean cheap. Final judgment still requires comps, reference research, and your eye.</li>
        </ul>
      </div>
      <div class="aboutBlock">
        <h3>How It Learns</h3>
        <p>Hearting a watch tells the app, “more like this.” The thumbs-down tells it, “less like this.” Those signals nudge future listings by title, search, reasons, and feedback terms so the feed slowly bends toward my revealed taste instead of staying a static rules list.</p>
      </div>
      <div class="aboutBlock">
        <h3>Why This Exists</h3>
        <p>eBay is too noisy to browse manually all day. GemHunter watches the firehose, rejects the junk, groups the results into human-readable tabs, and leaves the final judgment to the collector. The goal is not automation replacing taste. The goal is taste made visible.</p>
      </div>
    </div>
  </section>
</div>
<div class="toast" id="toast"></div>

<script>
const COLLECTIONS = __COLLECTIONS__;
const RARE_COLLECTION = __RARE_COLLECTION__;
const MODES = [
  {id:'discover', label:'Discover', icon:'⌁'},
  {id:'saved', label:'Saved', icon:'♥'},
  {id:'comps', label:'Comps', icon:'⌁'},
  {id:'rare', label:'Rare', icon:'◆'},
  {id:'catalog', label:'Catalogs', icon:'▣'},
];
let mode = 'discover';
let active = COLLECTIONS[0].id;
let items = [];
let counts = {};
const $ = id => document.getElementById(id);
const feed = $('feed'), inspectFeed = $('inspectFeed'), savedFeed = $('savedFeed'), rareFeed = $('rareFeed'), toast = $('toast');

function money(n){ return '$' + Number(n || 0).toLocaleString(undefined,{maximumFractionDigits:0}); }
function esc(s){ return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function shortMoney(n){
  n = Number(n || 0);
  if (n >= 1000000) return '$' + (n / 1000000).toFixed(n >= 10000000 ? 0 : 1) + 'm';
  if (n >= 10000) return '$' + Math.round(n / 1000) + 'k';
  return money(n);
}
const HUMID_CC = new Set(['JP','SG','MY','ID','PH','TW','VN','IN','TH','HK','BR']);
const PREF_CC = new Set(['JP','DE','GB','UK','FR']);
function flagEmoji(cc){ return cc.length===2 ? cc.replace(/./g, c => String.fromCodePoint(127397 + c.charCodeAt(0))) : ''; }
function locBadge(cc){
  cc = String(cc || '').toUpperCase();
  if (!cc) return '';
  if (cc === 'US') return '';
  const humid = HUMID_CC.has(cc);
  const cls = PREF_CC.has(cc) ? 'loc pref' : 'loc warn';
  const note = humid ? ' · import fees · moisture' : ' · import fees';
  return `<small class="${cls}${humid ? ' humid' : ''}">${flagEmoji(cc)} ${cc}${note}</small>`;
}
function costParts(item){
  const price = Number(item.price || 0);
  const ship = Number(item.shipping_cost || 0);
  const imp = Number(item.import_charges || 0);
  const cc = String(item.country || '').toUpperCase();
  const foreign = cc && cc !== 'US';
  return { price, ship, imp, cc, foreign, importTbd: foreign && !imp, total: price + ship + imp };
}
function landedNumber(item){ return costParts(item).total; }
function priceStack(item, kind, bids){
  const c = costParts(item);
  const hasKnownAddons = c.ship > 0 || c.imp > 0;
  const main = hasKnownAddons ? c.total : c.price;
  const label = hasKnownAddons ? 'landed est.' : `${kind}${bids}`;
  const lines = [];
  if (hasKnownAddons) lines.push(`item ${shortMoney(c.price)}`);
  lines.push(c.ship > 0 ? `ship +${shortMoney(c.ship)}` : 'ship included/unknown');
  if (c.imp > 0) lines.push(`import +${shortMoney(c.imp)}`);
  else if (c.importTbd) lines.push('import TBD');
  const extra = lines.length ? `<span class="costLine${c.importTbd ? ' costWarn' : ''}">${lines.join(' · ')}</span>` : '';
  return `<span class="price"><b>${money(main)}</b><small>${label}</small>${extra}${locBadge(item.country)}</span>`;
}
function factChips(item){
  const c = costParts(item);
  const risks = String(item.risk_tags || '');
  const reasons = String(item.reasons || '');
  const chips = [];
  if (item.seller_pct) chips.push(`<span class="fact good">seller ${Math.round(item.seller_pct)}%</span>`);
  if (item.buying_option === 'AUCTION' && item.bid_count) chips.push(`<span class="fact">${item.bid_count} bids</span>`);
  if (reasons.includes('auth-guarantee')) chips.push('<span class="fact good">auth</span>');
  if (c.foreign) chips.push(`<span class="fact warn">${c.cc} import</span>`);
  if (risks.includes('humidity/moisture')) chips.push('<span class="fact warn">moisture</span>');
  return chips.slice(0, 3).join('');
}
function activeCollection(){ return COLLECTIONS.find(c => c.id === active) || COLLECTIONS[0]; }
function showToast(msg){ toast.textContent = msg; toast.classList.add('show'); setTimeout(()=>toast.classList.remove('show'), 1100); }
function hardRefresh(){ window.location.href = window.location.pathname + '?v=' + Date.now(); }
function openAbout(){ $('aboutBackdrop').classList.add('open'); }
function closeAbout(){ $('aboutBackdrop').classList.remove('open'); }
function backdropClose(ev){ if (ev.target.id === 'aboutBackdrop') closeAbout(); }
function titleKey(ev){ if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openAbout(); } }

function renderModeDock(){
  if (!MODES.some(m => m.id === 'inspect')) MODES.unshift({id:'inspect', label:'Inspect', icon:'!'});
  $('modeDock').innerHTML = MODES.map(m => `<button class="mode ${m.id===mode?'active':''}" onclick="setMode('${m.id}')"><span class="ico">${m.icon}</span>${m.label}</button>`).join('');
}
function installSortOptions(){
  const s = $('sortBy');
  if (!s || s.querySelector('option[value="opportunity"]')) return;
  s.querySelector('option[value="smart"]')?.insertAdjacentHTML('afterend', '<option value="opportunity">Opportunity</option><option value="confidence">Confidence</option>');
}
function setMode(next){
  mode = next; renderModeDock();
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  $(`${next}View`)?.classList.add('active');
  if (next === 'inspect') loadInspect();
  if (next === 'discover') loadCollection();
  if (next === 'saved') loadSaved();
  if (next === 'rare') loadRare();
}
function renderRail(){
  $('collectionRail').innerHTML = COLLECTIONS.map(c => `<button class="collection ${c.id===active?'active':''}" style="--accent:${c.color}" onclick="selectCollection('${c.id}')">
    <div class="topline"><span class="ci">${c.icon}</span><span class="num">${counts[c.id] ?? ''}</span></div><div class="name">${c.short}</div>
  </button>`).join('');
}
function selectCollection(id){
  active = id; renderRail(); loadCollection();
  document.querySelector('.collection.active')?.scrollIntoView({inline:'center', block:'nearest', behavior:'smooth'});
}
function renderHero(){
  const c = activeCollection();
  $('heroCard').style.setProperty('--accent', c.color);
  $('activeOrb').innerHTML = c.icon;
  $('activeOrb').style.background = c.color;
  $('activeTitle').textContent = c.label;
  $('activeDeck').textContent = c.deck;
}
async function loadCollection(){
  const c = activeCollection();
  renderHero();
  feed.innerHTML = '<p class="empty">Loading ' + c.label + '…</p>';
  const res = await fetch(`/api/listings?stream=${encodeURIComponent(active)}`);
  const data = await res.json();
  items = data.items || []; counts = data.counts || {};
  $('updated').textContent = 'updated ' + data.updated;
  renderRail(); applyFilters();
}
async function loadSaved(){
  savedFeed.innerHTML = '<p class="empty">Loading saved watches…</p>';
  const res = await fetch('/api/listings?view=saved');
  const data = await res.json();
  const rows = data.items || [];
  savedFeed.innerHTML = rows.length ? rows.map(r => card(r, '#facc15')).join('') : '<p class="empty">No saved watches yet. Heart a listing to start training your taste model.</p>';
}
async function loadInspect(){
  inspectFeed.innerHTML = '<p class="empty">Loading inspect list...</p>';
  const res = await fetch('/api/inspect');
  const data = await res.json();
  const sections = data.sections || [];
  inspectFeed.innerHTML = sections.length ? sections.map(section => `
    <section class="inspectSection">
      <h3>${esc(section.label)}</h3>
      <div class="feed">${section.items.map(r => card(r, '#e5edf7')).join('')}</div>
    </section>`).join('') : '<p class="empty">No inspect-now candidates yet.</p>';
}
async function loadRare(){
  rareFeed.innerHTML = '<p class="empty">Loading rare radar...</p>';
  const res = await fetch('/api/listings?stream=rare');
  const data = await res.json();
  const rows = data.items || [];
  rareFeed.innerHTML = rows.length ? rows.map(r => card(r, RARE_COLLECTION.color)).join('') : '<p class="empty">No rare-watch matches yet. When a Deep Sea Alarm, Rolex Kew/Observatory, or Movado Tempograf listing appears, it will land here.</p>';
}
function filters(){
  return {
    maxPrice: Number($('maxPrice').value || 0),
    minSeller: Number($('minSeller').value || 0),
    minScore: Number($('minScore').value || 0),
    sortBy: $('sortBy').value,
    auctionOnly: $('auctionOnly').classList.contains('active'),
    savedOnly: $('savedOnly').classList.contains('active'),
  };
}
function applyFilters(){
  const f = filters();
  let rows = [...items];
  if (f.maxPrice) rows = rows.filter(r => landedNumber(r) <= f.maxPrice);
  if (f.minSeller) rows = rows.filter(r => Number(r.seller_pct || 0) >= f.minSeller);
  if (f.minScore) rows = rows.filter(r => Number(r.smart_score || r.score || 0) >= f.minScore);
  if (f.auctionOnly) rows = rows.filter(r => r.buying_option === 'AUCTION');
  if (f.savedOnly) rows = rows.filter(r => r.saved);
  rows.sort((a,b) => {
    if (f.sortBy === 'priceAsc') return landedNumber(a) - landedNumber(b);
    if (f.sortBy === 'priceDesc') return landedNumber(b) - landedNumber(a);
    if (f.sortBy === 'seller') return Number(b.seller_pct||0) - Number(a.seller_pct||0);
    if (f.sortBy === 'opportunity') return Number(b.opportunity||0) - Number(a.opportunity||0);
    if (f.sortBy === 'confidence') return Number(b.confidence||0) - Number(a.confidence||0);
    return Number(b.smart_score||b.score||0) - Number(a.smart_score||a.score||0);
  });
  $('shownCount').textContent = rows.length + ' shown';
  feed.innerHTML = rows.length ? rows.map(r => card(r, activeCollection().color)).join('') : '<p class="empty">No watches match those filters.</p>';
}
function card(item, color){
  const kind = item.buying_option === 'AUCTION' ? 'Auction' : 'BIN';
  const bids = item.buying_option === 'AUCTION' && item.bid_count ? ` · ${item.bid_count} bids` : '';
  const seller = item.seller_pct ? `${Math.round(item.seller_pct)}% (${Number(item.seller_score||0).toLocaleString()})` : '—';
  const boost = Number(item.preference_boost || 0);
  const learn = boost > .25 ? `<span class="learn">+${boost.toFixed(1)} taste</span>` : '';
  const img = item.image_url ? `<img src="${esc(item.image_url)}" loading="lazy">` : '';
  const opp = Math.round(Number(item.opportunity || 0));
  const conf = Math.round(Number(item.confidence || 0));
  const risks = String(item.risk_tags || '').split(',').map(s => s.trim()).filter(Boolean);
  if (Number(item.relist_count || 1) > 1) risks.unshift(`similar x${Number(item.relist_count)}`);
  const riskHtml = risks.length ? `<div class="risks">${risks.map(r => `<span class="risk">${esc(r)}</span>`).join('')}</div>` : '';
  const groupNote = item.relist_group_summary ? `<div class="groupNote">${esc(item.relist_group_summary)}</div>` : '';
  const action = item.action_note ? `<div class="actionNote">${esc(item.action_note)}</div>` : '';
  const facts = factChips(item);
  return `<article class="card ${item.saved ? 'saved' : ''}" style="--accent:${color}" data-id="${esc(item.item_id)}">
    <div class="media">
      <a class="thumb" href="${esc(item.url)}" target="_blank" rel="noopener">${img}</a>
      ${facts ? `<div class="factStack">${facts}</div>` : ''}
    </div>
    <div class="body">
      <div class="meta"><span class="score">${Math.round(item.smart_score || item.score || 0)}</span>${learn}${priceStack(item, kind, bids)}</div>
      <a class="title" href="${esc(item.url)}" target="_blank" rel="noopener">${esc(item.title)}</a>
      <div class="reasons">${esc(item.reasons)}</div>
      <div class="judgment"><div class="meter" title="How worth inspecting this is for your taste; not a price-comp verdict yet."><b>${opp}</b><span>Opportunity</span></div><div class="meter" title="How much the listing context supports trusting the signal."><b>${conf}</b><span>Confidence</span></div></div>
      ${riskHtml}
      ${groupNote}
      ${action}
      <div class="seller">seller ${seller} · ${esc(item.search_name)}</div>
      <div class="actions">
        <button class="heart ${item.saved ? 'saved' : ''}" onclick="toggleSaved(event,'${esc(item.item_id)}',${item.saved ? 0 : 1})">${item.saved ? '♥ Hearted' : '♡ Heart'}</button>
        <button class="less" aria-label="Less like this" onclick="hideItem(event,'${esc(item.item_id)}')"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 14V3"/><path d="M7 10.5 9.1 3H17v11h-5.2l-1.5 5.2c-.2.7-.8 1.2-1.6 1.2h-.4c-.8 0-1.4-.8-1.2-1.6L8.4 14H5.2c-1.2 0-2.1-1.1-1.8-2.3l1.3-5.2C5 5.6 5.8 5 6.7 5h2"/></svg></button>
      </div>
    </div>
  </article>`;
}
async function action(path, body){
  const res = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!res.ok) throw new Error(await res.text());
  return res.json();
}
async function toggleSaved(ev,id,saved){
  ev.preventDefault(); ev.stopPropagation();
  await action('/api/save',{item_id:id,saved:!!saved});
  showToast(saved ? 'Hearted' : 'Unhearted');
  const card = ev.target.closest('.card');
  const button = ev.target.closest('button');
  items = items.map(item => item.item_id === id ? {...item, saved: saved ? 1 : 0} : item);
  if (mode === 'saved' && !saved) {
    card?.remove();
    if (!savedFeed.querySelector('.card')) savedFeed.innerHTML = '<p class="empty">No saved watches yet. Heart a listing to start training your taste model.</p>';
    return;
  }
  card?.classList.toggle('saved', !!saved);
  if (button) {
    button.classList.toggle('saved', !!saved);
    button.textContent = saved ? '♥ Hearted' : '♡ Heart';
    button.setAttribute('onclick', `toggleSaved(event,'${id}',${saved ? 0 : 1})`);
  }
}
async function hideItem(ev,id){
  ev.preventDefault(); ev.stopPropagation();
  const reason = prompt('Less like this because?', 'taste');
  if (reason === null) return;
  await action('/api/hide',{item_id:id,hidden:true,reason:reason});
  showToast('Taught: less like this');
  const card = ev.target.closest('.card');
  items = items.filter(item => item.item_id !== id);
  card?.remove();
  if (mode === 'discover') $('shownCount').textContent = document.querySelectorAll('#feed .card').length + ' shown';
  if (mode === 'saved' && !savedFeed.querySelector('.card')) savedFeed.innerHTML = '<p class="empty">No saved watches yet. Heart a listing to start training your taste model.</p>';
}
function clearFilters(){
  ['maxPrice','minSeller','minScore'].forEach(id => $(id).value = '');
  $('sortBy').value = 'smart';
  $('auctionOnly').classList.remove('active');
  $('savedOnly').classList.remove('active');
  applyFilters();
}
['maxPrice','minSeller','minScore','sortBy'].forEach(id => $(id).addEventListener('input', applyFilters));
$('auctionOnly').onclick = () => { $('auctionOnly').classList.toggle('active'); applyFilters(); };
$('savedOnly').onclick = () => { $('savedOnly').classList.toggle('active'); applyFilters(); };

let swipe = null;
function swipeStart(x,y){ swipe = {x,y}; }
function swipeEnd(x,y){
  if(!swipe || mode !== 'discover') return;
  const dx = x - swipe.x, dy = y - swipe.y; swipe = null;
  if(Math.abs(dx) < 70 || Math.abs(dx) < Math.abs(dy) * 1.25) return;
  const i = COLLECTIONS.findIndex(c => c.id === active);
  const next = Math.max(0, Math.min(COLLECTIONS.length - 1, i + (dx > 0 ? 1 : -1)));
  if(next !== i) selectCollection(COLLECTIONS[next].id);
}
const surface = $('appSurface');
surface.addEventListener('pointerdown', ev => { if(ev.button !== 0 && ev.pointerType === 'mouse') return; swipeStart(ev.clientX, ev.clientY); }, {passive:true});
surface.addEventListener('pointerup', ev => swipeEnd(ev.clientX, ev.clientY), {passive:true});
surface.addEventListener('pointercancel', () => { swipe = null; }, {passive:true});
surface.addEventListener('touchstart', ev => { if(ev.changedTouches.length){ const t=ev.changedTouches[0]; swipeStart(t.clientX,t.clientY); }}, {passive:true});
surface.addEventListener('touchend', ev => { if(ev.changedTouches.length){ const t=ev.changedTouches[0]; swipeEnd(t.clientX,t.clientY); }}, {passive:true});
document.addEventListener('keydown', ev => { if(ev.key==='ArrowRight') swipeEnd((swipe?.x || 0)+100, swipe?.y || 0); if(ev.key==='ArrowLeft') swipeEnd((swipe?.x || 0)-100, swipe?.y || 0); });
document.addEventListener('keydown', ev => { if(ev.key==='Escape') closeAbout(); });

renderModeDock(); installSortOptions(); renderRail(); loadCollection();
</script>
</body>
</html>
"""


def _html() -> bytes:
    return (
        HTML
        .replace("__COLLECTIONS__", json.dumps(COLLECTIONS))
        .replace("__RARE_COLLECTION__", json.dumps(RARE_COLLECTION))
        .replace("__UPDATED__", _now_str())
    ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    db_path = "gemhunter.db"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: object) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/gems.html"):
            self._send(HTTPStatus.OK, _html(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/inspect":
            storage = Storage(self.db_path)
            likes, dislikes = _preference_profile(storage.feedback_rows())
            sections = storage.inspect_now()
            for section in sections:
                section["items"] = _apply_learning(section["items"], likes, dislikes)
            storage.close()
            self._json(HTTPStatus.OK, {
                "updated": _now_str(),
                "sections": sections,
            })
            return
        if parsed.path == "/api/listings":
            params = parse_qs(parsed.query)
            storage = Storage(self.db_path)
            likes, dislikes = _preference_profile(storage.feedback_rows())
            counts = {
                c["id"]: len(storage.top_gems(c["min"], c["limit"], stream=c["id"]))
                for c in COLLECTIONS
            }
            saved_count = len(storage.saved_gems())
            if params.get("view", [""])[0] == "saved":
                rows = storage.saved_gems()
            else:
                stream = params.get("stream", ["repair"])[0]
                cfg = next((c for c in STREAMS if c["id"] == stream), COLLECTIONS[0])
                rows = storage.top_gems(cfg["min"], cfg["limit"], stream=cfg["id"])
            storage.close()
            self._json(HTTPStatus.OK, {
                "updated": _now_str(),
                "counts": counts,
                "saved_count": saved_count,
                "items": _apply_learning(rows, likes, dislikes),
            })
            return
        if parsed.path == "/manifest.json":
            self._json(HTTPStatus.OK, {
                "name": "GemHunter",
                "short_name": "GemHunter",
                "display": "standalone",
                "start_url": "/",
                "theme_color": "#08111f",
                "background_color": "#08111f",
            })
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        item_id = str(body.get("item_id", ""))
        if not item_id:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "missing item_id"})
            return
        storage = Storage(self.db_path)
        if self.path == "/api/save":
            ok = storage.set_saved(item_id, bool(body.get("saved")))
        elif self.path == "/api/hide":
            ok = storage.set_hidden(item_id, bool(body.get("hidden", True)), str(body.get("reason", ""))[:120])
        else:
            storage.close()
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        storage.close()
        self._json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="GemHunter private mobile app")
    parser.add_argument("--db", default="gemhunter.db")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[web] serving GemHunter on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
