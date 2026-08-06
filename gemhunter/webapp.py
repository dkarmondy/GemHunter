"""Private mobile web app for GemHunter.

The Pi serves this over Tailscale. No framework, no build step, no public
surface area: just SQLite, a small JSON API, and a mobile-first app shell.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

from .config import load_config
from .ebay import EbayClient
from .rarities import SELLER as RARITIES_SELLER
from .rarities import STORE_URL as RARITIES_STORE_URL
from .rarities import apply_details, start_detail_pass
from .rarities import fetch as fetch_seller_auctions
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
      width:40px;height:40px;border-radius:13px;font-size:18px;font-weight:800;
      display:grid;place-items:center;text-decoration:none}
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
    .card.ended{opacity:.74;border-color:rgba(148,163,184,.26);filter:saturate(.72)}
    .media{background:#08111f;display:flex;flex-direction:column;min-height:100%}
    .thumb{height:112px;background:#08111f;display:grid;place-items:center}
    .thumb img{width:118px;height:112px;object-fit:cover}
    .body{padding:12px 12px 12px 0;min-width:0}
    .meta{display:flex;align-items:center;gap:7px;margin-bottom:6px}
    .score{border-radius:9px;background:var(--accent);color:#07111f;font-weight:950;padding:3px 8px;font-size:13px}
    .learn{border-radius:999px;background:rgba(34,197,94,.12);color:#86efac;border:1px solid rgba(134,239,172,.25);font-size:11px;font-weight:850;padding:3px 7px}
    .price{margin-left:auto;min-width:76px;max-width:118px;font-size:14px;font-weight:950;text-align:right;line-height:1.05;overflow-wrap:anywhere}
    .price b{display:block;font-size:15px}
    .price small{display:block;text-align:right;color:var(--muted);font-size:10px;font-weight:800}
    .costLine{display:block;margin-top:3px;color:#aebed4;font-size:9.5px;font-weight:800;line-height:1.18}
    .costWarn{color:#fbbf24}
    .loc{display:block;text-align:right;margin-top:3px;font-size:9.5px;font-weight:900;letter-spacing:0;color:#cbd5e1;line-height:1.15}
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
    .statusBadge{display:inline-flex;align-items:center;gap:5px;border:1px solid rgba(251,191,36,.3);background:rgba(251,191,36,.1);color:#fde68a;border-radius:999px;padding:4px 8px;font-size:11px;font-weight:900;margin:0 0 7px}
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
    .removeSaved{background:#e6edf7!important;color:#07111f!important;border-color:#e6edf7!important}
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
    @media (max-width:390px){body{padding-left:10px;padding-right:10px}.top{margin-left:-10px;margin-right:-10px;padding-left:10px;padding-right:10px}.card{grid-template-columns:104px 1fr}.thumb,.thumb img{width:104px;height:112px}.body{padding-right:9px}.price{max-width:102px;font-size:13px}.price b{font-size:14px}.costLine,.loc{font-size:9px}h1{font-size:38px}.mode{font-size:10px}.aboutTitle{font-size:28px}.aboutSheet{padding:16px}}
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
        <a class="iconBtn" href="/" aria-label="Back to dashboard">&#8592;</a>
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
  const shipKnown = Boolean(Number(item.shipping_known || 0));
  const imp = Number(item.import_charges || 0);
  const importKnown = Boolean(Number(item.import_charges_known || 0));
  const cc = String(item.country || '').toUpperCase();
  const foreign = cc && cc !== 'US';
  return { price, ship, shipKnown, imp, importKnown, cc, foreign, importTbd: foreign && !importKnown, total: price + ship + imp };
}
function landedNumber(item){ return costParts(item).total; }
function priceStack(item, kind, bids){
  const c = costParts(item);
  const hasKnownAddons = c.shipKnown || c.imp > 0;
  const main = hasKnownAddons ? c.total : c.price;
  const label = hasKnownAddons ? 'landed' : `${kind}${bids}`;
  const lines = [];
  if (hasKnownAddons) lines.push(`item ${shortMoney(c.price)}`);
  lines.push(c.shipKnown ? (c.ship > 0 ? `ship ${shortMoney(c.ship)}` : 'ship free') : 'ship TBD');
  if (c.imp > 0) lines.push(`import ${shortMoney(c.imp)}`);
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
function isActive(item){ return Number(item.active ?? 1) !== 0; }
function inactiveLabel(item){
  if (isActive(item)) return '';
  const reason = String(item.inactive_reason || '').trim();
  return reason ? `Ended / likely sold · ${reason}` : 'Ended / likely sold';
}
function activeCollection(){ return COLLECTIONS.find(c => c.id === active) || COLLECTIONS[0]; }
function showToast(msg){ toast.textContent = msg; toast.classList.add('show'); setTimeout(()=>toast.classList.remove('show'), 1100); }
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
  const activeNow = isActive(item);
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
  const ended = inactiveLabel(item);
  const heartText = mode === 'saved' && !activeNow ? 'Remove' : (item.saved ? '♥ Hearted' : '♡ Heart');
  const heartClass = mode === 'saved' && !activeNow ? 'removeSaved' : `heart ${item.saved ? 'saved' : ''}`;
  return `<article class="card ${item.saved ? 'saved' : ''} ${activeNow ? '' : 'ended'}" style="--accent:${color}" data-id="${esc(item.item_id)}">
    <div class="media">
      <a class="thumb" href="${esc(item.url)}" target="_blank" rel="noopener">${img}</a>
      ${facts ? `<div class="factStack">${facts}</div>` : ''}
    </div>
    <div class="body">
      <div class="meta"><span class="score">${Math.round(item.smart_score || item.score || 0)}</span>${learn}${priceStack(item, kind, bids)}</div>
      ${ended ? `<div class="statusBadge">${esc(ended)}</div>` : ''}
      <a class="title" href="${esc(item.url)}" target="_blank" rel="noopener">${esc(item.title)}</a>
      <div class="reasons">${esc(item.reasons)}</div>
      <div class="judgment"><div class="meter" title="How worth inspecting this is for your taste; not a price-comp verdict yet."><b>${opp}</b><span>Opportunity</span></div><div class="meter" title="How much the listing context supports trusting the signal."><b>${conf}</b><span>Confidence</span></div></div>
      ${riskHtml}
      ${groupNote}
      ${action}
      <div class="seller">seller ${seller} · ${esc(item.search_name)}</div>
      <div class="actions">
        <button class="${heartClass}" onclick="toggleSaved(event,'${esc(item.item_id)}',${item.saved ? 0 : 1})">${heartText}</button>
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


# ---------------------------------------------------------------------------
# Live eBay lookup — one listing, straight from the Browse API on demand.
# Everything else in this app reads the local DB; this is the one route that
# goes out to eBay while you wait.
# ---------------------------------------------------------------------------

_ebay_lock = threading.Lock()
_ebay_client = None


def ebay_client() -> EbayClient:
    """Built once, lazily: the app must still boot with no keys configured."""
    global _ebay_client
    with _ebay_lock:
        if _ebay_client is None:
            cfg = load_config()
            if not cfg.has_ebay_keys:
                raise RuntimeError(
                    "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set — "
                    "check .env in the service's working directory")
            _ebay_client = EbayClient(
                cfg.ebay_client_id, cfg.ebay_client_secret,
                cfg.marketplace, cfg.buyer_country, cfg.buyer_postal_code)
        return _ebay_client


BROWSE_ID_RE = re.compile(r"v1\|\d+\|\d+")
# The number a human can copy off a listing page or out of a URL. Handles
# /itm/1234, /itm/some-title-slug/1234, ?item=1234, and a bare number.
LEGACY_ID_RE = re.compile(
    r"(?:/itm/(?:[^/?#]+/)?|[?&](?:item|iid)=|^)(\d{9,15})(?!\d)")

# Redirects are only ever followed within eBay, so a share link can't be used
# to make this Pi fetch an arbitrary address.
EBAY_HOSTS = ("ebay.io", "ebay.us", "ebay.com", "ebay.co.uk", "ebay.de",
              "ebay.fr", "ebay.it", "ebay.es", "ebay.ca", "ebay.com.au")


def _is_ebay_host(netloc: str) -> bool:
    host = netloc.lower().split("@")[-1].split(":")[0]
    return any(host == h or host.endswith("." + h) for h in EBAY_HOSTS)


def _find_id(text: str):
    found = BROWSE_ID_RE.search(text)
    if found:
        return "browse", found.group(0)
    found = LEGACY_ID_RE.search(text)
    if found:
        return "legacy", found.group(1)
    return None


def resolve_ebay_link(url: str, hops: int = 5) -> str:
    """Follow an eBay share link far enough to expose the item id.

    The share button on the eBay app hands out ebay.io/m/xxxx, which carries no
    id at all. Only Location headers are read — the listing page itself 403s
    for non-browser clients and its body is never needed.

    Must be GET, not HEAD: eBay's short-link service answers HEAD with a 302 to
    /n/error and only reveals the real target to a GET. stream=True keeps the
    body off the wire, which is the reason HEAD looked attractive to begin with.
    """
    current = url
    for _ in range(hops):
        parsed = urlparse(current)
        if not _is_ebay_host(parsed.netloc):
            raise ValueError("that link doesn't point at eBay")
        try:
            resp = requests.get(current, allow_redirects=False, timeout=10,
                                stream=True)
            location = resp.headers.get("Location")
            resp.close()
        except requests.RequestException as exc:
            raise ValueError(f"couldn't follow that link ({exc})") from exc
        if not location:
            return current
        current = (location if "://" in location
                   else f"{parsed.scheme}://{parsed.netloc}{location}")
        if _find_id(current):
            return current
    return current


def parse_item_ref(raw: str) -> tuple[str, str]:
    """('browse'|'legacy', id) from a Browse id, a bare number, or any eBay URL."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("pass ?id= a listing id, an item number, or an eBay link")
    found = _find_id(raw)
    if found:
        return found
    if raw.lower().startswith(("http://", "https://")):
        resolved = resolve_ebay_link(raw)
        found = _find_id(resolved)
        if found:
            return found
        raise ValueError("that's an eBay link, but not to a single listing"
                         if resolved == raw else
                         "that link redirected but never revealed a listing id")
    raise ValueError("no listing id found in that text")


