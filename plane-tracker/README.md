# Flying Above Me

A live, airport-departure-board style tracker that shows the aircraft flying
near you: callsign, distance, direction, altitude, speed, airline, route and
aircraft type. Black screen, yellow capitals, runs in a terminal.

The screen redraws every second. Fresh data is fetched in the background every
25-60 seconds; between fetches each plane's position is projected forward from
its last known speed and heading, so distances tick down live.

## Setup

Requires Python 3.9+.

```
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Copy the example config and put in your own coordinates (right-click your
location in Google Maps to copy them; negative longitude = west):

```
cp config.example.json config.json      # Windows: copy config.example.json config.json
```

`config.json` is git-ignored, so your home location is never committed.

## Run

```
python flying_above_me.py
```

Press `Ctrl+C` to quit. Maximise the terminal and increase the font size for a
wall-display look.

## Config options (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `home_lat`, `home_lon` | New York | Your location |
| `radius_km` | 25 | Search radius |
| `max_shown` | 5 | Planes listed on screen |
| `overhead_km` | 3 | Distance that triggers the OVERHEAD banner and beep |
| `api_poll_seconds` | `null` | Seconds between API fetches (`null` = automatic) |

## OpenSky rate limits

Positions come from the OpenSky Network API. Anonymous access allows about 400
requests per day, so at the default 60 s polling it lasts a few hours. A free
OpenSky account (create an API client in your account settings) raises that to
4,000 per day and enables 25 s polling. Provide the credentials through
environment variables - never put them in the repo:

```
# macOS / Linux
export OPENSKY_CLIENT_ID=your-id
export OPENSKY_CLIENT_SECRET=your-secret

# Windows (PowerShell)
$env:OPENSKY_CLIENT_ID="your-id"
$env:OPENSKY_CLIENT_SECRET="your-secret"
```

For all-day anonymous use, set `"api_poll_seconds": 220`.

## Data sources and credits

- Aircraft positions: [OpenSky Network](https://opensky-network.org) (research and non-commercial use)
- Airline, route and aircraft details: [adsbdb](https://www.adsbdb.com)

Not every flight has route data (private, military and some cargo flights
often don't), and the on-screen positions between updates are estimates.
