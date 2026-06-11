"""Small private web app for browsing GemHunter on a phone.

This intentionally uses only the Python standard library. The Pi already has
Python, and Tailscale provides the private network boundary, so this process can
stay tiny: read SQLite, render tabs, and accept Save/Hide actions.
"""

from __future__ import annotations

import argparse
import html
import json
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


STREAMS = [
    {"id": "repair", "label": "For Parts", "icon": "&#128295;", "color": "#f59e0b", "min": 6, "limit": 100},
    {"id": "chrono", "label": "Chronos", "icon": "&#9201;", "color": "#3b82f6", "min": 10, "limit": 100},
    {"id": "taste", "label": "Taste", "icon": "&#128142;", "color": "#a855f7", "min": 10, "limit": 100},
    {"id": "rolex", "label": "Rolex", "icon": "&#127919;", "color": "#22c55e", "min": 10, "limit": 10},
    {"id": "patek", "label": "Patek", "icon": "&#128081;", "color": "#eab308", "min": 10, "limit": 100},
]


def _now_str() -> str:
    now = datetime.now(MOUNTAIN) if MOUNTAIN else datetime.now().astimezone()
    hour12 = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    return f"{now.month}/{now.day}/{now.year} {hour12}:{now.minute:02d} {ampm} {now.strftime('%Z')}"


