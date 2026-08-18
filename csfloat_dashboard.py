#!/usr/bin/env python3
"""
CSFloat Vanilla Knife Dashboard
===============================

A small LOCAL web app to watch vanilla knife prices on CSFloat. Zero installs —
it uses Python's built-in web server, and the charts render in your browser.

WHAT IT DOES
  - Serves a dashboard at http://localhost:8000
  - Shows current cheapest price + a trend chart for each tracked item
    (vanilla knives, plus any skinned items like gloves in TRACKED_ITEMS),
    windowed to the last 7/14/30 days so you can see the recent price drop-off
  - A "Refresh prices" button pulls live prices from CSFloat on demand
  - Records every fetch to a history CSV so trends build up over time
  - Flags each knife BUY-ish / WAIT / NEUTRAL based on where its price sits
    within its own logged range for the selected window

THE END-TO-END DATA FLOW
  CSFloat's API has no historical-price endpoint (checked docs.csfloat.com) —
  the only way to get a real trend is to keep sampling current listings over
  time, and this machine isn't on 24/7. So collection happens in two places
  that feed the same chart:

    1. IN THE CLOUD (does the heavy lifting): a GitHub Actions workflow,
       .github/workflows/snapshot.yml, runs `python csfloat_dashboard.py
       snapshot` every 15 minutes on GitHub's own servers — nothing to do
       with whether this PC is on. Each run appends new rows to
       price_history.csv and commits/pushes them. This file is git-tracked,
       so it's the shared, durable history.

    2. LOCALLY (supplemental): clicking "Refresh prices" in the browser
       fetches live prices right now and appends them to
       price_history.local.csv instead. That file is gitignored on purpose —
       if it were tracked, a local commit and a cloud commit could both touch
       the end of the same file and conflict on `git pull`. Keeping local
       writes in a separate file means `git pull` is always a clean
       fast-forward.

  load_history() reads both CSVs and merges them per knife before the chart
  or the BUY/WAIT signal ever see the data. The dashboard also runs `git
  pull` (see git_pull()) on every page load, on every refresh, and on a
  background timer (GIT_SYNC_INTERVAL_SECONDS) — that's what pulls in
  whatever the cloud job collected while this machine was off.

  Downstream of price_history.csv, you can also point external tools at the
  raw file on GitHub — e.g. Google Sheets' IMPORTDATA(url) can read
  https://raw.githubusercontent.com/<you>/<repo>/main/price_history.csv
  directly (repo must be public for that URL to be fetchable without auth).
  That's a read-only mirror; this script doesn't push data anywhere itself.

RUN THE DASHBOARD LOCALLY
  export CSFLOAT_API_KEY="your-key"
  python3 csfloat_dashboard.py            # starts the app, open the URL it prints

SET UP THE CLOUD COLLECTION (recommended — this is what makes the history
continuous instead of full of gaps)
  1. Push this folder to a GitHub repo. Make it public if you also want to
     read price_history.csv from Google Sheets via IMPORTDATA (see above);
     private is fine if you only care about the local dashboard.
  2. In the repo's Settings -> Secrets and variables -> Actions, add a secret
     named CSFLOAT_API_KEY with your key. The workflow can't fetch prices
     without it.
  3. The included .github/workflows/snapshot.yml runs automatically once
     it's pushed — nothing to run locally for this part. You can also
     trigger it on demand from the repo's Actions tab ("Run workflow").
  4. On any machine where you want the dashboard, `git clone` the repo and
     run this script normally (RUN THE DASHBOARD LOCALLY above); it pulls
     the cloud-collected history automatically.

If you'd rather stay fully local/manual, that still works — just skip the
GitHub Actions setup. You'll only get data points from whenever you had the
dashboard open and clicked "Refresh prices", so history will have gaps.
"""

import csv
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

API_KEY = os.environ.get("CSFLOAT_API_KEY", "PASTE_YOUR_KEY_HERE")
PORT = 8000
HISTORY_FILE = "price_history.csv"              # git-tracked; written by the GitHub Actions job
LOCAL_HISTORY_FILE = "price_history.local.csv"  # gitignored; written by local "Refresh prices" clicks
PAGE_LIMIT = 50            # listings sampled per knife (max 50)

