#!/usr/bin/env python3
"""
CSFloat Vanilla Knife Dashboard
===============================

A small LOCAL web app to watch vanilla knife prices on CSFloat. Zero installs —
it uses Python's built-in web server, and the charts render in your browser.

WHAT IT DOES
  - Serves a dashboard at http://localhost:8000
  - Shows current cheapest price + a trend chart for each tracked item —
    every skin of every knife/glove type except EXCLUDED_*_TYPES (see
    get_tracked_items()) — windowed to the last 7/14/30 days so you can see
    the recent price drop-off
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
       snapshot` once an hour on GitHub's own servers — nothing to do with
       whether this PC is on. Each run appends new rows to price_history.csv
       and commits/pushes them. This file is git-tracked, so it's the
       shared, durable history. Hourly (not more often) because
       get_tracked_items() now covers ~463 items and snapshot() throttles
       to ~1 request/sec to stay polite to CSFloat, so a full run takes
       roughly 10-13 minutes — too long to safely overlap a 15-minute
       schedule. The workflow also has a concurrency guard so a slow run
       can never overlap the next one regardless.

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
# Render (and most hosts) inject PORT and expect a bind on 0.0.0.0; running
# locally there's no PORT env var, so it falls back to 127.0.0.1:8000.
PORT = int(os.environ.get("PORT", 8000))
HOST = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
HISTORY_FILE = "price_history.csv"              # git-tracked; written by the GitHub Actions job
LOCAL_HISTORY_FILE = "price_history.local.csv"  # gitignored; written by local "Refresh prices" clicks
PAGE_LIMIT = 50            # listings sampled per knife (max 50)

BUY_BELOW = 0.25          # position <= this -> BUY-ish
WAIT_ABOVE = 0.75         # position >= this -> WAIT

DEFAULT_WINDOW_DAYS = 14   # default trend window shown in the chart
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 90

GIT_SYNC_INTERVAL_SECONDS = 300   # background `git pull` cadence while the server is open

# What actually gets tracked: every skin of every knife/glove TYPE except
# these, one representative wear each (see representative_wear()). Derived
# from the catalog at runtime (see get_tracked_items()) rather than hardcoded
# — 463 items as of the exclusions below, too many to hand-maintain as a
# literal list. Edit these sets, not a tracked-items list, to change scope.
EXCLUDED_KNIFE_TYPES = {
    "Bowie Knife", "Classic Knife", "Kukri Knife", "Navaja Knife",
    "Paracord Knife", "Survival Knife", "Ursus Knife",
}
EXCLUDED_GLOVE_TYPES = {
    "Bloodhound Gloves", "Hydra Gloves", "Specialist Gloves", "Sport Gloves",
}

API_URL = "https://csfloat.com/api/v1/listings"
_lock = threading.Lock()   # guards CSV writes

# CSFloat has no browse/catalog endpoint (only exact-name price lookups), so
# the "what skins exist for this weapon, with images" data comes from a free,
# open, no-auth CS2 item dataset instead — see build_catalog_index().
CATALOG_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
CATALOG_FILE = "catalog.json"   # gitignored: a regenerable cache, unlike price_history.csv
CATALOG_MAX_AGE_DAYS = 7
CATALOG_CATEGORIES = {"Knives", "Gloves"}

# (label, min_float, max_float) — the 5 canonical CS2 wear breakpoints, used
# to figure out which wears actually exist for a given skin's float range.
WEAR_RANGES = [
    ("Factory New", 0.00, 0.07),
    ("Minimal Wear", 0.07, 0.15),
    ("Field-Tested", 0.15, 0.38),
    ("Well-Worn", 0.38, 0.45),
    ("Battle-Scarred", 0.45, 1.00),
]

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
    for name in get_tracked_items():
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
    """days=None means no cutoff — return every logged point ("All")."""
    if not points or days is None:
        return points
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    windowed = [p for p in points if p[0] >= cutoff]
    # if the window is too tight to have any data yet, fall back to everything
    return windowed if windowed else points


def git_pull():
    """Best-effort sync with the repo so cloud-collected snapshots show up
    locally. Silently no-ops if this isn't a git checkout or there's no
    network — this is a convenience, not a requirement.

    Only meaningful for a normal local clone tracking a branch. On a hosting
    platform (Render etc.) the checkout is a detached-HEAD snapshot of one
    commit with nothing to "pull" into, and the platform already redeploys
    the whole app on every new commit anyway — so skip it there entirely.
    """
    if HOST != "127.0.0.1":
        return None
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


# Maps the detail page's period buttons onto window_points' day cutoffs.
# "all" -> None means no cutoff at all (see window_points).
PERIOD_DAYS = {"7d": 7, "1m": 30, "3m": 90, "6m": 180, "1y": 365, "all": None}


def _item_summary(name, pts):
    """One item's chart/stat payload for a given (already-windowed) points
    list. Shared by build_payload (loops get_tracked_items()) and
    item_history_payload (one arbitrary name, for the browse detail page)."""
    s = analyze_knife(pts)
    return {
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
    }


def build_payload(days=DEFAULT_WINDOW_DAYS, sync_error=None):
    """Assemble the JSON the dashboard renders, windowed to the last N days."""
    history = load_history()
    knives = []
    for name in get_tracked_items():
        pts = window_points(history.get(name), days)
        if not pts:
            continue
        knives.append(_item_summary(name, pts))
    last = max((k["labels"][-1] for k in knives), default=None)
    return {"knives": knives, "updated": last, "days": days,
            "have_key": API_KEY != "PASTE_YOUR_KEY_HERE",
            "sync_error": sync_error}


def item_history_payload(name, period):
    """Same shape as one build_payload() entry, but for any single item name
    (tracked or not) and a period key from PERIOD_DAYS, for the browse
    detail page. has_data=False means: not tracked yet, or tracked but
    nothing logged in this window."""
    days = PERIOD_DAYS.get(period, DEFAULT_WINDOW_DAYS)
    pts = window_points(load_history().get(name), days)
    if not pts:
        return {"name": name, "has_data": False}
    summary = _item_summary(name, pts)
    summary["has_data"] = True
    return summary

# ─────────────────────────────────────────────────────────────────────────────
# CATALOG (what skins exist, for the browse UI and for deriving
# get_tracked_items() — separate from price_history.csv, which is what we
# actually have price history for)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_catalog_raw():
    """Pull the full CS2 item dataset. No auth needed, unrelated to CSFloat."""
    req = urllib.request.Request(CATALOG_URL)
    req.add_header("User-Agent", "csfloat-dashboard/1.0")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def valid_wears_for(min_float, max_float):
    """Which of the 5 wear names actually exist for a skin's float range."""
    if min_float is None or max_float is None:
        return []
    return [label for label, lo, hi in WEAR_RANGES if lo < max_float and hi > min_float]


