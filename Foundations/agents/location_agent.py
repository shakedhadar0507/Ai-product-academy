"""Where Shaked is right now, as the phone reports it.

The page sends the phone's coordinates and time zone with each chat message. The last report is
kept in the Supabase settings row "config:location", so a message sent without one (location
permission off, or a slow GPS fix) still uses the most recent place.
"""
import sys
import time
from datetime import datetime, timezone

import requests

from agents import formatting, push_agent

HEADERS = {"User-Agent": "shaked-hub-dashboard/1.0"}
MAX_AGE_HOURS = 12

_geocode_cache = {}


def place_name(lat, lon):
    """City and country for coordinates, e.g. 'Tel Aviv-Yafo, Israel'. None if the lookup fails."""
    key = (round(lat, 2), round(lon, 2))
    cached = _geocode_cache.get(key)
    if cached and time.time() - cached[0] < 86400:
        return cached[1]
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 12, "accept-language": "he,en"},
            headers=HEADERS, timeout=6,
        )
        r.raise_for_status()
        a = r.json().get("address", {})
        city = a.get("city") or a.get("town") or a.get("village") or a.get("suburb") or a.get("county")
        name = ", ".join(x for x in (city, a.get("state") if a.get("country_code") == "us" else None, a.get("country")) if x) or None
    except Exception as e:
        print(f"[WARN] reverse geocode: {type(e).__name__}: {e}", file=sys.stderr)
        name = None
    _geocode_cache[key] = (time.time(), name)
    return name


def remember(report, store=True):
    """Store a fresh report from the page and return it in full form, or None if it is unusable.

    `report` is {"lat", "lon", "accuracy"?, "tz"?}."""
    try:
        lat, lon = float(report["lat"]), float(report["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    tz = formatting.resolve_tz_name(report.get("tz")) if report.get("tz") else None
    loc = {
        "lat": round(lat, 4), "lon": round(lon, 4),
        "accuracy_m": int(float(report.get("accuracy") or 0)) or None,
        "tz": tz, "place": place_name(lat, lon),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if store:
        try:
            push_agent.set_config("location", loc)
        except Exception as e:
            print(f"[ERROR] location save: {type(e).__name__}", file=sys.stderr)
    return loc


def current(report=None, store=True):
    """The best known location: this request's report if it has one, otherwise the last stored
    one if it is under MAX_AGE_HOURS old. None if nothing is known."""
    if report:
        loc = remember(report, store)
        if loc:
            return loc
    try:
        loc = push_agent.get_config("location")
    except Exception:
        return None
    if not loc:
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(loc["at"])
    except (KeyError, ValueError):
        return None
    if age.total_seconds() > MAX_AGE_HOURS * 3600:
        return None
    return loc