def _html() -> bytes:
    stream_json = json.dumps(STREAMS)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="GemHunter">
  <meta name="theme-color" content="#0b1220">
  <title>GemHunter</title>
  <style>
    *{{box-sizing:border-box}}
    body{{font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0b1220;color:#e2e8f0;margin:0;padding:0 16px 56px}}
    .wrap{{max-width:760px;margin:0 auto}}
    header{{padding:28px 0 14px}}
    h1{{font-size:44px;line-height:1;font-weight:850;letter-spacing:-1.5px;margin:0;background:linear-gradient(90deg,#22c55e,#3b82f6,#a855f7);-webkit-background-clip:text;background-clip:text;color:transparent}}
    .sub{{color:#94a3b8;font-size:13px;margin-top:7px}}
    .tabs{{position:sticky;top:0;z-index:10;display:flex;gap:8px;overflow-x:auto;padding:12px 0;background:#0b1220;border-bottom:1px solid #1e293b;-webkit-overflow-scrolling:touch}}
    .tab{{flex:0 0 auto;background:#1e293b;border:1px solid #334155;color:#cbd5e1;padding:9px 15px;border-radius:999px;font-size:14px;font-weight:700;cursor:pointer;white-space:nowrap}}
    .tab .badge{{margin-left:5px;opacity:.75;font-weight:600}}
    .tab.active{{background:var(--c);border-color:var(--c);color:#07111f}}
    .toolbar{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:14px 0 10px;color:#94a3b8;font-size:13px}}
    .reload{{border:1px solid #334155;background:#172033;color:#cbd5e1;border-radius:8px;padding:7px 10px;font-weight:650}}
    .grid{{display:flex;flex-direction:column;gap:11px}}
    .empty{{color:#64748b;padding:28px 4px}}
    .card{{display:flex;gap:13px;background:#131c2e;border:1px solid #233047;border-radius:13px;overflow:hidden;color:inherit;text-decoration:none}}
    .card.saved{{border-color:#eab308;box-shadow:0 0 0 1px rgba(234,179,8,.25)}}
    .thumb{{width:112px;min-width:112px;background:#0b1220;display:flex;align-items:center;justify-content:center}}
    .thumb img{{width:112px;height:112px;object-fit:cover}}
    .body{{padding:11px 12px 10px 0;min-width:0;flex:1}}
    .row1{{display:flex;align-items:center;gap:8px;margin-bottom:5px}}
    .score{{font-weight:850;font-size:15px;border-radius:7px;padding:2px 9px;color:#07111f;background:var(--c)}}
    .price{{margin-left:auto;font-weight:800;font-size:15px;white-space:nowrap}}
    .price small{{color:#94a3b8;font-weight:500}}
    .title{{font-weight:700;margin-bottom:4px}}
    .reasons{{color:#7dd3fc;font-size:12.5px;margin-bottom:3px}}
    .seller{{color:#94a3b8;font-size:12.5px}}
    .actions{{display:flex;gap:8px;margin-top:9px}}
    .actions button{{border:1px solid #334155;background:#1e293b;color:#cbd5e1;border-radius:8px;padding:6px 9px;font-weight:700}}
    .actions button.saved{{background:#eab308;color:#07111f;border-color:#eab308}}
    .toast{{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#e2e8f0;color:#0b1220;padding:8px 12px;border-radius:999px;font-weight:750;opacity:0;transition:opacity .18s}}
    .toast.show{{opacity:1}}
  </style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>GemHunter</h1>
    <div class="sub" id="updated">updated {_now_str()}</div>
  </header>
  <nav class="tabs" id="tabs"></nav>
  <div class="toolbar"><span id="summary">Loading...</span><button class="reload" onclick="loadActive()">Refresh</button></div>
  <main class="grid" id="grid"></main>
</div>
<div class="toast" id="toast"></div>
<script>
const STREAMS = {stream_json};
let active = STREAMS[0].id;
const tabs = document.getElementById('tabs');
const grid = document.getElementById('grid');
const summary = document.getElementById('summary');
const toast = document.getElementById('toast');

function money(n) {{
  return '$' + Number(n || 0).toLocaleString(undefined, {{maximumFractionDigits:0}});
}}
function escapeHtml(s) {{
  return String(s || '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}
function showToast(msg) {{
  toast.textContent = msg; toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 1100);
}}
function renderTabs(counts={{}}) {{
  tabs.innerHTML = STREAMS.map(s => `<button class="tab ${{s.id===active?'active':''}}" style="--c:${{s.color}}" onclick="selectTab('${{s.id}}')">${{s.icon}} ${{s.label}} <span class="badge">${{counts[s.id] ?? ''}}</span></button>`).join('');
}}
function selectTab(id) {{ active = id; renderTabs(window.counts || {{}}); loadActive(); }}
async function loadActive() {{
  const stream = STREAMS.find(s => s.id === active);
  summary.textContent = 'Loading ' + stream.label + '...';
  const res = await fetch(`/api/listings?stream=${{encodeURIComponent(active)}}`);
  const data = await res.json();
  window.counts = data.counts || {{}};
  renderTabs(window.counts);
  document.getElementById('updated').textContent = 'updated ' + data.updated;
  summary.textContent = `${{data.items.length}} shown`;
  if (!data.items.length) {{ grid.innerHTML = '<p class="empty">none yet</p>'; return; }}
  grid.innerHTML = data.items.map(item => card(item, stream.color)).join('');
}}
function card(item, color) {{
  const kind = item.buying_option === 'AUCTION' ? 'Auction' : 'BIN';
  const bids = item.buying_option === 'AUCTION' && item.bid_count ? ` · ${{item.bid_count}} bids` : '';
  const seller = item.seller_pct ? `${{Math.round(item.seller_pct)}}% (${{Number(item.seller_score||0).toLocaleString()}})` : '—';
  const img = item.image_url ? `<img src="${{escapeHtml(item.image_url)}}" loading="lazy">` : '';
  return `<article class="card ${{item.saved ? 'saved' : ''}}" style="--c:${{color}}" data-id="${{escapeHtml(item.item_id)}}">
    <a class="thumb" href="${{escapeHtml(item.url)}}" target="_blank" rel="noopener">${{img}}</a>
    <div class="body">
      <div class="row1"><span class="score">${{Math.round(item.score || 0)}}</span><span class="price">${{money(item.price)}}<small> ${{kind}}${{bids}}</small></span></div>
      <a class="title" href="${{escapeHtml(item.url)}}" target="_blank" rel="noopener">${{escapeHtml(item.title)}}</a>
      <div class="reasons">${{escapeHtml(item.reasons)}}</div>
      <div class="seller">seller ${{seller}} · ${{escapeHtml(item.search_name)}}</div>
      <div class="actions">
        <button class="${{item.saved ? 'saved' : ''}}" onclick="toggleSaved(event,'${{escapeHtml(item.item_id)}}',${{item.saved ? 0 : 1}})">${{item.saved ? 'Saved' : 'Save'}}</button>
        <button onclick="hideItem(event,'${{escapeHtml(item.item_id)}}')">Hide</button>
      </div>
    </div>
  </article>`;
}}
async function action(path, body) {{
  const res = await fetch(path, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}}
async function toggleSaved(ev, id, saved) {{
  ev.preventDefault(); ev.stopPropagation();
  await action('/api/save', {{item_id:id, saved:!!saved}});
  showToast(saved ? 'Saved' : 'Unsaved');
  loadActive();
}}
async function hideItem(ev, id) {{
  ev.preventDefault(); ev.stopPropagation();
  await action('/api/hide', {{item_id:id, hidden:true}});
  document.querySelector(`[data-id="${{CSS.escape(id)}}"]`)?.remove();
  showToast('Hidden');
}}
renderTabs(); loadActive();
</script>
</body></html>""".encode("utf-8")


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
        if parsed.path == "/api/listings":
            stream = parse_qs(parsed.query).get("stream", ["repair"])[0]
            stream_cfg = next((s for s in STREAMS if s["id"] == stream), STREAMS[0])
            storage = Storage(self.db_path)
            counts = {
                s["id"]: len(storage.top_gems(s["min"], s["limit"], stream=s["id"]))
                for s in STREAMS
            }
            rows = storage.top_gems(
                stream_cfg["min"], stream_cfg["limit"], stream=stream_cfg["id"])
            storage.close()
            self._json(HTTPStatus.OK, {
                "updated": _now_str(),
                "counts": counts,
                "items": rows,
            })
            return
        if parsed.path == "/manifest.json":
            self._json(HTTPStatus.OK, {
                "name": "GemHunter",
                "short_name": "GemHunter",
                "display": "standalone",
                "start_url": "/",
                "theme_color": "#0b1220",
                "background_color": "#0b1220",
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
            ok = storage.set_hidden(item_id, bool(body.get("hidden", True)))
        else:
            storage.close()
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        storage.close()
        self._json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})

    def log_message(self, fmt: str, *args) -> None:
        # Keep systemd logs quiet; errors still surface via exceptions.
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