def build_catalog_index(raw):
    """Group the raw item dataset into {category: {weapon: [skin, ...]}}.

    Skips StatTrak/Souvenir entries (matches the convention every tracked
    item so far has followed — the plain/"Normal" variant).
    """
    index = {}
    for entry in raw:
        category = (entry.get("category") or {}).get("name")
        if category not in CATALOG_CATEGORIES:
            continue
        weapon = (entry.get("weapon") or {}).get("name")
        pattern = (entry.get("pattern") or {}).get("name")
        name = entry.get("name")
        if not (weapon and pattern and name):
            continue
        # The stattrak/souvenir booleans mean "this skin CAN be StatTrak"
        # (always True for knives), not "this row IS one" — separate
        # StatTrak/Souvenir rows, when they exist, carry the marker right
        # in the name instead, so that's the actual per-row signal to skip.
        if "StatTrak" in name or "Souvenir" in name:
            continue
        wears = valid_wears_for(entry.get("min_float"), entry.get("max_float"))
        index.setdefault(category, {}).setdefault(weapon, []).append({
            "pattern": pattern,
            "name": name,
            "image": entry.get("image"),
            "wears": wears,
        })
    for by_weapon in index.values():
        for skins in by_weapon.values():
            skins.sort(key=lambda s: s["pattern"])
    # The item dataset only covers actual skins, so plain "no skin" knives
    # (VANILLA_KNIVES: sold on Steam as e.g. "★ Bayonet", no wear suffix)
    # never appear in it — synthesize one Vanilla entry per knife type,
    # inserted to the front after sorting, matching the reference site's
    # own grid layout.
    for weapon in index.get("Knives", {}):
        index["Knives"][weapon].insert(0, {
            "pattern": "Vanilla",
            "name": f"★ {weapon}",
            "image": None,
            "wears": [],
        })
    return index


