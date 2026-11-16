#!/usr/bin/env python3
"""
Flying Above Me - a live airport-board style tracker for aircraft near you.

Data: OpenSky Network (positions) + adsbdb (airline / route / aircraft type).

How "real time" works
---------------------
The SCREEN redraws every second. The OpenSky API cannot be polled that often
(anonymous: 400 requests/day, free account: 4,000/day, and the data itself only
updates every 5-10 s), so a background thread fetches fresh data every
API_POLL_SECONDS and, in between, each plane's position is projected forward
from its last known speed and heading. Distances and altitudes therefore tick
live every second and snap to the true position on each new fetch.

Setup:
    pip install -r requirements.txt
    Copy config.example.json to config.json and set your latitude/longitude,
    then run:  python flying_above_me.py

Optional: set OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET environment variables
(free OpenSky account -> API client) for a 10x higher daily quota.
"""

import math
import os
import sys
import threading
import time
from datetime import datetime

import requests

# ---------------- CONFIG ----------------
# Settings live in config.json next to this script (copy config.example.json).
# config.json is git-ignored so your home location never gets committed.
import json

_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_cfg = {}
if os.path.exists(_cfg_path):
    with open(_cfg_path, encoding="utf-8") as _f:
        _cfg = json.load(_f)

HOME_LAT = _cfg.get("home_lat", 40.7128)        # your latitude
HOME_LON = _cfg.get("home_lon", -74.0060)       # your longitude
RADIUS_KM = _cfg.get("radius_km", 25)           # search radius
MAX_SHOWN = _cfg.get("max_shown", 5)            # how many planes to list
OVERHEAD_KM = _cfg.get("overhead_km", 3)        # "overhead" alert distance

SCREEN_REFRESH_SECONDS = 1                      # how often the screen redraws
API_POLL_SECONDS = _cfg.get("api_poll_seconds") # None = automatic (25 s with an OpenSky login, 60 s without)
MAX_PROJECT_SECONDS = 90                        # stop projecting a plane after this long without new data
# ----------------------------------------

API_URL = "https://opensky-network.org/api/states/all"
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
ADSBDB_ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{}"
ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{}"

CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID")
CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET")
HAS_LOGIN = bool(CLIENT_ID and CLIENT_SECRET)
POLL_SECONDS = API_POLL_SECONDS or (25 if HAS_LOGIN else 60)

# Airport-board look: black background, bright yellow text, ALL CAPS.
BG_BLACK = "\033[40m"
FG_YELLOW = "\033[93m"
INVERT_ON = "\033[7m"
INVERT_OFF = "\033[27m"
HOME = "\033[H"
CLEAR = "\033[2J\033[H"
ERASE_LINE = "\033[K"
ERASE_BELOW = "\033[J"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
RESET = "\033[0m"

# Shared state, written by the background poller and read by the screen loop.
_lock = threading.Lock()
_state = {"planes": [], "fetched": None, "error": None, "next_poll": time.time()}
_route_cache = {}
_aircraft_cache = {}
_token = {"value": None, "expires": 0}


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f"Rate limited by OpenSky (retry in {retry_after}s)")
        self.retry_after = retry_after


# ---------- geometry ----------

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(deg):
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) // 45) % 8]


