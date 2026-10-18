import time
from datetime import datetime

import requests

DEFAULT_LATITUDE = 32.08
DEFAULT_LONGITUDE = 34.78
CACHE_TTL_SECONDS = 1800
TIMEZONE = "Asia/Jerusalem"
HEADERS = {"User-Agent": "shaked-hub-dashboard/1.0"}

_cache = {}


def _cached(key, fetch):
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        result = cached[1]
        if isinstance(result, Exception):
            raise result
        return result
    try:
        result = fetch()
    except Exception as e:
        _cache[key] = (time.time(), e)
        raise
    _cache[key] = (time.time(), result)
    return result


def _forecast(latitude, longitude):
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,wind_speed_10m",
            "hourly": "temperature_2m,apparent_temperature,dew_point_2m,precipitation_probability",
            "forecast_days": 2,
            "timezone": TIMEZONE,
        },
        headers=HEADERS,
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def _air_quality(latitude, longitude):
    response = requests.get(
        "https://air-quality-api.open-meteo.com/v1/air-quality",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "us_aqi,pm10,pm2_5,dust",
            "hourly": "us_aqi,pm10",
            "forecast_days": 2,
            "timezone": TIMEZONE,
        },
        headers=HEADERS,
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def get_current_weather(latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE):
    current = _cached(("forecast", latitude, longitude), lambda: _forecast(latitude, longitude))["current"]
    return {
        "temperature": current["temperature_2m"],
        "humidity": current["relative_humidity_2m"],
        "windspeed": current["wind_speed_10m"],
    }


def get_running_conditions(latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE):
    """Current conditions plus the best hour to run in the next day. Air quality is optional."""
    forecast = _cached(("forecast", latitude, longitude), lambda: _forecast(latitude, longitude))
    try:
        air = _cached(("air", latitude, longitude), lambda: _air_quality(latitude, longitude))
    except Exception:
        air = None

    current = forecast["current"]
    air_now = (air or {}).get("current", {})
    conditions = {
        "temperature": current["temperature_2m"],
        "feels_like": current["apparent_temperature"],
        "humidity": current["relative_humidity_2m"],
        "dew_point": current["dew_point_2m"],
        "windspeed": current["wind_speed_10m"],
        "aqi": air_now.get("us_aqi"),
        "pm10": air_now.get("pm10"),
    }
    conditions["best_hour"] = _best_hour(forecast, air, current["time"])
    return conditions


def _best_hour(forecast, air, now_iso):
    """The coolest, cleanest daylight-ish hour (05:00-20:00) from now until tomorrow evening."""
    now = datetime.fromisoformat(now_iso)
    hourly = forecast["hourly"]
    air_by_time = {}
    if air:
        air_by_time = dict(zip(air["hourly"]["time"], air["hourly"]["pm10"]))

    best = None
    for i, iso in enumerate(hourly["time"]):
        moment = datetime.fromisoformat(iso)
        if moment < now or not 5 <= moment.hour <= 20:
            continue
        feels = hourly["apparent_temperature"][i]
        dew = hourly["dew_point_2m"][i]
        rain = hourly["precipitation_probability"][i] or 0
        pm10 = air_by_time.get(iso) or 0
        score = feels + max(dew - 15, 0) + max(pm10 - 50, 0) / 10 + rain / 20
        if best is None or score < best[0]:
            best = (score, moment, feels, dew)
    if best is None:
        return None
    _, moment, feels, dew = best
    return {"time": moment, "feels_like": feels, "dew_point": dew}


def recommend_run(conditions, planned_workout=None):
    """Rule-based running advice in Hebrew: what kind of run today suits, and how to adjust."""
    feels = conditions["feels_like"]
    dew = conditions["dew_point"]
    pm10 = conditions.get("pm10") or 0
    aqi = conditions.get("aqi") or 0
    tips = []

    if pm10 >= 150 or aqi >= 150:
        verdict = "אובך כבד: עדיף הליכון, או מנוחה והזזת האימון למחר"
        level = "bad"
    elif feels >= 32 or dew >= 24:
        verdict = "עומס חום גבוה: רק ריצה קלה וקצרה, או הליכון"
        level = "bad"
    elif feels >= 27 or dew >= 20:
        verdict = "חם ולח: ריצה קלה מתאימה. אימון איכות רק בשעה הקרירה ביותר"
        level = "warn"
        tips.append("להאט ב-15-30 שנ' לק\"מ ולרוץ לפי דופק, לא לפי קצב")
    elif feels <= 5:
        verdict = "קר: מתאים לכל סוג ריצה, עם חימום ארוך יותר"
        level = "good"
    else:
        verdict = "תנאים טובים: מתאים גם לאימון איכות או ריצה ארוכה"
        level = "good"

    if 100 <= max(pm10, aqi) < 150:
        tips.append("איכות אוויר בינונית: לוותר על אינטרוולים חזקים")
    if dew >= 18:
        tips.append("לשתות לפני הריצה, ובריצה מעל שעה לקחת שתייה עם מלחים")
    if conditions["windspeed"] >= 25:
        tips.append("רוח חזקה: לצאת נגד הרוח ולחזור איתה")

    best = conditions.get("best_hour")
    if best and level != "good":
        tips.append(f"השעה הכי טובה: {best['time'].strftime('%H:%M')} (מורגש {best['feels_like']:.0f}°)")

    if planned_workout and level == "bad" and "מנוחה" not in planned_workout:
        tips.append("האימון המתוכנן היום לא מתאים לתנאים, שקול להזיז אותו")

    return {"verdict": verdict, "level": level, "tips": tips}