def load_catalog():
    if not os.path.exists(CATALOG_FILE):
        return None
    with open(CATALOG_FILE, encoding="utf-8") as f:
        return json.load(f)


def catalog_needs_refresh():
    if not os.path.exists(CATALOG_FILE):
        return True
    age_days = (time.time() - os.path.getmtime(CATALOG_FILE)) / 86400
    return age_days > CATALOG_MAX_AGE_DAYS


# In-memory view of catalog.json for the browse routes: the nested
# {category: {weapon: [skin, ...]}} index, plus a flat name -> skin lookup
# (a skin's catalog `name` is exactly the vanilla/no-wear market_hash_name,
# so this doubles as "does this name exist in the catalog at all" for
# validating input from the detail page).
_catalog_lock = threading.Lock()
_catalog_index = {}
_catalog_by_name = {}


def set_catalog(index):
    global _catalog_index, _catalog_by_name
    by_name = {}
    for category, by_weapon in index.items():
        for weapon, skins in by_weapon.items():
            for skin in skins:
                by_name[skin["name"]] = {**skin, "category": category, "weapon": weapon}
    with _catalog_lock:
        _catalog_index = index
        _catalog_by_name = by_name


def ensure_catalog_loaded():
    """Non-blocking: serve whatever's cached (even if stale) immediately,
    kick a background refresh if the cache is missing/old."""
    if not _catalog_index:
        on_disk = load_catalog()
        if on_disk:
            set_catalog(on_disk)
    if catalog_needs_refresh():
        threading.Thread(target=lambda: set_catalog(refresh_catalog(verbose=False)),
                          daemon=True).start()


def get_skin_info(name):
    return _catalog_by_name.get(name)


def market_hash_name_for(catalog_name, wear):
    """The exact CSFloat/Steam market_hash_name for a catalog entry + wear.
    Vanilla/no-skin knives (wears == []) never take a wear suffix."""
    info = get_skin_info(catalog_name)
    if not wear or not info or not info.get("wears"):
        return catalog_name
    return f"{catalog_name} ({wear})"


def representative_wear(skin):
    """One wear to track per skin (not all 5, to keep snapshot() runtime
    sane across hundreds of skins) — Field-Tested if it exists, else
    whatever's first, else None for vanilla knives. Mirrors the browse
    grid's client-side repWear()."""
    wears = skin.get("wears") or []
    if not wears:
        return None
    return "Field-Tested" if "Field-Tested" in wears else wears[0]


def build_tracked_items(index):
    """Every skin of every knife/glove type except EXCLUDED_*_TYPES, at its
    representative_wear()."""
    items = []
    for category, excluded in (("Knives", EXCLUDED_KNIFE_TYPES),
                                ("Gloves", EXCLUDED_GLOVE_TYPES)):
        for weapon, skins in index.get(category, {}).items():
            if weapon in excluded:
                continue
            for skin in skins:
                wear = representative_wear(skin)
                items.append(f"{skin['name']} ({wear})" if wear else skin["name"])
    return items


_tracked_items_cache = None