def _strip_html(html: str) -> str:
    """Seller descriptions are hand-rolled HTML; reduce to readable text."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# Aspect names vary by seller ("Case Size", "Case Diameter", "Dial Diameter"),
# so promote by pattern rather than exact key. First match wins, and the
# specific patterns are listed before the loose ones.
SPEC_PATTERNS = [
    ("Case size", r"case\s*(size|diameter)|dial\s*diameter|watch\s*size"),
    ("Band/lug width", r"(band|strap|lug|bracelet)\s*(width|size)"),
    ("Thickness", r"thick|depth"),
    ("Weight", r"weight"),
    ("Movement", r"movement|calib(er|re)"),
]

# Sentences in the description that mention a dimension worth reading.
SPEC_HINT_RE = re.compile(
    r"(?i)(thick|depth|weigh|\blug\b|band width|strap width|case size|"
    r"case diameter|diameter|\d+\s?mm\b|\d+(\.\d+)?\s?(g|gram|grams|oz)\b)")


def _key_specs(aspects: dict) -> tuple[list, list]:
    """(promoted [label, value] pairs, aspect names consumed)."""
    promoted, used = [], []
    for label, pattern in SPEC_PATTERNS:
        rx = re.compile(pattern, re.I)
        for name, value in aspects.items():
            if name in used or not rx.search(name or ""):
                continue
            promoted.append([label, value])
            used.append(name)
            break
    return promoted, used


# What the seller says is wrong with it. On a for-parts watch this decides
# whether it's an afternoon at the bench or a donor, and it only ever appears
# in prose — no aspect or API field carries it.
FAULT_HINT_RE = re.compile(
    r"(?i)\b(overwound|over-wound|not running|does ?n[o']t run|won'?t run|frozen|"
    r"seized|stuck|rust|corrosion|pitted|scratch|dent|crack|chip|hairline|"
    r"missing|broken|bent|replaced|non-?original|redial|refinish|water damage|"
    r"needs? (a )?(service|cleaning|repair)|as[- ]is|for parts|does ?n[o']t "
    r"(advance|set|wind|keep)|mainspring|balance staff|stem)\b")


def _match_notes(text: str, pattern, limit: int = 6) -> list:
    """Sentences from a description matching a keyword pattern."""
    notes = []
    for chunk in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        line = chunk.strip()
        if 8 <= len(line) <= 240 and pattern.search(line):
            notes.append(line)
            if len(notes) >= limit:
                break
    return notes


def _spec_notes(text: str, limit: int = 6) -> list:
    """Lines from the description that mention size, thickness or weight."""
    return _match_notes(text, SPEC_HINT_RE, limit)


def _fault_notes(text: str, limit: int = 8) -> list:
    return _match_notes(text, FAULT_HINT_RE, limit)


# "Ref. 6606A-1127-55B", "Reference: 145.022", "ref no. 6239" — capture stops
# at the first comma/semicolon/newline so trailing prose stays out.
# The prefix matches any case, but the ref itself must be uppercase/digits —
# with (?i) everywhere, "no reference here" captured "here". A following
# lowercase word ends the ref, so "145.022 from 1969" stops at "145.022".
_REF_RE = re.compile(
    r"(?i:\bref(?:erence)?\.?\s*(?:number|no\.?|#)?\s*[:.\-]?)\s*"
    r"([A-Z0-9][A-Z0-9\-/\. ]{1,24}?)(?=\s*[,;)\n]|\s+[a-z]|$)")

# Year with context ("circa 1965", "Ca. 1950s", "manufactured 1972") beats a
# bare year, which beats nothing — descriptions are full of stray numbers.
_YEAR_CTX_RE = re.compile(
    r"(?i)(?:circa|ca\.?|c\.|year|manufactured|made|produced|dates?\s+(?:to|from)|from)"
    r"\s*[:\-]?\s*((?:19[0-9]{2}|20[0-2][0-9])(?:'?s)?)")
_YEAR_ANY_RE = re.compile(r"\b((?:19[0-9]{2}|20[0-2][0-9])(?:'?s)?)\b")


def _model_ref(aspects: dict, text: str):
    """(model, reference) from item specifics first, description as fallback."""
    model = ref = None
    for name, value in aspects.items():
        low = (name or "").lower()
        if model is None and "model" in low and "year" not in low:
            model = value
        if ref is None and ("reference" in low or low == "mpn"):
            ref = value
    if ref in ("Does not apply", "Does Not Apply", "NA", "N/A"):
        ref = None
    if not ref:
        found = _REF_RE.search(text or "")
        if found:
            ref = found.group(1).strip(" .")
    return model, ref


def _year_guess(*texts):
    """Specific year (or decade) from prose; context-anchored matches first."""
    for rx in (_YEAR_CTX_RE, _YEAR_ANY_RE):
        for t in texts:
            found = rx.search(t or "")
            if found:
                return found.group(1)
    return None


# Region ids whose scope covers the US when they appear in regionIncluded.
_US_COVERING = {"US", "WORLDWIDE", "NORTH_AMERICA", "AMERICAS"}


def _ships_to_us(d: dict):
    """True/False from shipToLocations; None when the listing carries no data.

    An explicit US (or Americas-wide) exclusion wins over any inclusion —
    'ships worldwide except US' is a common pattern on overseas listings.
    """
    ship_to = d.get("shipToLocations") or {}
    included = {r.get("regionId") for r in ship_to.get("regionIncluded") or []}
    excluded = {r.get("regionId") for r in ship_to.get("regionExcluded") or []}
    if not included and not excluded:
        return None
    if excluded & _US_COVERING:
        return False
    return bool(included & _US_COVERING)


def _format_time_left(secs: float) -> str:
    """Always show two useful units, seconds included once inside the hour.

    This is a snapshot taken at fetch time — it is already stale by the time it
    reaches a phone. The page re-derives the countdown from `ends` and ticks it
    live; this exists for API callers, which is why it carries seconds too.
    """
    if secs <= 0:
        return "ended"
    days, rem = int(secs // 86400), secs % 86400
    hours, mins, sec = int(rem // 3600), int(rem % 3600 // 60), int(rem % 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m {sec}s"
    return f"{mins}m {sec}s"


def item_summary(d: dict) -> dict:
    """The handful of fields worth reading on a phone, off the raw response."""
    options = d.get("buyingOptions") or []
    is_auction = "AUCTION" in options
    money = (d.get("currentBidPrice") if is_auction else d.get("price")) \
        or d.get("price") or {}
    end = d.get("itemEndDate") or ""
    time_left = None
    if end:
        try:
            dt = datetime.strptime(end[:19], "%Y-%m-%dT%H:%M:%S") \
                         .replace(tzinfo=timezone.utc)
            time_left = _format_time_left(
                (dt - datetime.now(timezone.utc)).total_seconds())
        except ValueError:
            pass
    seller = d.get("seller") or {}
    primary = (d.get("image") or {}).get("imageUrl", "")
    created = d.get("itemCreationDate") or ""
    listed_days = None
    if created:
        try:
            cdt = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S") \
                          .replace(tzinfo=timezone.utc)
            listed_days = int((datetime.now(timezone.utc) - cdt)
                              .total_seconds() // 86400)
        except ValueError:
            pass
    # Sold vs expired-unsold changes how to read the price entirely: a sold
    # listing is a comp, an unsold one is a relist candidate you can lowball.
    avail = (d.get("estimatedAvailabilities") or [{}])[0]
    ship = (d.get("shippingOptions") or [{}])[0]
    coupon = (d.get("availableCoupons") or [{}])[0]
    marketing = d.get("marketingPrice") or {}
    loc = d.get("itemLocation") or {}
    aspects = {a.get("name"): a.get("value")
               for a in (d.get("localizedAspects") or []) if a.get("name")}
    key_specs, spec_names = _key_specs(aspects)
    description = _strip_html(d.get("description") or "")
    short_desc = d.get("shortDescription") or ""
    cond_desc = _strip_html(d.get("conditionDescription") or "")
    model, ref = _model_ref(aspects, short_desc + "\n" + description)
    box = next((v for k, v in aspects.items()
                if re.search(r"(?i)original\s+box|box/packaging", k or "")), None)
    papers = next((v for k, v in aspects.items()
                   if re.search(r"(?i)with\s+papers|papers/coa", k or "")), None)
    manual = next((v for k, v in aspects.items()
                   if re.search(r"(?i)manual|booklet", k or "")), None)
    caseback = next((v for k, v in aspects.items()
                     if re.match(r"(?i)case\s*back$", (k or "").strip())), None)
    # Numeric diameter for colour-coding. Sellers write "40 mm" or
    # "44.70mm X 36mm" (lug-to-lug x diameter) — the smaller plausible
    # number is the diameter.
    case_val = next((v for label, v in key_specs if label == "Case size"), None)
    case_mm = None
    if case_val:
        nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", case_val)]
        nums = [n for n in nums if 16 <= n <= 60]
        if nums:
            case_mm = min(nums)
    lug_val = next((v for label, v in key_specs if label == "Band/lug width"), None)
    lug_mm = None
    if lug_val:
        nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", lug_val)]
        nums = [n for n in nums if 6 <= n <= 30]
        if nums:
            lug_mm = nums[0]
    # Prose year first (the seller's own dating), aspect Year Manufactured as
    # the fallback — specifics are often left at a placeholder value.
    year = _year_guess(short_desc, cond_desc, description) \
        or next((v for k, v in aspects.items()
                 if "year" in (k or "").lower() and v), None)
    return {
        "title": d.get("title"),
        # getItem hands back the full-size render; ask for the small one so a
        # phone on cellular pulls ~9 KB instead of ~175 KB.
        "image": re.sub(r"s-l\d+", "s-l500", primary) if primary else "",
        "price": money.get("value"),
        "currency": money.get("currency"),
        "buying": options,
        "is_auction": is_auction,
        "bid_count": d.get("bidCount"),
        # Bids alone can't tell a two-bidder duel from broad demand.
        "unique_bidders": d.get("uniqueBidderCount"),
        # The seller's own one-line summary — usually names the calibre.
        "short_description": d.get("shortDescription"),
        "brand": d.get("brand"),
        "model": model,
        "reference": ref,
        "year": year,
        "box": box,
        "papers": papers,
        "manual": manual,
        "caseback": caseback,
        "case_mm": case_mm,
        "lug_mm": lug_mm,
        "condition": d.get("condition"),
        # Numeric and locale-stable (7000 = for parts, 3000 = used), unlike
        # the free-text condition above.
        "condition_id": d.get("conditionId"),
        "ends": end,
        "time_left": time_left,
        "listed": created,
        "listed_days": listed_days,
        "availability": avail.get("estimatedAvailabilityStatus"),
        "sold_qty": avail.get("estimatedSoldQuantity"),
        "remaining_qty": avail.get("estimatedRemainingQuantity"),
        "min_bid": (d.get("minimumPriceToBid") or {}).get("value"),
        "shipping_cost": (ship.get("shippingCost") or {}).get("value"),
        "shipping_type": ship.get("shippingCostType"),
        "coupon": ({"amount": (coupon.get("discountAmount") or {}).get("value"),
                    "code": coupon.get("redemptionCode")}
                   if coupon.get("discountAmount") else None),
        "discount_pct": marketing.get("discountPercentage"),
        "location": ", ".join(x for x in (loc.get("city"),
                                          loc.get("stateOrProvince"),
                                          loc.get("country")) if x),
        "lot_size": d.get("lotSize") or None,
        "ships_to_us": _ships_to_us(d),
        "seller": seller.get("username"),
        "feedback_pct": seller.get("feedbackPercentage"),
        "feedback_score": seller.get("feedbackScore"),
        "returns": (d.get("returnTerms") or {}).get("returnsAccepted"),
        "photos": (1 if d.get("image") else 0) + len(d.get("additionalImages") or []),
        "aspects": aspects,
        # Dimensions first — they decide whether it fits the wrist and whether
        # a band you own will fit it, before price is worth thinking about.
        "key_specs": key_specs,
        "key_spec_names": spec_names,
        "spec_notes": _spec_notes(description),
        "fault_notes": _fault_notes(description),
        # Some sellers write a free-text condition note on top of eBay's
        # canonical condition name; it's where the real faults get described.
        "condition_description": cond_desc or None,
        "description": description[:8000],
        "description_truncated": len(description) > 8000,
        "url": d.get("itemWebUrl"),
        # Same date shape as Listed/Ends (2026-08-06), not 8/6/2026.
        # Always Mountain time (America/Denver), wherever the server runs.
        "fetched": (datetime.now(MOUNTAIN) if MOUNTAIN else datetime.now())
                   .strftime("%Y-%m-%d %I:%M %p").replace(" 0", " ", 1),
    }


def fetch_item(ref: str) -> dict:
    kind, item_id = parse_item_ref(ref)
    client = ebay_client()
    return (client.get_item(item_id) if kind == "browse"
            else client.get_item_by_legacy_id(item_id))


# ---------------------------------------------------------------------------
# National Rarities: the turnaroundrarities weekly auction drop. One trusted
# consignment seller (source of the IWC 3706, Navitimer A23322, and Chronomat),
# whose auctions mostly end Sunday night — so this is a browse-the-week page,
# not an alert stream. Everything they list is shown; taste only sets the order.
# ---------------------------------------------------------------------------

RARITIES_TTL = 180  # seconds; reopening the tab shouldn't cost an eBay sweep

_rarities_lock = threading.Lock()
_rarities_cache: dict = {"ts": 0.0, "items": None}


def _invalidate_rarities() -> None:
    """Force the next load to re-rank — a new heart changes the order."""
    with _rarities_lock:
        _rarities_cache["ts"] = 0.0


def fetch_rarities(db_path: str, fresh: bool = False) -> dict:
    """The week's lots, taste-ranked, with what he has hearted layered on top."""
    with _rarities_lock:
        age = time.time() - _rarities_cache["ts"]
        if _rarities_cache["items"] is not None and age < RARITIES_TTL and not fresh:
            return {"cached_secs": int(age), "items": _rarities_cache["items"]}
    items = fetch_seller_auctions(ebay_client())
    storage = Storage(db_path)
    try:
        likes, dislikes = _preference_profile(storage.feedback_rows())
        # Hearts on this tab are the sharpest signal available here: they are
        # about these lots, from this seller, not the scout's wider feed. They
        # weigh a little heavier than the thumbs history for that reason.
        hearted = storage.rarities_liked_titles()
        saved_ids = storage.rarities_saved_ids()
    finally:
        storage.close()
    for title in hearted:
        likes.update(_tokens(title))
    for item in items:
        bag = _tokens(item["title"])
        boost = (min(6.0, sum(min(likes[t], 4) for t in bag) * 0.22)
                 - min(4.0, sum(min(dislikes[t], 3) for t in bag) * 0.22))
        item["saved"] = item["id"] in saved_ids
        item["boost"] = round(boost, 2)
        item["taste"] = round(item["taste"] + boost, 2)
    # Hearts are not pinned to the top — that would crowd out the new lots the
    # tab exists to surface. They shift the ranking instead, and the heart
    # filter is there when he wants the shortlist on its own.
    items.sort(key=lambda r: (-r["taste"], r["ends"] or "9999"))
    with _rarities_lock:
        _rarities_cache["ts"] = time.time()
        _rarities_cache["items"] = items
    return {"cached_secs": 0, "items": items}