BUY_BELOW = 0.25          # position <= this -> BUY-ish
WAIT_ABOVE = 0.75         # position >= this -> WAIT

DEFAULT_WINDOW_DAYS = 14   # default trend window shown in the chart
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 90

GIT_SYNC_INTERVAL_SECONDS = 300   # background `git pull` cadence while the server is open

VANILLA_KNIVES = [
    "★ Bayonet", "★ Bowie Knife", "★ Butterfly Knife", "★ Classic Knife",
    "★ Falchion Knife", "★ Flip Knife", "★ Gut Knife", "★ Huntsman Knife",
    "★ Karambit", "★ Kukri Knife", "★ M9 Bayonet", "★ Navaja Knife",
    "★ Nomad Knife", "★ Paracord Knife", "★ Shadow Daggers", "★ Skeleton Knife",
    "★ Stiletto Knife", "★ Survival Knife", "★ Talon Knife", "★ Ursus Knife",
]

# Skinned items need the exact pattern + condition in the market_hash_name
# (unlike the vanilla knives above, which cover every float in one lookup).
SKINNED_KNIVES = [
    "★ Butterfly Knife | Safari Mesh (Field-Tested)",
    "★ Butterfly Knife | Urban Masked (Field-Tested)",
]

GLOVES = [
    "★ Moto Gloves | Polygon (Field-Tested)",
    "★ Driver Gloves | Queen Jaguar (Field-Tested)",
    "★ Moto Gloves | Transport (Field-Tested)",
    "★ Driver Gloves | King Snake (Field-Tested)",
    "★ Driver Gloves | Brocade Flowers (Field-Tested)",
]

# Order here is the default card order (before the on-page sort dropdown is
# touched) — skinned knives are listed above gloves.
TRACKED_ITEMS = VANILLA_KNIVES + SKINNED_KNIVES + GLOVES

API_URL = "https://csfloat.com/api/v1/listings"
_lock = threading.Lock()   # guards CSV writes

# ─────────────────────────────────────────────────────────────────────────────
# DATA COLLECTION
# ─────────────────────────────────────────────────────────────────────────────