def get_tracked_items():
    """The list snapshot()/build_payload() actually iterate — derived from
    the catalog rather than hardcoded (see EXCLUDED_*_TYPES above). Loads/
    fetches the catalog synchronously if nothing's cached yet (unlike
    ensure_catalog_loaded()'s background-thread refresh, this needs the
    real list before snapshot() can run at all) and memoizes the result for
    the life of the process."""
    global _tracked_items_cache
    if _tracked_items_cache is not None:
        return _tracked_items_cache
    if not _catalog_index:
        on_disk = load_catalog()
        set_catalog(on_disk if on_disk else refresh_catalog(verbose=False))
    _tracked_items_cache = build_tracked_items(_catalog_index)
    return _tracked_items_cache


# Bounds concurrent CSFloat calls from the browse grid's per-card price
# fetches — those come from many simultaneous browser requests, unlike
# snapshot()'s self-throttled (1 req/sec) sequential loop.
_price_fetch_semaphore = threading.Semaphore(3)


def refresh_catalog(verbose=True):
    """Fetch the item dataset and rebuild catalog.json. Safe to run anytime —
    this is a cache of third-party reference data, not collected history."""
    if verbose:
        print(f"Fetching catalog from {CATALOG_URL} ...")
    raw = fetch_catalog_raw()
    index = build_catalog_index(raw)
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f)
    if verbose:
        for category, by_weapon in sorted(index.items()):
            total = sum(len(skins) for skins in by_weapon.values())
            print(f"  {category}: {len(by_weapon)} types, {total} skins")
    return index

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

    def _param(self, query, key, default=""):
        return urllib.parse.parse_qs(query).get(key, [default])[0]

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, parsed.query

        if path == "/" or path.startswith("/index"):
            self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
        elif path == "/browse":
            self._send(200, BROWSE_HTML, "text/html; charset=utf-8")
        elif path == "/browse/item":
            self._send(200, BROWSE_ITEM_HTML, "text/html; charset=utf-8")
        elif path == "/api/data":
            days = self._days_param(query)
            sync_error = git_pull()
            self._send(200, json.dumps(build_payload(days, sync_error)))
        elif path == "/api/refresh":
            days = self._days_param(query)
            try:
                n = snapshot(verbose=False, target_file=LOCAL_HISTORY_FILE)
                sync_error = git_pull()
                payload = build_payload(days, sync_error)
                payload["refreshed"] = n
                self._send(200, json.dumps(payload))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}))
        elif path == "/api/catalog/categories":
            ensure_catalog_loaded()
            self._send(200, json.dumps({"categories": sorted(_catalog_index.keys())}))
        elif path == "/api/catalog/types":
            ensure_catalog_loaded()
            category = self._param(query, "category")
            types = sorted(_catalog_index.get(category, {}).keys())
            self._send(200, json.dumps({"types": types}))
        elif path == "/api/catalog/skins":
            ensure_catalog_loaded()
            category = self._param(query, "category")
            weapon = self._param(query, "type")
            skins = _catalog_index.get(category, {}).get(weapon, [])
            self._send(200, json.dumps({"skins": skins}))
        elif path == "/api/item_wears":
            ensure_catalog_loaded()
            name = self._param(query, "name")
            info = get_skin_info(name)
            self._send(200, json.dumps({"wears": (info or {}).get("wears", [])}))
        elif path == "/api/item_price":
            name = self._param(query, "name")
            wear = self._param(query, "wear")
            full_name = market_hash_name_for(name, wear)
            try:
                with _price_fetch_semaphore:
                    result = fetch_stats(full_name)
                if result is None:
                    self._send(200, json.dumps({"error": "no live listings found"}))
                else:
                    lowest, median, count = result
                    self._send(200, json.dumps({
                        "price": round(lowest / 100, 2),
                        "median": round(median / 100, 2),
                        "count": count,
                    }))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}))
        elif path == "/api/history":
            name = self._param(query, "name")
            wear = self._param(query, "wear")
            period = self._param(query, "period", "1m")
            full_name = market_hash_name_for(name, wear)
            self._send(200, json.dumps(item_history_payload(full_name, period)))
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
    ensure_catalog_loaded()   # loads cached catalog.json instantly; refreshes in the background if stale/missing
    print(f"CSFloat knife dashboard listening on {HOST}:{PORT}")
    if HOST == "127.0.0.1":
        url = f"http://localhost:{PORT}"
        print(f"Open {url}")
        print("Press Ctrl+C to stop.")
        try:
            webbrowser.open(url)
        except Exception:
            pass
    server = ThreadingHTTPServer((HOST, PORT), Handler)
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
  nav{margin-bottom:14px}
  nav a{color:var(--muted);text-decoration:none;font-size:13px;margin-right:16px}
  nav a.active{color:var(--text);font-weight:600}
