import time

import requests

DEFAULT_LATITUDE = 32.08
DEFAULT_LONGITUDE = 34.78
CACHE_TTL_SECONDS = 600

_cache = {}


def get_current_weather(latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE):
    cache_key = (latitude, longitude)
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    current = response.json()["current"]
    weather = {
        "temperature": current["temperature_2m"],
        "humidity": current["relative_humidity_2m"],
        "windspeed": current["wind_speed_10m"],
    }
    _cache[cache_key] = (time.time(), weather)
    return weather