def bounding_box(lat, lon, radius_km):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def project(lat, lon, speed_ms, track_deg, seconds):
    """Move a point along a heading at a given speed (great-circle)."""
    if speed_ms is None or track_deg is None or seconds <= 0:
        return lat, lon
    ang = speed_ms * seconds / 6371000.0
    t = math.radians(track_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(ang) + math.cos(p1) * math.sin(ang) * math.cos(t))
    l2 = l1 + math.atan2(math.sin(t) * math.sin(ang) * math.cos(p1),
                         math.cos(ang) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


# ---------- OpenSky ----------

def get_token():
    """Fetch (and cache) an OAuth2 token if credentials are provided."""
    if not HAS_LOGIN:
        return None
    if _token["value"] and time.time() < _token["expires"] - 30:
        return _token["value"]
    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    _token["value"] = data["access_token"]
    _token["expires"] = time.time() + data.get("expires_in", 300)
    return _token["value"]


def fetch_aircraft():
    lamin, lamax, lomin, lomax = bounding_box(HOME_LAT, HOME_LON, RADIUS_KM)
    params = {"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax}
    headers = {}
    token = get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    r = requests.get(API_URL, params=params, headers=headers, timeout=15)
    got_at = time.time()
    if r.status_code == 429:
        retry = r.headers.get("X-Rate-Limit-Retry-After-Seconds", "")
        raise RateLimited(int(retry) if retry.isdigit() else 300)
    r.raise_for_status()
    body = r.json()
    server_time = body.get("time") or got_at
    states = body.get("states") or []

    planes = []
    for s in states:
        lon, lat = s[5], s[6]
        if lat is None or lon is None or s[8]:  # skip no position / on ground
            continue
        if haversine_km(HOME_LAT, HOME_LON, lat, lon) > RADIUS_KM:
            continue
        alt_m = s[7] if s[7] is not None else s[13]
        pos_time = s[3] or s[4] or server_time
        age = max(0, server_time - pos_time)  # how old the position already was
        planes.append(
            {
                "icao24": s[0],
                "callsign": (s[1] or "").strip() or "(unknown)",
                "country": s[2],
                "lat": lat,
                "lon": lon,
                "alt_ft": alt_m * 3.281 if alt_m is not None else None,
                "speed_ms": s[9],
                "track": s[10],
                "vrate_fpm": s[11] * 196.85 if s[11] is not None else None,
                # local time the position was true, independent of clock differences
                "t_ref": got_at - age,
            }
        )
    return planes


# ---------- adsbdb lookups (cached) ----------

def lookup_route(callsign):
    """Fetch and cache {'airline', 'origin', 'destination'} from adsbdb."""
    if not callsign or callsign == "(unknown)" or callsign in _route_cache:
        return
    result = None
    try:
        r = requests.get(ADSBDB_ROUTE_URL.format(callsign), timeout=8)
        if r.status_code == 200:
            route = r.json().get("response")
            route = route.get("flightroute") if isinstance(route, dict) else None
            if route:
                def place(p):
                    if not p:
                        return None
                    code = p.get("iata_code") or p.get("icao_code") or ""
                    city = p.get("municipality") or p.get("name") or ""
                    return f"{city} ({code})" if code else city

                result = {
                    "airline": (route.get("airline") or {}).get("name"),
                    "origin": place(route.get("origin")),
                    "destination": place(route.get("destination")),
                }
    except requests.RequestException:
        return  # not cached; retried on the next poll
    _route_cache[callsign] = result


def lookup_aircraft(icao24):
    """Fetch and cache {'type', 'registration', 'owner'} from adsbdb."""
    if icao24 in _aircraft_cache:
        return
    result = None
    try:
        r = requests.get(ADSBDB_AIRCRAFT_URL.format(icao24), timeout=8)
        if r.status_code == 200:
            ac = r.json().get("response")
            ac = ac.get("aircraft") if isinstance(ac, dict) else None
            if ac:
                kind = " ".join(x for x in (ac.get("manufacturer"), ac.get("type")) if x)
                result = {
                    "type": kind or None,
                    "registration": ac.get("registration"),
                    "owner": ac.get("registered_owner"),
                }
    except requests.RequestException:
        return
    _aircraft_cache[icao24] = result


# ---------- background poller ----------

def poller():
    """Runs forever in a thread: fetch planes, then look up details for the closest."""
    while True:
        started = time.time()
        wait = POLL_SECONDS
        try:
            planes = fetch_aircraft()
            with _lock:
                _state.update(planes=planes, fetched=time.time(), error=None)
            planes.sort(key=lambda p: haversine_km(HOME_LAT, HOME_LON, p["lat"], p["lon"]))
            for p in planes[:MAX_SHOWN]:
                lookup_route(p["callsign"])
                lookup_aircraft(p["icao24"])
        except RateLimited as e:
            wait = max(wait, e.retry_after)
            with _lock:
                _state["error"] = str(e)
        except Exception as e:  # keep running through network/API hiccups
            with _lock:
                _state["error"] = f"Update failed: {e}"
        next_poll = started + wait
        with _lock:
            _state["next_poll"] = next_poll
        time.sleep(max(1, next_poll - time.time()))


def snapshot():
    """Project every plane to 'now' and return them sorted by distance."""
    with _lock:
        planes = list(_state["planes"])
        fetched = _state["fetched"]
        error = _state["error"]
        next_poll = _state["next_poll"]

    now = time.time()
    live = []
    for p in planes:
        elapsed = min(max(now - p["t_ref"], 0), MAX_PROJECT_SECONDS)
        lat, lon = project(p["lat"], p["lon"], p["speed_ms"], p["track"], elapsed)
        dist = haversine_km(HOME_LAT, HOME_LON, lat, lon)
        if dist > RADIUS_KM:
            continue  # flown out of range since the last update
        alt = p["alt_ft"]
        if alt is not None and p["vrate_fpm"] is not None:
            alt = max(0, alt + p["vrate_fpm"] * elapsed / 60)
        live.append(dict(
            p,
            dist_km=dist,
            bearing=bearing_deg(HOME_LAT, HOME_LON, lat, lon),
            alt_ft=alt,
        ))
    live.sort(key=lambda p: p["dist_km"])
    return live, error, fetched, next_poll


# ---------- display ----------

def fmt(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "n/a"


def climb_label(vrate):
    if vrate is None:
        return ""
    if vrate > 300:
        return "climbing"
    if vrate < -300:
        return "descending"
    return "level"


def render(planes, error, fetched, next_poll):
    """Build one screen frame. Returns (frame_text, ids_of_planes_overhead)."""
    lines = []

    def add(s=""):
        lines.append(s.upper())  # boards shout in capitals

    now = time.time()
    add("=" * 52)
    add("  FLYING ABOVE ME")
    add(f"  {datetime.now():%H:%M:%S}  |  radius {RADIUS_KM} km")
    if fetched is None:
        add("  Waiting for first data...")
    else:
        add(f"  Data {now - fetched:.0f}s old  |  next update in {max(0, next_poll - now):.0f}s")
    add("=" * 52)

    if error:
        add()
        add(f"  ! {error}")

    overhead = set()
    if not planes:
        if fetched is not None:
            add()
            add("  Clear skies. No aircraft in range.")
    else:
        nearest = planes[0]
        add()
        add(f"  NEAREST: {nearest['callsign']}")
        alt = round(nearest["alt_ft"]) if nearest["alt_ft"] is not None else None
        spd = round(nearest["speed_ms"] * 1.944) if nearest["speed_ms"] is not None else None
        add(f"  {nearest['dist_km']:.2f} km {compass(nearest['bearing'])}  |  "
            f"{fmt(alt, ' ft')}  |  {fmt(spd, ' kt')}  {climb_label(nearest['vrate_fpm'])}")
        add(f"  ICAO {nearest['icao24']}  |  {nearest['country']}")

        route = _route_cache.get(nearest["callsign"])
        aircraft = _aircraft_cache.get(nearest["icao24"])
        if route:
            if route.get("airline"):
                add(f"  Airline: {route['airline']}")
            if route.get("origin") or route.get("destination"):
                add(f"  Route:   {route.get('origin') or '?'}  ->  {route.get('destination') or '?'}")
        if aircraft:
            if aircraft.get("type"):
                add(f"  Aircraft: {aircraft['type']}")
            reg, owner = aircraft.get("registration"), aircraft.get("owner")
            if reg or owner:
                add(f"  Reg: {reg or 'n/a'}  |  Owner: {owner or 'n/a'}")

        overhead = {p["icao24"] for p in planes if p["dist_km"] <= OVERHEAD_KM}
        if nearest["icao24"] in overhead:
            add()
            # Black-on-yellow banner (added raw so the escape codes aren't upper-cased)
            lines.append(f"{INVERT_ON}  >>> OVERHEAD NOW <<<  {INVERT_OFF}")

        if len(planes) > 1:
            add()
            add("  Also nearby:")
            for p in planes[1:MAX_SHOWN]:
                p_alt = round(p["alt_ft"]) if p["alt_ft"] is not None else None
                p_spd = round(p["speed_ms"] * 1.944) if p["speed_ms"] is not None else None
                add(f"   {p['callsign']:<9} {p['dist_km']:>6.2f} km {compass(p['bearing']):<2}  "
                    f"{fmt(p_alt, ' ft'):>9}  {fmt(p_spd, ' kt'):>7}")

    # Overwrite in place (no full clear) so the screen doesn't flicker.
    frame = BG_BLACK + FG_YELLOW + HOME
    frame += "".join(line + ERASE_LINE + "\n" for line in lines) + ERASE_BELOW
    return frame, overhead


def main():
    os.system("")  # enables ANSI colors in Windows terminals; harmless elsewhere
    threading.Thread(target=poller, daemon=True).start()

    sys.stdout.write(BG_BLACK + FG_YELLOW + HIDE_CURSOR + CLEAR)
    alerted = set()
    try:
        while True:
            tick = time.time()
            planes, error, fetched, next_poll = snapshot()
            frame, overhead = render(planes, error, fetched, next_poll)
            if overhead - alerted:  # beep once per plane, not every second
                frame += "\a"
            alerted = overhead
            sys.stdout.write(frame)
            sys.stdout.flush()
            time.sleep(max(0, SCREEN_REFRESH_SECONDS - (time.time() - tick)))
    finally:
        sys.stdout.write(RESET + SHOW_CURSOR + CLEAR)
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bye!")