</style></head><body>
<nav><a class="active" href="/">My Tracked Items</a><a href="/browse">Browse Catalog</a></nav>
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
# BROWSE HTML (Phase 2/3: catalog tabs -> type dropdown -> skin grid -> detail page)
# ─────────────────────────────────────────────────────────────────────────────

# Shared with BROWSE_ITEM_HTML below — kept as one constant so both pages
# look identical without copy-pasting the whole <style> block twice.
BROWSE_STYLE = r"""
  :root{--bg:#0f1115;--card:#181b21;--line:#262b34;--muted:#8a90a0;--text:#e6e6e6}
  *{box-sizing:border-box}
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);
       color:var(--text);margin:0;padding:24px}
  nav{margin-bottom:14px}
  nav a{color:var(--muted);text-decoration:none;font-size:13px;margin-right:16px}
  nav a.active{color:var(--text);font-weight:600}
  h1{font-weight:600;font-size:22px;margin:0 0 14px}
  .tabs{display:flex;gap:8px;margin-bottom:14px}
  .tab{background:var(--card);color:var(--muted);border:1px solid var(--line);
       border-radius:8px;padding:7px 16px;font-size:13px;cursor:pointer}
  .tab.active{color:var(--text);border-color:#2563eb;background:#152036}
  select{background:var(--card);color:var(--text);border:1px solid var(--line);
         border-radius:8px;padding:7px 10px;font-size:13px;margin-bottom:18px;min-width:220px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:14px;cursor:pointer;text-decoration:none;color:inherit;display:block}
  .card:hover{border-color:#3b4252}
  .card img{width:100%;height:110px;object-fit:contain;margin-bottom:8px}
  .card .ph{width:100%;height:110px;margin-bottom:8px;display:flex;align-items:center;
            justify-content:center;color:var(--muted);font-size:12px;background:#12151b;border-radius:8px}
  .card .pattern{font-size:14px;font-weight:600;margin-bottom:4px}
  .card .price{font-size:15px;color:#c2c7d0}
  .card .price.pending{color:var(--muted)}
  .empty,.loading{color:var(--muted);padding:40px 0}
"""

BROWSE_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Browse Catalog</title>
<style>""" + BROWSE_STYLE + r"""</style></head><body>
<nav><a href="/">My Tracked Items</a><a class="active" href="/browse">Browse Catalog</a></nav>
<h1>Browse Catalog</h1>
<div class="tabs" id="tabs"></div>
<select id="typeSelect"></select>
<div id="grid" class="grid"><div class="loading">Loading…</div></div>
<script>
const params = new URLSearchParams(location.search);
let category = params.get('category') || 'Knives';
let type = params.get('type') || '';

async function load(url){ const r = await fetch(url); return r.json(); }

function repWear(skin){
  if(!skin.wears || !skin.wears.length) return '';
  return skin.wears.includes('Field-Tested') ? 'Field-Tested' : skin.wears[0];
}

async function runQueue(items, worker, concurrency){
  let i = 0;
  async function next(){
    if(i >= items.length) return;
    const idx = i++;
    try{ await worker(items[idx], idx); }catch(e){}
    return next();
  }
  await Promise.all(Array.from({length: Math.min(concurrency, items.length)}, next));
}

