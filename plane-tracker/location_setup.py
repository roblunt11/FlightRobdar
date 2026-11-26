"""First-run location setup for FlightRobdar.

If config.json does not exist, ask for a postcode or place name, look up its
coordinates, confirm with the user, and write config.json.

Lookups:
  * UK postcodes -> postcodes.io (free, no key)
  * anything else -> OpenStreetMap Nominatim (free, no key, one-off lookup)

Only the resulting latitude/longitude are saved, never the postcode.
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests

CONFIG_PATH = Path(__file__).with_name("config.json")
EXAMPLE_PATH = Path(__file__).with_name("config.example.json")
USER_AGENT = "FlightRobdar/1.0 (https://github.com/roblunt11/FlightRobdar)"
TIMEOUT = 10

_UK_POSTCODE = re.compile(r"^[A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2}$")
_LATLON = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def _valid(lat, lon):
    return -90 <= lat <= 90 and -180 <= lon <= 180


def geocode(query):
    """Return (lat, lon, label) or None if not found.

    Raises requests.RequestException if the network lookup itself fails.
    """
    query = query.strip()
    if not query:
        return None

    m = _LATLON.match(query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return (lat, lon, "your coordinates") if _valid(lat, lon) else None

    if _UK_POSTCODE.match(query):
        r = requests.get(
            "https://api.postcodes.io/postcodes/" + quote(query.replace(" ", "")),
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            res = (r.json() or {}).get("result") or {}
            if res.get("latitude") is not None and res.get("longitude") is not None:
                area = res.get("admin_district") or res.get("region") or "UK"
                return res["latitude"], res["longitude"], f"{query.upper()} ({area})"

    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "jsonv2", "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    hits = r.json()
    if hits:
        return float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0].get("display_name", query)
    return None


def _write_config(lat, lon):
    cfg = {}
    if EXAMPLE_PATH.exists():
        try:
            cfg = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    cfg["home_lat"] = round(lat, 4)
    cfg["home_lon"] = round(lon, 4)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def ensure_config():
    """Make sure config.json exists, prompting the user if it does not.

    Returns True if a config exists afterwards, False if setup was skipped
    (non-interactive terminal or the user gave up).
    """
    if CONFIG_PATH.exists():
        return True

    if not sys.stdin.isatty():
        print("No config.json found. Run once in a terminal to set your location,")
        print("or copy config.example.json to config.json and edit it.")
        return False

    print("FLYING ABOVE ME - first-time setup")
    print("Enter your postcode or town (or 'lat,lon' coordinates). Blank to quit.")
    print("Only the coordinates are saved, in config.json on this machine.\n")

    while True:
        try:
            query = input("Postcode or town: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not query:
            return False
        try:
            found = geocode(query)
        except requests.RequestException as exc:
            print(f"  Couldn't reach the lookup service ({exc.__class__.__name__}). Check your connection and try again.")
            continue
        if not found:
            print("  Couldn't find that. Try a full postcode, a town and country, or 'lat,lon'.")
            continue
        lat, lon, label = found
        print(f"  Found: {label}  ({lat:.4f}, {lon:.4f})")
        if input("  Use this location? [Y/n] ").strip().lower() in ("", "y", "yes"):
            _write_config(lat, lon)
            print(f"  Saved to {CONFIG_PATH.name}.\n")
            return True