# ---------------------------------------------------------------------------
# The dashboard. `/` is the board; every tool hangs off it and links back.
# HUB_PORT is a second service (Watch Hub) on this same Pi, so its links are
# built client-side from whatever hostname you arrived on — that keeps one
# home-screen icon working over both Tailscale and the LAN.
# ---------------------------------------------------------------------------

HUB_PORT = 8090

APPS = [
    {"id": "scout", "name": "GemHunter", "icon": "&#128142;", "where": "local",
     "path": "/app", "blurb": "The ranked feed. Streams, saved gems, scoring brain."},
    {"id": "lookup", "name": "Listing lookup", "icon": "&#128269;", "where": "local",
     "path": "/item", "blurb": "Paste an eBay listing, get live JSON back."},
    {"id": "rarities", "name": "National Rarities", "icon": "&#127963;", "where": "local",
     "path": "/rarities", "blurb": "The turnaroundrarities weekly drop, ranked to your taste."},
    {"id": "search", "name": "Quick search", "icon": "&#9889;", "where": "hub",
     "path": "/search", "blurb": "Live eBay search, any query, newest first."},
    {"id": "lathe", "name": "Lathe outfits", "icon": "&#128296;", "where": "hub",
     "path": "/search?hunt=LATHE&preset=1&days=30",
     "blurb": "The standing 8mm lathe sweep, scored."},
    {"id": "jacot", "name": "Jacot tools", "icon": "&#9881;", "where": "hub",
     "path": "/search?hunt=JACOT&preset=1&days=30",
     "blurb": "The standing Jacot / pivot-polisher sweep."},
]