async function initTabs(){
  const {categories} = await load('/api/catalog/categories');
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = categories.map(c =>
    `<div class="tab${c===category?' active':''}" data-cat="${c}">${c}</div>`).join('');
  tabs.querySelectorAll('.tab').forEach(el => el.onclick = () => {
    location.href = '/browse?category=' + encodeURIComponent(el.dataset.cat);
  });
}

async function initTypes(){
  const {types} = await load('/api/catalog/types?category=' + encodeURIComponent(category));
  if(!type) type = types[0] || '';
  const sel = document.getElementById('typeSelect');
  sel.innerHTML = types.map(t =>
    `<option value="${t}"${t===type?' selected':''}>${t}</option>`).join('');
  sel.onchange = () => {
    location.href = '/browse?category=' + encodeURIComponent(category) +
      '&type=' + encodeURIComponent(sel.value);
  };
}

async function loadGrid(){
  const grid = document.getElementById('grid');
  if(!type){ grid.innerHTML = '<div class="empty">No types found.</div>'; return; }
  const {skins} = await load('/api/catalog/skins?category=' + encodeURIComponent(category) +
    '&type=' + encodeURIComponent(type));
  if(!skins.length){ grid.innerHTML = '<div class="empty">No skins found for this type.</div>'; return; }
  grid.innerHTML = skins.map((s, i) => `
    <a class="card" data-i="${i}" href="/browse/item?name=${encodeURIComponent(s.name)}">
      ${s.image ? `<img src="${s.image}" loading="lazy">` : '<div class="ph">No image</div>'}
      <div class="pattern">${s.pattern}</div>
      <div class="price pending" id="price-${i}">Loading price…</div>
    </a>`).join('');
  runQueue(skins, async (s, i) => {
    const wear = repWear(s);
    const d = await load('/api/item_price?name=' + encodeURIComponent(s.name) +
      '&wear=' + encodeURIComponent(wear));
    const el = document.getElementById('price-' + i);
    if(!el) return;
    if(d.error){ el.textContent = 'No listings'; }
    else { el.textContent = '$' + d.price.toLocaleString() + (wear ? ' (' + wear + ')' : ''); el.classList.remove('pending'); }
  }, 3);
}