def fetch_stats(name):
    """Return (lowest_cents, median_cents, count) for a knife, or None."""
    # type=buy_now excludes auction listings, whose "price" is just the
    # current bid/reserve rather than a real sale price and skews lows down.
    params = {"market_hash_name": name, "sort_by": "lowest_price",
              "type": "buy_now", "limit": PAGE_LIMIT}
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    if API_KEY and API_KEY != "PASTE_YOUR_KEY_HERE":
        req.add_header("Authorization", API_KEY)
    req.add_header("User-Agent", "csfloat-dashboard/1.0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    prices = [l["price"] for l in data if l.get("price")]
    if not prices:
        return None
    return min(prices), int(statistics.median(prices)), len(prices)


def snapshot(verbose=True, target_file=HISTORY_FILE):
    """Fetch every knife once and append the results to a history CSV.

    `target_file` defaults to the cloud/git-tracked file (used by the CI cron
    job). Local on-demand refreshes should pass LOCAL_HISTORY_FILE instead so
    they never collide with `git pull`.
    """
    stamp = datetime.now().isoformat(timespec="seconds")
    rows = []
    for name in TRACKED_ITEMS:
        try:
            result = fetch_stats(name)
        except Exception as e:
            if verbose:
                print(f"  {name}: error {e}")
            continue
        if result:
            rows.append([stamp, name, *result])
            if verbose:
                print(f"  {name}: ${result[0]/100:,.2f}")
        time.sleep(1)  # be polite to the API
    with _lock:
        new_file = not os.path.exists(target_file)
        with open(target_file, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["timestamp", "name", "lowest_cents",
                            "median_cents", "count"])
            w.writerows(rows)
    return len(rows)


def _read_csv_points(path):
    points = {}
    if not os.path.exists(path):
        return points
    with _lock, open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                points.setdefault(row["name"], []).append((
                    row["timestamp"], int(row["lowest_cents"]),
                    int(row["median_cents"]), int(row["count"])))
            except (KeyError, ValueError):
                continue
    return points


def load_history():
    """Merge the cloud history and this machine's local-only history."""
    history = {}
    for path in (HISTORY_FILE, LOCAL_HISTORY_FILE):
        for name, pts in _read_csv_points(path).items():
            history.setdefault(name, []).extend(pts)
    for name in history:
        # de-dupe exact (timestamp, name) rows in case a point exists in both files
        history[name] = sorted(set(history[name]), key=lambda r: r[0])
    return history


def window_points(points, days):
    if not points:
        return points
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    windowed = [p for p in points if p[0] >= cutoff]
    # if the window is too tight to have any data yet, fall back to everything
    return windowed if windowed else points


def git_pull():
    """Best-effort sync with the repo so cloud-collected snapshots show up
    locally. Silently no-ops if this isn't a git checkout or there's no
    network — this is a convenience, not a requirement."""
    try:
        if not os.path.isdir(os.path.join(os.path.dirname(os.path.abspath(__file__)) or ".", ".git")):
            return "not a git checkout"
        result = subprocess.run(
            ["git", "pull", "--ff-only", "--quiet"],
            cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return (result.stderr or result.stdout or "git pull failed").strip()[:200]
        return None
    except Exception as e:
        return str(e)[:200]


def _auto_sync_loop():
    while True:
        time.sleep(GIT_SYNC_INTERVAL_SECONDS)
        git_pull()


def analyze_knife(points):
    lows = [p[1] for p in points]
    current, lo, hi = lows[-1], min(lows), max(lows)
    position = (current - lo) / (hi - lo) if hi > lo else None
    if position is None or len(lows) < 3:
        signal, reason = "NEED DATA", "Not enough history yet."
    elif position <= BUY_BELOW:
        signal, reason = "BUY-ish", "Near the low end of its logged range."
    elif position >= WAIT_ABOVE:
        signal, reason = "WAIT", "Near the high end of its logged range."
    else:
        signal, reason = "NEUTRAL", "Mid-range versus recent history."
    return {
        "current": current, "min": lo, "max": hi,
        "position": position, "signal": signal, "reason": reason,
        "pct_change": ((current - lows[0]) / lows[0] * 100) if lows[0] else 0,
    }


def build_payload(days=DEFAULT_WINDOW_DAYS, sync_error=None):
    """Assemble the JSON the dashboard renders, windowed to the last N days."""
    history = load_history()
    knives = []
    for name in TRACKED_ITEMS:
        pts = window_points(history.get(name), days)
        if not pts:
            continue
        s = analyze_knife(pts)
        knives.append({
            "name": name,
            "labels": [p[0][5:16].replace("T", " ") for p in pts],
            "lows": [round(p[1] / 100, 2) for p in pts],
            "medians": [round(p[2] / 100, 2) for p in pts],
            "current": round(s["current"] / 100, 2),
            "min": round(s["min"] / 100, 2),
            "max": round(s["max"] / 100, 2),
            "pct_change": round(s["pct_change"], 1),
            "position": None if s["position"] is None else round(s["position"] * 100),
            "signal": s["signal"], "reason": s["reason"],
        })
    last = max((k["labels"][-1] for k in knives), default=None)
    return {"knives": knives, "updated": last, "days": days,
            "have_key": API_KEY != "PASTE_YOUR_KEY_HERE",
            "sync_error": sync_error}

# ─────────────────────────────────────────────────────────────────────────────
# WEB SERVER
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _days_param(self, query):
        try:
            days = int(urllib.parse.parse_qs(query).get("days", [DEFAULT_WINDOW_DAYS])[0])
        except (TypeError, ValueError):
            days = DEFAULT_WINDOW_DAYS
        return max(MIN_WINDOW_DAYS, min(MAX_WINDOW_DAYS, days))

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/" or parsed.path.startswith("/index"):
            self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
        elif parsed.path == "/api/data":
            days = self._days_param(parsed.query)
            sync_error = git_pull()
            self._send(200, json.dumps(build_payload(days, sync_error)))
        elif parsed.path == "/api/refresh":
            days = self._days_param(parsed.query)
            try:
                n = snapshot(verbose=False, target_file=LOCAL_HISTORY_FILE)
                sync_error = git_pull()
                payload = build_payload(days, sync_error)
                payload["refreshed"] = n
                self._send(200, json.dumps(payload))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def serve():
    if API_KEY == "PASTE_YOUR_KEY_HERE":
        print("WARNING: no API key set. Set CSFLOAT_API_KEY so live refresh works.\n")
    # Kick off one snapshot in the background so data is fresh shortly after
    # start. Goes to the local-only file, same as clicking "Refresh prices".
    if API_KEY != "PASTE_YOUR_KEY_HERE":
        threading.Thread(target=lambda: snapshot(verbose=False, target_file=LOCAL_HISTORY_FILE),
                          daemon=True).start()
    threading.Thread(target=_auto_sync_loop, daemon=True).start()
    url = f"http://localhost:{PORT}"
    print(f"CSFloat knife dashboard running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vanilla Knife Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root{--bg:#0f1115;--card:#181b21;--line:#262b34;--muted:#8a90a0;--text:#e6e6e6}
  *{box-sizing:border-box}
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);
       color:var(--text);margin:0;padding:24px}
  header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:6px}
  h1{font-weight:600;font-size:22px;margin:0}
  button{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:8px 14px;
         font-size:14px;cursor:pointer}
  button:disabled{opacity:.6;cursor:default}
  select{background:var(--card);color:var(--text);border:1px solid var(--line);
         border-radius:8px;padding:7px 10px;font-size:13px}
  .sub{color:var(--muted);font-size:13px;margin:2px 0 18px;max-width:820px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card h3{margin:0 0 6px;font-size:15px}
  .row{display:flex;justify-content:space-between;font-size:13px;color:#c2c7d0;margin:2px 0}
  .price{font-size:20px;font-weight:700}
  .badge{display:inline-block;padding:2px 9px;border-radius:6px;font-size:12px;font-weight:600}
  .b-BUY{background:#123d1e;color:#5fe08a}
  .b-WAIT{background:#3d1a12;color:#ff9a7a}
  .b-NEUTRAL{background:#2a2f38;color:#c2c7d0}
  .b-NEED{background:#2a2f38;color:#8a90a0}
  canvas{margin-top:10px}
  .empty{color:var(--muted);padding:40px 0}
  .warn{color:#ff9a7a}
</style></head><body>
<header>
  <h1>Vanilla Knife Dashboard</h1>
  <button id="refresh" onclick="refresh()">Refresh prices</button>
  <select id="window" onchange="onWindowChange()">
    <option value="7">Last 7 days</option>
    <option value="14" selected>Last 14 days</option>
    <option value="30">Last 30 days</option>
  </select>
  <select id="sort" onchange="render()">
    <option value="signal">Sort: buy candidates first</option>
    <option value="name">Sort: name</option>
    <option value="price">Sort: price (high→low)</option>
    <option value="change">Sort: biggest drop</option>
  </select>
  <span id="status" class="sub" style="margin:0"></span>
</header>
<div class="sub">Cheapest CSFloat listing per tracked item, windowed to the
selected range. "Refresh" pulls live prices on this machine and syncs with
any data collected in the cloud (see the GitHub Actions job) so trends keep
building even while your PC is off. Green = near its recent low, red = near
its recent high. Market data for your own decisions, not financial advice.</div>
<div id="grid" class="grid"></div>
<script>
let DATA = {knives:[]};
const charts = {};
const POLL_MS = 5 * 60 * 1000; // pick up cloud-collected data without a live refresh

async function load(url){
  const r = await fetch(url); return r.json();
}
function currentWindow(){ return document.getElementById('window').value; }

async function init(){
  setStatus("Loading…");
  await loadData();
  setInterval(loadData, POLL_MS);
}
async function loadData(){
  DATA = await load('/api/data?days=' + currentWindow());
  render(); updated();
}
async function onWindowChange(){
  setStatus("Loading…");
  await loadData();
}
async function refresh(){
  const btn = document.getElementById('refresh');
  btn.disabled = true; btn.textContent = 'Refreshing… (~20s)';
  setStatus("Fetching live prices from CSFloat…");
  try{
    const d = await load('/api/refresh?days=' + currentWindow());
    if(d.error){ setStatus("Error: " + d.error); }
    else { DATA = d; render(); updated(); }
  }catch(e){ setStatus("Error: " + e); }
  btn.disabled = false; btn.textContent = 'Refresh prices';
}
function updated(){
  let msg = DATA.updated ? ("Latest data: " + DATA.updated) : "No data yet";
  if(DATA.sync_error) msg += ' — <span class="warn">cloud sync: ' + DATA.sync_error + '</span>';
  document.getElementById('status').innerHTML = msg;
}
function setStatus(t){ document.getElementById('status').textContent = t; }

function sorted(){
  const k = [...DATA.knives];
  const mode = document.getElementById('sort').value;
  const rank = s => ({'BUY-ish':0,'NEUTRAL':1,'NEED DATA':2,'WAIT':3}[s] ?? 1);
  if(mode==='signal') k.sort((a,b)=> rank(a.signal)-rank(b.signal) ||
                                     (a.position??50)-(b.position??50));
  else if(mode==='name') k.sort((a,b)=> a.name.localeCompare(b.name));
  else if(mode==='price') k.sort((a,b)=> b.current-a.current);
  else if(mode==='change') k.sort((a,b)=> a.pct_change-b.pct_change);
  return k;
}

function render(){
  const grid = document.getElementById('grid');
  Object.values(charts).forEach(c=>c.destroy());
  grid.innerHTML = '';
  if(!DATA.knives.length){
    grid.innerHTML = '<div class="empty">No data yet in this window. Click '
      + '"Refresh prices" or widen the window above.</div>';
    return;
  }
  for(const d of sorted()){
    const cls = d.signal==='BUY-ish'?'b-BUY':d.signal==='WAIT'?'b-WAIT':
                d.signal==='NEED DATA'?'b-NEED':'b-NEUTRAL';
    const pos = d.position===null ? '–' : d.position + '% of range';
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <h3>${d.name}</h3>
      <div class="row"><span class="price">$${d.current.toLocaleString()}</span>
        <span class="badge ${cls}">${d.signal}</span></div>
      <div class="row"><span>Logged range</span>
        <span>$${d.min.toLocaleString()} – $${d.max.toLocaleString()}</span></div>
      <div class="row"><span>Change (${DATA.days}d)</span>
        <span>${d.pct_change>=0?'+':''}${d.pct_change}%</span></div>
      <div class="row"><span>Position</span><span>${pos}</span></div>
      <canvas height="140"></canvas>`;
    grid.appendChild(card);
    const ctx = card.querySelector('canvas');
    charts[d.name] = new Chart(ctx, {
      type:'line',
      data:{labels:d.labels, datasets:[
        {label:'Cheapest',data:d.lows,borderColor:'#5fa8ff',
         backgroundColor:'rgba(95,168,255,.12)',fill:true,tension:.25,
         pointRadius:0,borderWidth:2},
        {label:'Median',data:d.medians,borderColor:'#8a90a0',borderDash:[4,4],
         fill:false,tension:.25,pointRadius:0,borderWidth:1}]},
      options:{plugins:{legend:{labels:{color:'#c2c7d0',boxWidth:10,font:{size:10}}}},
        scales:{x:{ticks:{color:'#6b7280',maxTicksLimit:5,font:{size:9}},
                   grid:{display:false}},
                y:{ticks:{color:'#6b7280',font:{size:9},
                   callback:v=>'$'+v.toLocaleString()},grid:{color:'#20242c'}}}}
    });
  }
}
init();
</script></body></html>"""

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Windows consoles default to cp1252, which can't print the ★ in knife names.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "snapshot":
        # Writes to the cloud/git-tracked file. This is what
        # .github/workflows/snapshot.yml runs on its 15-minute schedule; you
        # can also run it by hand to add a point to the shared history.
        print(f"Snapshotting {len(TRACKED_ITEMS)} items...")
        n = snapshot(target_file=HISTORY_FILE)
        print(f"Saved {n} rows to {HISTORY_FILE}")
    else:
        serve()