# Health is a property of the service a tile lives on, not of the tile. Probing
# per-tile meant tiles with nothing to probe defaulted to green while their
# service was down. Both probes are cheap and neither costs an eBay call.
SERVICES = {
    "local": {"name": "GemHunter", "health": "/api/health"},
    "hub": {"name": "Watch Hub", "health": "/manifest.json"},
}

DASHBOARD = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#08111f"><title>GemHunter</title>
<link rel="manifest" href="/manifest.json">
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#08111f;color:#e9eef6;font:16px/1.45 -apple-system,
 BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:18px 14px 48px;
 max-width:720px;margin:0 auto}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.eyebrow{font-size:12px;font-weight:750;color:#9fb0c9}
h1{font-size:40px;line-height:.95;margin:4px 0 0;font-weight:900;letter-spacing:-1.6px;
 background:linear-gradient(90deg,#f8fafc,#7dd3fc 48%,#facc15);
 -webkit-background-clip:text;background-clip:text;color:transparent}
.refresh{border:1px solid rgba(148,163,184,.22);background:rgba(15,23,42,.72);
 color:#e9eef6;width:44px;height:44px;border-radius:14px;font-size:20px;font-weight:800;
 flex:none;display:grid;place-items:center}
.refresh:disabled{opacity:.5}
.status{font-size:12px;color:#8ba0bd;margin:14px 0 4px;min-height:17px}
a.tile{display:flex;gap:13px;align-items:center;text-decoration:none;color:inherit;
 background:rgba(16,26,45,.96);border:1px solid rgba(148,163,184,.16);
 border-radius:20px;padding:15px;margin-top:11px}
a.tile:active{background:rgba(26,40,66,.96)}
.orb{width:46px;height:46px;border-radius:15px;display:grid;place-items:center;
 font-size:22px;background:#0b1425;border:1px solid rgba(148,163,184,.18);flex:none}
.tile h2{margin:0;font-size:17px;font-weight:800}
.tile p{margin:3px 0 0;color:#8ba0bd;font-size:13px}
.dot{width:9px;height:9px;border-radius:50%;background:#334765;flex:none}
.dot.ok{background:#4ade80}.dot.bad{background:#f87171}
.foot{color:#64758c;font-size:12px;margin-top:22px;line-height:1.6}
.foot code{color:#8ba0bd;font-size:11px}
</style></head><body>
<div class="head">
  <div><div class="eyebrow">Private watch tools</div><h1>Dashboard</h1></div>
  <button class="refresh" id="refreshBtn" onclick="refreshAll()"
          aria-label="Refresh all apps">&#8635;</button>
</div>
<div class="status" id="status"></div>
<div id="tiles"></div>
<div class="foot">
  Every tool here is also a plain GET JSON endpoint &mdash;
  <code>/api/apps</code> lists them.
</div>
<script>
var APPS = __APPS__, SERVICES = __SERVICES__, HUB_PORT = __HUBPORT__;
var HUB = location.protocol + '//' + location.hostname + ':' + HUB_PORT;
function base(w){ return w === 'hub' ? HUB : ''; }
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g,
  function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

function render(){
  document.getElementById('tiles').innerHTML = APPS.map(function(a){
    return '<a class="tile" id="tile-' + a.id + '" href="' + base(a.where) + a.path + '">'
      + '<div class="orb">' + a.icon + '</div>'
      + '<div style="flex:1"><h2>' + esc(a.name) + '</h2><p>' + esc(a.blurb) + '</p></div>'
      + '<div class="dot" id="dot-' + a.id + '"></div></a>';
  }).join('');
}
render();

function setDots(where, cls){
  APPS.filter(function(a){ return a.where === where; }).forEach(function(a){
    document.getElementById('dot-' + a.id).className = 'dot ' + cls;
  });
}

// "Refresh all" re-checks every service, re-reads the feed's timestamp, and
// cache-busts each tile link so opening an app never lands on a stale page.
// It cannot force a new eBay sweep — that is the scout service's own timer.
function refreshAll(){
  var btn = document.getElementById('refreshBtn'), st = document.getElementById('status');
  btn.disabled = true; st.textContent = 'Refreshing\\u2026';
  var stamp = Date.now(), notes = [], pending = 0;

  APPS.forEach(function(a){
    var tile = document.getElementById('tile-' + a.id);
    tile.href = base(a.where) + a.path
              + (a.path.indexOf('?') < 0 ? '?' : '&') + 'v=' + stamp;
  });

  function finish(){
    if (--pending > 0) return;
    btn.disabled = false;
    st.textContent = notes.length ? notes.join('  \\u00b7  ') : 'All services responded.';
  }

  Object.keys(SERVICES).forEach(function(w){
    pending++;
    fetch(base(w) + SERVICES[w].health + '?v=' + stamp, {cache: 'no-store'})
      .then(function(r){ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(d){
        if (d.ebay_keys === false) {
          setDots(w, 'bad'); notes.push(SERVICES[w].name + ': eBay keys missing');
        } else {
          setDots(w, 'ok');
        }
      })
      .catch(function(e){
        setDots(w, 'bad'); notes.push(SERVICES[w].name + ' unreachable (' + e.message + ')');
      })
      .then(finish);
  });

  pending++;
  fetch('/api/listings?stream=repair&v=' + stamp, {cache: 'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(d){ if (d.updated) notes.push('feed ' + d.updated); })
    .catch(function(){})
    .then(finish);
}
refreshAll();
</script></body></html>
"""


def _dashboard() -> bytes:
    return (DASHBOARD
            .replace("__APPS__", json.dumps(APPS))
            .replace("__SERVICES__", json.dumps(SERVICES))
            .replace("__HUBPORT__", str(HUB_PORT))
            ).encode("utf-8")


ITEM_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#08111f"><title>Listing lookup</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#08111f;color:#e9eef6;font:16px/1.45 -apple-system,
 BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:16px 14px 48px;max-width:760px;margin:0 auto}
h1{font-size:20px;margin:4px 0 2px}a{color:#7cc4ff}
.sub{color:#8ba0bd;font-size:13px;margin:0 0 16px}
input,button{width:100%;font:inherit;color:#e9eef6;background:#0f1c30;
 border:1px solid #23395c;border-radius:11px;padding:12px 13px;appearance:none}
/* flex, so the wrapper is exactly the input's height — as a block it picked up
   the inline baseline gap and anything centred in it sat low. */
.field{position:relative;display:flex}
.field input{flex:1;padding-right:52px}
/* Pinned top and bottom rather than centred: fills the input's full height
   whatever that height is, and gives a proper thumb-sized target. */
/* margin:0 matters — the generic `button` rule below carries margin-top:12px,
   which this never overrode, so the button sat 12px low and 12px short. */
.clear{display:none;position:absolute;top:0;bottom:0;right:0;width:48px;height:auto;
 margin:0;padding:0;border:0;border-radius:0 11px 11px 0;background:transparent;
 color:#8ba0bd;font-size:25px;font-weight:400;line-height:1;place-items:center}
.clear.on{display:grid}
.clear:active{background:rgba(148,163,184,.15);color:#e9eef6}
button{background:#c9a227;color:#0b1220;border:0;font-weight:700;margin-top:12px}
button:disabled{opacity:.55}
.card{background:#0f1c30;border:1px solid #1d2f4c;border-radius:14px;padding:14px;margin-top:16px}
.t{font-size:15px;margin:0 0 10px;line-height:1.35}
.shot{display:block;width:100%;max-width:260px;margin:0 auto 12px;border-radius:12px;
 background:#08111f}
.sd{margin:0 0 10px;padding:9px 11px;background:#0b1526;border-left:3px solid #c9a227;
 border-radius:0 10px 10px 0;color:#c9d6e8;font-size:13px;line-height:1.4}
.sd.note{border-left-color:#ff4d3d}
.callout{margin:0 0 10px;padding:10px 12px;border-radius:11px;background:#122239;
 border:1px solid #23395c;font-size:18px;font-weight:800;text-align:center}
.callout small{font-size:11px;font-weight:600;opacity:.6;vertical-align:middle}
.callout.bad{background:rgba(255,77,61,.12);border-color:#ff4d3d;color:#ff6b5c;font-size:21px}
.callout.good{background:rgba(74,222,128,.1);border-color:#4ade80;color:#7ee2a8}
.callout.warn{background:rgba(251,191,36,.1);border-color:#fbbf24;color:#fde68a;font-size:17px}
.listed{color:#7dd3fc;font-size:17px;font-weight:800}
.model{font-weight:800;font-size:15px}
.fb{font-weight:700}
.fb.good{color:#4ade80;font-size:17px;font-weight:800}
.fb.bad{color:#ff4d3d;font-size:17px;font-weight:800}
.fb.gold{color:#f0d67a;font-size:17px;font-weight:800}
.fb.purple{color:#c084fc;font-size:17px;font-weight:800}
.notes{margin:0 0 10px;padding:9px 11px;background:#0b1526;border:1px solid #1d2f4c;
 border-radius:11px;font-size:13px;color:#c9d6e8}
.notes b{display:block;color:#8ba0bd;font-size:11px;text-transform:uppercase;
 letter-spacing:.6px;margin-bottom:5px}
.notes p{margin:0 0 5px}
.notes.fault{border-color:rgba(255,77,61,.4);background:rgba(255,77,61,.07)}
.notes.fault b{color:#ff8a7a}
.desc{white-space:pre-wrap;background:#0b1526;border:1px solid #1d2f4c;border-radius:11px;
 padding:12px;font-size:13px;line-height:1.5;color:#c9d6e8;max-height:340px;overflow-y:auto}
.big{font-size:26px;font-weight:700;color:#f0d67a}
.k{display:flex;justify-content:space-between;gap:12px;padding:7px 0;
 border-top:1px solid #1d2f4c;font-size:14px}
.k span:first-child{color:#8ba0bd}
.k span:last-child{text-align:right}
.tl{font-variant-numeric:tabular-nums;font-size:19px;font-weight:800;line-height:1.1}
.tl.soon{color:#ff4d3d}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.tl.soon{animation:pulse 2s ease-in-out infinite}
.err{background:#3a1c1c;border:1px solid #6b2f2f;color:#ffc9bd;padding:12px;
 border-radius:11px;margin-top:16px;font-size:14px}
pre{background:#0b1526;border:1px solid #1d2f4c;border-radius:11px;padding:12px;
 overflow-x:auto;font-size:11px;line-height:1.4;color:#b9cbe4}
summary{cursor:pointer;color:#8ba0bd;font-size:13px;margin-top:14px}
</style></head><body>
<h1><a href="/">&larr;</a> &nbsp;Listing lookup</h1>
<p class="sub">Live from the eBay Browse API with your developer keys. Paste a
listing URL, an item number, or a share link from the eBay app.</p>
<div class="field">
  <input id="q" placeholder="ebay.io/m/pG5UUs &nbsp;or&nbsp; 127998919225"
         autocapitalize="none" autocorrect="off" enterkeyhint="go">
  <button type="button" class="clear" id="clear" onclick="clearField()"
          aria-label="Clear">&times;</button>
</div>
<button id="btn" onclick="go()">Fetch live data</button>
<div id="out"></div>
<script>
var P = new URLSearchParams(location.search);
if (P.get('id')) { document.getElementById('q').value = P.get('id'); }
document.getElementById('q').addEventListener('keydown', function(e){
  if (e.key === 'Enter') go();
});
document.getElementById('q').addEventListener('input', toggleClear);

function toggleClear(){
  document.getElementById('clear')
          .classList.toggle('on', !!document.getElementById('q').value);
}
// Clearing means "I'm looking up something else", so the old card goes too —
// leaving it would show a frozen listing whose countdown and auto-refresh have
// already stopped, which reads as live when it isn't.
function clearField(){
  var q = document.getElementById('q');
  q.value = '';
  toggleClear();
  if (ticker) { clearInterval(ticker); ticker = null; }
  if (refresher) { clearTimeout(refresher); refresher = null; }
  lastFetch = 0; staleMsg = '';
  document.getElementById('out').innerHTML = '';
  history.replaceState(null, '', '/item');
  q.focus();
}
toggleClear();
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g,
  function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function row(k, v){ return v == null || v === '' ? '' :
  '<div class="k"><span>' + k + '</span><span>' + esc(v) + '</span></div>'; }
function noteBlock(label, lines, cls){
  if (!lines || !lines.length) return '';
  return '<div class="notes' + cls + '"><b>' + label + '</b>'
       + lines.map(function(n){ return '<p>' + esc(n) + '</p>'; }).join('') + '</div>';
}
// Case diameter, colour-coded to wearability: under 36 red, 36-41 green,
// over 41 purple. Unparseable sizes stay gold — flagged, not judged.
function caseAndLug(s){
  var ks = s.key_specs || [], caseKv = null, lugKv = null, rest = [];
  ks.forEach(function(kv){
    if (kv[0] === 'Case size') caseKv = kv;
    else if (kv[0] === 'Band/lug width') lugKv = kv;
    else rest.push(kv);
  });
  var h = '';
  if (caseKv) {
    var cls = 'gold';
    if (s.case_mm != null)
      cls = s.case_mm > 41 ? 'purple' : (s.case_mm >= 36 ? 'good' : 'bad');
    h += '<div class="k"><span>Case diameter</span><span class="fb ' + cls + '">'
       + esc(caseKv[1])
       + (s.case_mm != null && /x/i.test(caseKv[1])
            ? ' <small style="opacity:.65">(' + s.case_mm + 'mm dia)</small>' : '')
       + '</span></div>';
  }
  if (lugKv) {
    // 18 gold, 19-22 green, over 22 purple; below 18 stays neutral (no band
    // was specified for it). Unparseable stays neutral too.
    var lc = '';
    if (s.lug_mm != null) {
      if (s.lug_mm === 18) lc = ' gold';
      else if (s.lug_mm >= 19 && s.lug_mm <= 22) lc = ' good';
      else if (s.lug_mm > 22) lc = ' purple';
    }
    h += '<div class="k"><span>Lug width</span><span class="fb' + lc + '">'
       + esc(lugKv[1]) + '</span></div>';
  }
  return h + rest.map(function(kv){ return row(kv[0], kv[1]); }).join('');
}

function yesNoRow(label, v){
  if (!v) return '';
  var cls = /^yes/i.test(v) ? ' good' : (/^no/i.test(v) ? ' bad' : '');
  return '<div class="k"><span>' + label + '</span><span class="fb' + cls + '">'
       + esc(v) + '</span></div>';
}
function money(v){
  var n = Number(v);
  return isNaN(n) ? String(v)
       : '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2,
                                          maximumFractionDigits: 2});
}
function isOver(s){ return s.ends ? Date.parse(s.ends) <= Date.now() : false; }
// Sold vs expired-unsold, from estimatedAvailabilities. "ended" alone hides
// the only fact that decides how to read the final price: a sold listing is a
// comp, an unsold one is a relist candidate you can approach with a low offer.
function outcomeBadge(s){
  if (!isOver(s)) return '';
  // sold_qty > 0 is definitive. OUT_OF_STOCK alone only counts when eBay
  // omitted the quantity — an unsold ending also reads OUT_OF_STOCK, but with
  // an explicit sold_qty of 0.
  var sold = Number(s.sold_qty || 0) > 0
          || (s.sold_qty == null && s.availability === 'OUT_OF_STOCK');
  return sold
    ? '<div class="callout good">SOLD \\u2014 price above is a comp</div>'
    : '<div class="callout warn">ENDED UNSOLD \\u2014 relist / lowball candidate</div>';
}
function runLength(listed, ends){
  var a = listed ? Date.parse(listed) : NaN, b = ends ? Date.parse(ends) : NaN;
  if (isNaN(a) || isNaN(b) || b <= a) return '';
  return ' \\u00b7 ' + Math.round((b - a) / 86400000) + 'd run';
}

// The countdown is re-derived from the listing's own end timestamp and ticked
// here, so it stays true no matter how long the page has been open. Anything
// computed on the Pi is stale the moment it's sent.
var ticker = null, lastFetch = 0, staleMsg = '', lastItem = null;

// navigator.clipboard needs HTTPS; the Pi serves plain http, so fall back to
// the textarea/execCommand path, which still works there.
function copyJson(){
  if (!lastItem) return;
  var text = JSON.stringify(lastItem, null, 2);
  var done = function(ok){
    var b = document.getElementById('copyBtn');
    if (!b) return;
    b.textContent = ok ? 'Copied \\u2713' : 'Copy failed \\u2014 long-press the JSON above';
    setTimeout(function(){ b.textContent = 'Copy raw JSON'; }, 2500);
  };
  var legacy = function(){
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    done(ok);
  };
  if (navigator.clipboard && window.isSecureContext) {
    // If the modern API refuses (permissions, focus), try the old way
    // before reporting failure.
    navigator.clipboard.writeText(text).then(function(){ done(true); }, legacy);
    return;
  }
  legacy();
}
function ago(ms){
  var s = Math.floor(ms / 1000);
  if (s < 5) return 'just now';
  if (s < 60) return s + 's ago';
  var m = Math.floor(s / 60);
  return m < 60 ? m + 'm ago' : Math.floor(m / 60) + 'h ago';
}
function fmtLeft(ms){
  if (ms <= 0) return 'ended';
  var t = Math.floor(ms / 1000);
  var d = Math.floor(t / 86400), h = Math.floor(t % 86400 / 3600),
      m = Math.floor(t % 3600 / 60), s = t % 60;
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm ' + s + 's';
  return m + 'm ' + s + 's';
}
function startTicker(ends){
  if (ticker) { clearInterval(ticker); ticker = null; }
  var el = document.getElementById('tl');
  var end = ends ? Date.parse(ends) : NaN;
  if (!el || isNaN(end)) return;
  var paint = function(){
    var left = end - Date.now();
    el.textContent = fmtLeft(left);
    el.classList.toggle('soon', left > 0 && left < 3600000);
    // Relative age, not a wall-clock stamp: two refreshes inside the same
    // minute have to look different, or the display can't be trusted.
    var f = document.getElementById('fresh');
    if (f) f.textContent = staleMsg || (lastFetch ? ago(Date.now() - lastFetch) : '');
    if (left <= 0 && ticker) { clearInterval(ticker); ticker = null; }
  };
  paint();
  ticker = setInterval(paint, 1000);
}

// The clock ticks on its own, but price and bid count only change when we ask
// eBay again. Re-fetch on a cadence matched to how fast the listing can move,
// and never while the tab is hidden — a phone in your pocket shouldn't burn
// API quota.
var refresher = null;
function refreshEvery(left){
  if (left < 600000) return 20000;      // last 10 min: sniping range
  if (left < 3600000) return 60000;     // last hour
  if (left < 86400000) return 300000;   // last day
  return 900000;
}
function scheduleRefresh(ends){
  if (refresher) { clearTimeout(refresher); refresher = null; }
  var end = ends ? Date.parse(ends) : NaN;
  if (isNaN(end)) return;               // fixed-price with no end: nothing to chase
  var left = end - Date.now();
  if (left <= 0) return;
  refresher = setTimeout(function(){
    if (document.hidden) { scheduleRefresh(ends); return; }
    go(true);
  }, refreshEvery(left));
}
function stale(msg){
  staleMsg = msg;
  var f = document.getElementById('fresh');
  if (f) f.textContent = msg;
}
// Coming back to the tab should show current numbers, not whatever was on
// screen when you locked the phone.
document.addEventListener('visibilitychange', function(){
  if (!document.hidden && document.getElementById('tl')) go(true);
});
function go(silent){
  var q = document.getElementById('q').value.trim();
  if (!q) return;
  var btn = document.getElementById('btn'), out = document.getElementById('out');
  if (!silent) {
    history.replaceState(null, '', '/item?id=' + encodeURIComponent(q));
    btn.disabled = true; btn.textContent = 'Fetching\\u2026'; out.innerHTML = '';
  }
  fetch('/api/item?id=' + encodeURIComponent(q) + (silent ? '&_=' + Date.now() : ''),
        {cache: 'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d.error) {
        // A background refresh must never wipe a card you're reading — an
        // ended auction 404s, and that answer belongs next to the last price.
        if (silent) { stale(d.error); return; }
        out.innerHTML = '<div class="err">' + esc(d.error) + '</div>';
        return;
      }
      var s = d.summary;
      // What is it -> what shape is it in -> what will it cost me and how long
      // have I got -> who am I buying from -> housekeeping.
      var bids = s.bid_count == null ? null
               : (s.unique_bidders ? s.bid_count + ' from ' + s.unique_bidders + ' bidders'
                                   : String(s.bid_count));
      // For-parts is the single fact that decides whether this is bench work
      // or a wearer, so it gets size and colour rather than a quiet row.
      var cid = Number(s.condition_id);
      var condCls = cid === 7000 ? ' bad' : (cid === 1000 || cid === 1500 ? ' good' : '');
      // A 100%/720 seller and a 91%/3 seller should not look alike at a glance.
      var pct = s.feedback_pct == null ? null : Number(s.feedback_pct);
      var sc = Number(s.feedback_score || 0);
      var fbCls = pct == null ? ''
                : (sc < 10 || pct < 97 ? ' bad'
                : (pct >= 99 && sc >= 100 ? ' good' : ''));
      var fbText = pct == null ? null
                 : pct + '% · ' + sc + (sc < 10 ? ' sales — thin history' : ' sales');
      var h = '<div class="card">'
        + (s.image ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">'
                     + '<img class="shot" src="' + esc(s.image) + '" alt=""></a>' : '')
        + '<p class="t">' + esc(s.title) + '</p>'
        + (s.short_description ? '<p class="sd">' + esc(s.short_description) + '</p>' : '')
        + (s.condition ? '<div class="callout' + condCls + '">' + esc(s.condition)
                         + '</div>' : '')
        + (s.condition_description
             ? '<p class="sd note">' + esc(s.condition_description) + '</p>' : '')
        + caseAndLug(s)
        + noteBlock('Dimensions in the description', s.spec_notes, '')
        + noteBlock('What the seller says is wrong', s.fault_notes, ' fault')
        + row('Brand', s.brand)
        + ((s.model || s.reference)
             ? '<div class="k"><span>Model</span><span class="model">'
               + esc([s.model, s.reference ? 'Ref. ' + s.reference : null]
                     .filter(Boolean).join(' \\u2014 '))
               + '</span></div>' : '')
        + (s.year ? row('Year', s.year) : '')
        + '<div class="big">' + (s.price ? money(s.price) : 'n/a')
        + (s.is_auction ? ' <span style="font-size:13px;color:#8ba0bd">current bid</span>' : '')
        + '</div>'
        + outcomeBadge(s)
        + (s.ships_to_us === false
             ? '<div class="callout bad">DOES NOT SHIP TO THE US</div>' : '')
        + (s.ends ? '<div class="k"><span>Time left</span>'
                    + '<span id="tl" class="tl">' + esc(s.time_left || '') + '</span></div>'
                  : row('Time left', s.time_left))
        + row('Bids', bids)
        + (s.is_auction && s.min_bid && !isOver(s) ? row('Next bid', money(s.min_bid)) : '')
        + (s.listed
             ? '<div class="k"><span>Listed</span><span class="listed">'
               + esc(s.listed.slice(0,10))
               + (s.listed_days != null ? ' \\u00b7 ' + s.listed_days + 'd ago' : '')
               + '</span></div>'
             : '')
        + ((s.remaining_qty != null && (s.remaining_qty > 1 || (s.sold_qty || 0) > 1))
             ? row('Quantity', (s.sold_qty || 0) + ' sold \\u00b7 '
                               + s.remaining_qty + ' left') : '')
        + (s.shipping_cost != null
             ? row('Shipping', (Number(s.shipping_cost) === 0 ? 'free'
                                : money(s.shipping_cost))
                   + (s.shipping_type === 'CALCULATED' ? ' (calculated)' : '')) : '')
        + (s.coupon ? '<div class="k"><span>Coupon</span><span class="fb good">'
                      + money(s.coupon.amount) + ' off'
                      + (s.coupon.code ? ' \\u00b7 ' + esc(s.coupon.code) : '')
                      + '</span></div>' : '')
        + (s.discount_pct ? row('Marked down', s.discount_pct + '%') : '')
        + row('Location', s.location)
        + (s.lot_size ? row('Lot size', s.lot_size) : '')
        + row('Seller', s.seller)
        + (fbText ? '<div class="k"><span>Feedback</span>'
                    + '<span class="fb' + fbCls + '">' + esc(fbText) + '</span></div>' : '')
        + row('Returns', s.returns === null ? null : (s.returns ? 'yes' : 'no'))
        + row('Photos', s.photos)
        + row('Ends', s.ends
                ? s.ends.slice(0,10) + runLength(s.listed, s.ends)
                : null)
        + '<div class="k"><span>Last Updated</span><span id="fresh">'
        + esc(s.fetched || '') + '</span></div>'
        + yesNoRow('Original box', s.box)
        + yesNoRow('Manual/booklet', s.manual)
        + yesNoRow('Papers', s.papers)
        + (s.caseback ? '<div class="k"><span>Caseback</span>'
                        + '<span class="fb gold">' + esc(s.caseback)
                        + '</span></div>' : '');
      // Everything else, minus whatever was already promoted above.
      var used = s.key_spec_names || [];
      Object.keys(s.aspects || {}).forEach(function(k){
        if (used.indexOf(k) >= 0) return;
        if (s.brand && /^brand$/i.test(k)) return;
        if (s.model && /^model$/i.test(k)) return;
        if (s.reference && /reference/i.test(k)) return;
        if (s.year && /^year/i.test(k)) return;
        if (s.box && /original\s+box|box\/packaging/i.test(k)) return;
        if (s.papers && /with\s+papers|papers\/coa/i.test(k)) return;
        if (s.manual && /manual|booklet/i.test(k)) return;
        if (s.caseback && /^case\s*back$/i.test(k.trim())) return;
        h += row(k, s.aspects[k]);
      });
      h += '<div class="k"><span></span><span><a target="_blank" rel="noopener" href="'
         + esc(s.url) + '">open on eBay</a></span></div></div>'
         + (s.description
              ? '<details open><summary>Full description</summary><div class="desc">'
                + esc(s.description)
                + (s.description_truncated ? '\\n\\n[truncated \\u2014 see raw JSON]' : '')
                + '</div></details>'
              : '')
         + '<details><summary>Raw JSON from eBay</summary><pre>'
         + esc(JSON.stringify(d.item, null, 2)) + '</pre></details>'
         + '<button type="button" class="go" id="copyBtn" onclick="copyJson()">'
         + 'Copy raw JSON</button>';
      lastItem = d.item;
      out.innerHTML = h;
      lastFetch = Date.now(); staleMsg = '';
      startTicker(s.ends);
      scheduleRefresh(s.ends);
    })
    .catch(function(e){
      if (silent) { stale('refresh failed'); return; }
      out.innerHTML = '<div class="err">' + esc(e) + '</div>';
    })
    .then(function(){
      if (!silent) { btn.disabled = false; btn.textContent = 'Fetch live data'; }
    });
}
if (P.get('id')) go();
</script></body></html>
"""


RARITIES_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#08111f"><title>National Rarities</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#08111f;color:#e9eef6;font:16px/1.45 -apple-system,
 BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:16px 14px 48px;max-width:560px;margin:0 auto}
h1{font-size:20px;margin:4px 0 2px}a{color:#7cc4ff}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.sub{color:#8ba0bd;font-size:13px;margin:0 0 6px}
.refresh{border:1px solid rgba(148,163,184,.22);background:rgba(15,23,42,.72);
 color:#e9eef6;width:44px;height:44px;border-radius:14px;font-size:20px;font-weight:800;
 flex:none;display:grid;place-items:center}
.refresh:disabled{opacity:.5}
.status{font-size:12px;color:#8ba0bd;min-height:17px;margin-bottom:8px}
.card{background:#0f1c30;border:1px solid #1d2f4c;border-radius:16px;
 margin-top:14px;overflow:hidden}
/* The picture IS the page — full-bleed inside the card, info rides below. */
.shot{display:block;width:100%;min-height:200px;background:#0b1526}
.pic{position:relative;display:block}
/* Floats over the top-right of the photo. Sized for a thumb, and dark enough
   underneath that a white dial doesn't swallow it. */
.heart{position:absolute;top:9px;right:9px;width:42px;height:42px;
 border:0;border-radius:50%;background:rgba(8,17,31,.55);
 backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);
 color:#e9eef6;font-size:21px;line-height:1;display:grid;place-items:center;
 padding:0;transition:transform .12s ease}
.heart:active{transform:scale(.86)}
.heart.on{color:#ff4d6a;background:rgba(8,17,31,.72)}
.card.saved{border-color:rgba(255,77,106,.55)}
.filters{display:flex;gap:8px;margin-bottom:4px}
.filt{font:inherit;font-size:12px;font-weight:700;color:#8ba0bd;
 background:#0f1c30;border:1px solid #23395c;border-radius:9px;padding:5px 11px}
.filt.on{color:#ff4d6a;border-color:rgba(255,77,106,.5);
 background:rgba(255,77,106,.1)}
.info{padding:11px 13px 13px}
.row{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.bid{font-size:24px;font-weight:800;color:#f0d67a}
.bid small{font-size:12px;font-weight:600;color:#8ba0bd;margin-left:6px}
.ends{text-align:right;font-size:14px;font-weight:700;color:#7dd3fc;
 font-variant-numeric:tabular-nums}
.ends .left{display:block;font-size:12px;font-weight:600;color:#8ba0bd}
.ends.soon,.ends.soon .left{color:#ff4d3d}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.ends.soon{animation:pulse 2s ease-in-out infinite}
.t{margin:7px 0 0;color:#c9d6e8;font-size:13px;line-height:1.35;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tags{display:flex;gap:8px;align-items:center;margin-top:10px;font-size:12px;
 padding-top:9px;border-top:1px solid #17263f}
.parts{color:#ff8a7a;border:1px solid rgba(255,77,61,.4);border-radius:8px;
 padding:2px 8px;font-weight:700}
.copy{margin-left:auto;font:inherit;font-size:11px;font-weight:700;
 color:#8ba0bd;background:transparent;border:1px solid #23395c;
 border-radius:8px;padding:3px 9px}
.copy:active{background:rgba(148,163,184,.15);color:#e9eef6}
.copy.done{color:#4ade80;border-color:rgba(74,222,128,.5)}
/* Straight under the photo: what the seller's own Condition Description says,
   which is the only place "Pre-owned - Good" gets contradicted. */
.flags{display:flex;flex-wrap:wrap;gap:6px;padding:11px 13px 0}
.flags:empty{display:none}
.fl{font-size:11px;font-weight:800;letter-spacing:.4px;border-radius:7px;
 padding:3px 8px;border:1px solid}
.fl.bad{color:#ff5b48;border-color:#ff4d3d;background:rgba(255,77,61,.13)}
.fl.warn{color:#fbbf24;border-color:rgba(251,191,36,.5);background:rgba(251,191,36,.1)}
.fl.good{color:#4ade80;border-color:rgba(74,222,128,.45);background:rgba(74,222,128,.1)}
.mm{color:#8ba0bd;font-weight:700}
.mm.ok{color:#7dd3fc}
.err{background:#3a1c1c;border:1px solid #6b2f2f;color:#ffc9bd;padding:12px;
 border-radius:11px;margin-top:16px;font-size:14px}
.more{width:100%;font:inherit;font-weight:700;color:#e9eef6;background:#0f1c30;
 border:1px solid #23395c;border-radius:12px;padding:13px;margin-top:16px}
.more:disabled{opacity:.55}
</style></head><body>
<div class="head">
  <div>
    <h1><a href="/">&larr;</a> &nbsp;National Rarities</h1>
    <p class="sub"><a href="__STORE__" target="_blank" rel="noopener">turnaroundrarities</a>
    &middot; the week's auctions, ranked to your taste &middot; most end Sunday night</p>
  </div>
  <button class="refresh" id="refreshBtn" onclick="load(true)"
          aria-label="Refresh">&#8635;</button>
</div>
<div class="filters" id="filters"></div>
<div class="status" id="status"></div>
<div id="out"></div>
<div id="foot"></div>
<script>
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g,
  function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function money(v){
  var n = Number(v);
  return isNaN(n) ? String(v)
       : '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2,
                                          maximumFractionDigits: 2});
}
// "Sun 6:42 PM" in the phone's own timezone — the day is the fact that
// matters when everything funnels toward Sunday night.
function endLabel(iso){
  var d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleString(undefined, {weekday:'short'}) + ' '
       + d.toLocaleString(undefined, {hour:'numeric', minute:'2-digit'});
}
function fmtLeft(ms){
  if (ms <= 0) return 'ended';
  var t = Math.floor(ms / 1000);
  var d = Math.floor(t / 86400), h = Math.floor(t % 86400 / 3600),
      m = Math.floor(t % 3600 / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}
// Start on the top slice: the whole consignment runs to several hundred lots,
// which is a slow parse and a slow paint on a phone. LIMIT 0 means everything.
var LIMIT = 60, TOTAL = 0, ITEMS = [], ticker = null, flagTries = 0;
var SAVED = 0, SAVED_ONLY = false;
function flagHtml(flags){
  if (!flags || !flags.length) return '';
  return flags.map(function(f){
    return '<span class="fl ' + esc(f.sev) + '">' + esc(f.label) + '</span>';
  }).join('');
}
// Hearting is optimistic: the icon fills immediately, because waiting on the
// Pi round-trip makes a tap feel broken. If the write fails it reverts.
function toggleHeart(i){
  var it = ITEMS[i];
  if (!it) return;
  var want = !it.saved, btn = document.getElementById('hr-' + i),
      card = document.getElementById('card-' + i);
  paintHeart(btn, card, want);
  it.saved = want;
  fetch('/api/rarities/like', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({item_id: it.id, saved: want, title: it.title})
  })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d.ok) throw new Error('rejected');
      SAVED += want ? 1 : -1;
      paintFilters();
    })
    .catch(function(){
      it.saved = !want;
      paintHeart(btn, card, !want);
    });
}
function paintHeart(btn, card, on){
  if (btn) {
    btn.classList.toggle('on', on);
    btn.innerHTML = on ? '\\u2665' : '\\u2661';
  }
  if (card) card.classList.toggle('saved', on);
}
function paintFilters(){
  var el = document.getElementById('filters');
  if (!el) return;
  el.innerHTML = '<button type="button" class="filt' + (SAVED_ONLY ? ' on' : '')
    + '" onclick="toggleSavedOnly()">' + (SAVED_ONLY ? '\\u2665' : '\\u2661')
    + ' hearted' + (SAVED ? ' (' + SAVED + ')' : '') + '</button>';
}
function toggleSavedOnly(){
  SAVED_ONLY = !SAVED_ONLY;
  flagTries = 0;
  paintFilters();
  load(false);
}
// The Pi serves plain http, where navigator.clipboard does not exist, so the
// old textarea trick is the path that actually works on the phone.
function copyText(text, btn){
  var done = function(ok){
    if (!btn) return;
    var was = btn.textContent;
    btn.textContent = ok ? 'copied \\u2713' : 'copy failed';
    btn.classList.toggle('done', ok);
    setTimeout(function(){
      btn.textContent = was; btn.classList.remove('done');
    }, 1800);
  };
  var legacy = function(){
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    done(ok);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function(){ done(true); }, legacy);
    return;
  }
  legacy();
}
// The card's own record — title, bid, bids, closing time, size, score, and
// whatever the condition description flagged. Everything the panel shows.
function copyItem(i){
  var it = ITEMS[i];
  if (!it) return;
  copyText(JSON.stringify(it, null, 2), document.getElementById('cp-' + i));
}
// Condition text lands after the photos do. Patch the chips into the cards
// already on screen rather than re-rendering — a full repaint would throw
// away your scroll position mid-browse.
function patchFlags(items){
  items.forEach(function(it, i){
    var el = document.getElementById('fl-' + i);
    if (el && it.flags) el.innerHTML = flagHtml(it.flags);
  });
}
function paintClocks(){
  ITEMS.forEach(function(it, i){
    var el = document.getElementById('ends-' + i);
    if (!el || !it.ends) return;
    var left = Date.parse(it.ends) - Date.now();
    el.querySelector('.left').textContent = fmtLeft(left);
    el.classList.toggle('soon', left > 0 && left < 3600000);
  });
}
function render(items){
  ITEMS = items;
  document.getElementById('out').innerHTML = items.map(function(it, i){
    return '<div class="card' + (it.saved ? ' saved' : '') + '" id="card-' + i + '">'
      + '<div class="pic">'
      +   '<a href="' + esc(it.url) + '" target="_blank" rel="noopener">'
      +   (it.image ? '<img class="shot" loading="lazy" src="' + esc(it.image) + '" alt="">' : '')
      +   '</a>'
      +   '<button type="button" class="heart' + (it.saved ? ' on' : '') + '" '
      +     'id="hr-' + i + '" onclick="toggleHeart(' + i + ')" '
      +     'aria-label="Save this lot">' + (it.saved ? '&#9829;' : '&#9825;')
      +   '</button>'
      + '</div>'
      + '<div class="flags" id="fl-' + i + '">' + flagHtml(it.flags) + '</div>'
      + '<div class="info">'
      +   '<div class="row">'
      +     '<div class="bid">' + money(it.bid)
      +       '<small>' + (it.bids ? it.bids + ' bid' + (it.bids > 1 ? 's' : '')
                                   : 'no bids yet') + '</small></div>'
      +     '<div class="ends" id="ends-' + i + '">' + esc(endLabel(it.ends))
      +       '<span class="left"></span></div>'
      +   '</div>'
      +   '<p class="t">' + esc(it.title) + '</p>'
      +   '<div class="tags">'
      +     (it.for_parts ? '<span class="parts">FOR PARTS</span>' : '')
      +     (it.mm ? '<span class="mm' + (it.mm >= 36 ? ' ok' : '') + '">'
                     + it.mm + 'mm</span>' : '')
      +     '<button type="button" class="copy" id="cp-' + i + '" '
      +       'onclick="copyItem(' + i + ')">copy JSON</button>'
      +     '<a class="inspect" href="/item?id=' + encodeURIComponent(it.id) + '">inspect &rarr;</a>'
      +   '</div>'
      + '</div></div>';
  }).join('');
  if (ticker) clearInterval(ticker);
  paintClocks();
  ticker = setInterval(paintClocks, 30000);
  paintFoot();
}
function paintFoot(){
  var foot = document.getElementById('foot');
  if (LIMIT && TOTAL > ITEMS.length) {
    foot.innerHTML = '<button type="button" class="more" id="moreBtn">'
      + 'Show all ' + TOTAL + ' lots</button>';
    document.getElementById('moreBtn').onclick = function(){
      this.disabled = true;
      this.textContent = 'Loading all ' + TOTAL + '…';
      LIMIT = 0;
      load(false);
    };
  } else {
    foot.innerHTML = '';
  }
}
// A failed "show all" must not leave a dead disabled button behind: put the
// cap back so the footer offers the expansion again.
// The background pass is still reading condition text. Come back for it a few
// times, backing off each round, then stop rather than polling forever.
function pollFlags(){
  if (flagTries >= 6) return;
  flagTries++;
  setTimeout(function(){
    fetch('/api/rarities?limit=' + LIMIT + (SAVED_ONLY ? '&saved=1' : '')
          + '&_=' + Date.now(), {cache: 'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d.items) return;
        ITEMS = d.items;
        patchFlags(d.items);
        if (d.details_pending) pollFlags();
      })
      .catch(function(){});
  }, 2000 * flagTries);
}
function failedExpand(){
  if (!LIMIT) { LIMIT = 60; paintFoot(); }
}
function load(fresh){
  var btn = document.getElementById('refreshBtn'), st = document.getElementById('status');
  btn.disabled = true;
  if (!ITEMS.length) st.textContent = 'Fetching the week\\u2019s auctions\\u2026';
  fetch('/api/rarities?limit=' + LIMIT + (SAVED_ONLY ? '&saved=1' : '')
        + (fresh ? '&fresh=1' : ''), {cache: 'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d.error) {
        if (!ITEMS.length)
          document.getElementById('out').innerHTML =
            '<div class="err">' + esc(d.error) + '</div>';
        st.textContent = 'Refresh failed \\u2014 showing what was loaded.';
        failedExpand();
        return;
      }
      TOTAL = d.total || d.items.length;
      SAVED = d.saved_count || 0;
      paintFilters();
      render(d.items);
      flagTries = 0;
      if (d.details_pending) pollFlags();
      st.textContent = (SAVED_ONLY
          ? d.items.length + ' hearted'
          : (d.items.length < TOTAL
              ? 'Top ' + d.items.length + ' of ' + TOTAL + ' lots'
              : TOTAL + ' auctions'))
        + ' \\u00b7 updated ' + d.updated + (d.cached_secs ? ' (cached)' : '');
    })
    .catch(function(e){
      if (!ITEMS.length)
        document.getElementById('out').innerHTML = '<div class="err">' + esc(e) + '</div>';
      st.textContent = 'Refresh failed.';
      failedExpand();
    })
    .then(function(){ btn.disabled = false; });
}
// Coming back to the tab re-pulls through the server cache — current bids on
// screen, but no eBay call unless the cache has actually gone stale.
document.addEventListener('visibilitychange', function(){
  if (!document.hidden && ITEMS.length) load(false);
});
load(false);
</script></body></html>
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
        if parsed.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, _dashboard(), "text/html; charset=utf-8")
            return
        # /gems.html kept so old phone bookmarks land on the scout, not a 404.
        if parsed.path in ("/app", "/gems.html"):
            self._send(HTTPStatus.OK, _html(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/apps":
            self._json(HTTPStatus.OK, {"hub_port": HUB_PORT, "apps": APPS,
                                       "services": SERVICES})
            return
        if parsed.path == "/api/health":
            try:
                keys = load_config().has_ebay_keys
            except Exception:
                keys = False
            self._json(HTTPStatus.OK, {
                "ok": True,
                "ebay_keys": keys,
                "db": self.db_path,
                "updated": _now_str(),
            })
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
        if parsed.path == "/item":
            self._send(HTTPStatus.OK, ITEM_PAGE.encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if parsed.path == "/rarities":
            self._send(HTTPStatus.OK,
                       RARITIES_PAGE.replace("__STORE__", RARITIES_STORE_URL)
                                    .encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if parsed.path == "/api/rarities":
            params = parse_qs(parsed.query)
            fresh = params.get("fresh", [""])[0] in ("1", "true", "yes")
            try:
                data = fetch_rarities(self.db_path, fresh)
            except RuntimeError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                self._json(HTTPStatus.BAD_GATEWAY,
                           {"error": f"eBay returned HTTP {code}"})
                return
            except requests.RequestException as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            # The cache holds the whole consignment because ranking needs it,
            # but a few hundred cards is a slow parse and a slow paint on a
            # phone. Send the top slice; "show all" asks for limit=0.
            items = data["items"]
            try:
                limit = int(params.get("limit", ["60"])[0])
            except ValueError:
                limit = 60
            saved_only = params.get("saved", [""])[0] in ("1", "true", "yes")
            if saved_only:
                items = [i for i in items if i.get("saved")]
            shown = items[:limit] if limit > 0 else items
            # Condition text arrives on a background pass; the page asks again
            # while `details_pending` is non-zero and patches the chips in.
            pending = apply_details(shown)
            if pending:
                start_detail_pass(ebay_client(), items, top=max(limit, 60))
            self._json(HTTPStatus.OK, {
                "updated": _now_str(),
                "seller": RARITIES_SELLER,
                "store": RARITIES_STORE_URL,
                "cached_secs": data["cached_secs"],
                "total": len(items),
                "saved_count": sum(1 for i in data["items"] if i.get("saved")),
                "details_pending": pending,
                "items": shown,
            })
            return
        if parsed.path == "/api/item":
            params = parse_qs(parsed.query)
            ref = (params.get("id", [""])[0] or params.get("url", [""])[0])
            try:
                item = fetch_item(ref)
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                self._json(HTTPStatus.BAD_GATEWAY, {"error": (
                    "eBay has no live record of that listing — ended listings "
                    "drop out of the Browse API" if code == 404
                    else f"eBay returned HTTP {code}")})
                return
            except requests.RequestException as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            # raw=1 hands back eBay's response untouched, nothing of ours added.
            if params.get("raw", [""])[0] in ("1", "true", "yes"):
                self._json(HTTPStatus.OK, item)
            else:
                self._json(HTTPStatus.OK, {"summary": item_summary(item),
                                           "item": item})
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
        if self.path == "/api/rarities/like":
            saved = bool(body.get("saved"))
            ok = storage.set_rarities_saved(item_id, saved,
                                            str(body.get("title", ""))[:300])
            storage.close()
            # The ranking is learned from hearts, so it has to be recomputed.
            _invalidate_rarities()
            self._json(HTTPStatus.OK, {"ok": ok, "saved": saved})
            return
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