(async function init(){
  await initTabs();
  await initTypes();
  await loadGrid();
})();
</script></body></html>"""

BROWSE_ITEM_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Item Detail</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>""" + BROWSE_STYLE + r"""
  .back{color:var(--muted);text-decoration:none;font-size:13px;display:inline-block;margin-bottom:10px}
  .detail{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:18px}
  .detail img{width:220px;height:160px;object-fit:contain;background:var(--card);
              border:1px solid var(--line);border-radius:12px;padding:12px}
  .pills{display:flex;gap:6px;margin:10px 0;flex-wrap:wrap}
  .pill{background:var(--card);color:var(--muted);border:1px solid var(--line);
        border-radius:6px;padding:5px 11px;font-size:12px;cursor:pointer}
  .pill.active{color:var(--text);border-color:#2563eb;background:#152036}
  .periods{display:flex;gap:6px;margin:14px 0 10px}
  .price{font-size:28px;font-weight:700}
  .stats{display:flex;gap:24px;flex-wrap:wrap;margin:12px 0;font-size:13px;color:#c2c7d0}
  .stats b{color:var(--text)}
  .note{color:var(--muted);font-size:13px;margin-top:10px}
</style></head><body>
<nav><a href="/">My Tracked Items</a><a class="active" href="/browse">Browse Catalog</a></nav>
<a class="back" href="#" onclick="history.back();return false;">&larr; Back</a>
<div id="content" class="loading">Loading…</div>
<script>
const params = new URLSearchParams(location.search);
const catalogName = params.get('name') || '';
let wear = '';
let period = '1m';
let chart = null;

async function load(url){ const r = await fetch(url); return r.json(); }

function pillsHTML(wears){
  if(!wears.length) return '';
  return '<div class="pills">' + wears.map(w =>
    `<div class="pill${w===wear?' active':''}" data-w="${w}">${w}</div>`).join('') + '</div>';
}

function periodsHTML(){
  const opts = [['7d','7D'],['1m','1M'],['3m','3M'],['6m','6M'],['1y','1Y'],['all','All']];
  return '<div class="periods">' + opts.map(([k,l]) =>
    `<div class="pill${k===period?' active':''}" data-p="${k}">${l}</div>`).join('') + '</div>';
}

async function render(){
  const el = document.getElementById('content');
  const priceP = load('/api/item_price?name=' + encodeURIComponent(catalogName) + '&wear=' + encodeURIComponent(wear));
  const histP = load('/api/history?name=' + encodeURIComponent(catalogName) + '&wear=' + encodeURIComponent(wear) + '&period=' + period);
  const [priceData, hist] = await Promise.all([priceP, histP]);

  el.innerHTML = `
    <div class="detail">
      <div>
        <h1>${catalogName}</h1>
        ${window.__wears && window.__wears.length ? pillsHTML(window.__wears) : ''}
        <div class="price">${priceData.error ? 'No live listings' : '$' + priceData.price.toLocaleString()}</div>
        ${periodsHTML()}
      </div>
    </div>
    <div class="stats" id="stats"></div>
    <canvas id="chart" height="90"></canvas>
    <div class="note" id="note"></div>
    <canvas id="hidden" style="display:none"></canvas>
  `;

  document.querySelectorAll('.pill[data-w]').forEach(p => p.onclick = () => { wear = p.dataset.w; render(); });
  document.querySelectorAll('.pill[data-p]').forEach(p => p.onclick = () => { period = p.dataset.p; render(); });

  const stats = document.getElementById('stats');
  const note = document.getElementById('note');
  if(!hist.has_data){
    stats.innerHTML = '';
    note.textContent = 'No price history logged yet for this item/wear — it starts being tracked ' +
      'automatically now that you\'ve viewed it, so a trend will build up over the next few days.';
    if(chart){ chart.destroy(); chart = null; }
    return;
  }
  note.textContent = '';
  stats.innerHTML = `
    <span>All-time low <b>$${hist.min.toLocaleString()}</b></span>
    <span>All-time high <b>$${hist.max.toLocaleString()}</b></span>
    <span>Change (${period}) <b>${hist.pct_change>=0?'+':''}${hist.pct_change}%</b></span>
    <span>Signal <b>${hist.signal}</b></span>
  `;
  if(chart) chart.destroy();
  chart = new Chart(document.getElementById('chart'), {
    type: 'line',
    data: {labels: hist.labels, datasets: [
      {label:'Cheapest', data: hist.lows, borderColor:'#5fa8ff',
       backgroundColor:'rgba(95,168,255,.12)', fill:true, tension:.25, pointRadius:0, borderWidth:2}]},
    options: {plugins:{legend:{labels:{color:'#c2c7d0',boxWidth:10,font:{size:10}}}},
      scales:{x:{ticks:{color:'#6b7280',maxTicksLimit:6,font:{size:9}},grid:{display:false}},
              y:{ticks:{color:'#6b7280',font:{size:9},callback:v=>'$'+v.toLocaleString()},grid:{color:'#20242c'}}}}
  });
}

// Resolve this catalog item's valid wears once up front (needed for the
// pill row before the first render), then render.
(async function init(){
  if(!catalogName){ document.getElementById('content').innerHTML = '<div class="empty">No item specified.</div>'; return; }
  const info = await load('/api/item_wears?name=' + encodeURIComponent(catalogName));
  window.__wears = info.wears || [];
  if(window.__wears.length) wear = window.__wears.includes('Field-Tested') ? 'Field-Tested' : window.__wears[0];
  await render();
})();
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
        # .github/workflows/snapshot.yml runs on its hourly schedule; you
        # can also run it by hand to add a point to the shared history.
        print(f"Snapshotting {len(get_tracked_items())} items...")
        n = snapshot(target_file=HISTORY_FILE)
        print(f"Saved {n} rows to {HISTORY_FILE}")
    elif cmd == "refresh_catalog":
        refresh_catalog()
    else:
        serve()
