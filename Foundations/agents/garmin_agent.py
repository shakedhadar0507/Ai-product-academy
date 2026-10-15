import os
import time
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

CACHE_TTL_SECONDS = 600
HISTORY_DAYS = 35

# Taper plan from the NYC readiness analysis (easy = HR up to 150, MP = 5:55-6:00).
TRAINING_PLAN = {
    date(2026, 10, 13): "מנוחה (התאוששות מהפיליפינים)",
    date(2026, 10, 14): "6-8 ק\"מ קלים",
    date(2026, 10, 15): "מנוחה",
    date(2026, 10, 16): "ריצה ארוכה אחרונה: 28-30 ק\"מ לפנות בוקר, דופק עד ~155, קצב 6:10-6:20. ג'ל כל 35 דק' מק\"מ 8",
    date(2026, 10, 17): "מנוחה",
    date(2026, 10, 18): "5-6 ק\"מ קלים מאוד (התאוששות)",
    date(2026, 10, 19): "מנוחה",
    date(2026, 10, 20): "10 ק\"מ: 2 חימום, 3×2 ק\"מ ב-MP (מנוחה 2 דק'), 2 שחרור",
    date(2026, 10, 21): "8 ק\"מ קלים",
    date(2026, 10, 22): "מנוחה",
    date(2026, 10, 23): "16-18 ק\"מ קלים",
    date(2026, 10, 24): "מנוחה",
    date(2026, 10, 25): "6-8 ק\"מ קלים",
    date(2026, 10, 26): "10 ק\"מ קלים + 4 האצות של 20 שנ'",
    date(2026, 10, 27): "מנוחה",
    date(2026, 10, 28): "8 ק\"מ, מתוכם 2-3×1.5 ק\"מ ב-MP",
    date(2026, 10, 29): "טיסה ✈️ מנוחה. לשתות הרבה, ללכת במטוס כל שעתיים",
    date(2026, 10, 30): "4-5 ק\"מ קלים בסנטרל פארק + 3 האצות",
    date(2026, 10, 31): "מנוחה או 15-20 דק' ג'וגינג. אקספו, ארוחת פחמימות, לישון מוקדם",
    date(2026, 11, 1): "🏁 מרתון ניו יורק",
}

_cache = {}


def is_configured():
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SECRET_KEY"))


def get_planned_workout(day):
    return TRAINING_PLAN.get(day)


def _query(table, params):
    response = requests.get(
        f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/{table}",
        params=params,
        headers={"apikey": os.environ["SUPABASE_SECRET_KEY"]},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def get_data(today):
    cached = _cache.get(today)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    wellness_rows = _query("garmin_daily_wellness", {
        "select": "date,resting_hr,hrv_last_night_ms,hrv_weekly_avg_ms,hrv_status,sleep_score,"
                  "sleep_s,body_battery_high,training_readiness,training_status",
        "order": "date.desc",
        "limit": "3",
    })
    runs = _query("garmin_activities", {
        "select": "start_time_local,name,distance_m,duration_s,avg_pace_s_per_km,avg_hr",
        "type": "like.*running*",
        "start_time_local": f"gte.{today - timedelta(days=HISTORY_DAYS)}",
        "order": "start_time_local.desc",
    })

    # Today's row fills in during the day, so take the latest night that has sleep or HRV.
    wellness = next(
        (row for row in wellness_rows if row.get("sleep_score") or row.get("hrv_last_night_ms")),
        wellness_rows[0] if wellness_rows else None,
    )
    data = {"wellness": wellness, "runs": runs, "weekly_km": _weekly_km(runs, today)}
    _cache[today] = (time.time(), data)
    return data


def _weekly_km(runs, today, weeks=4):
    """Kilometres per week (Monday to Sunday, like Garmin), oldest first, current week last."""
    this_monday = today - timedelta(days=today.weekday())
    totals = []
    for offset in range(weeks - 1, -1, -1):
        start = this_monday - timedelta(weeks=offset)
        end = start + timedelta(days=7)
        km = sum(
            (run["distance_m"] or 0) / 1000
            for run in runs
            if start <= datetime.fromisoformat(run["start_time_local"]).date() < end
        )
        totals.append((start, round(km, 1)))
    return totals


def as_training_log(runs):
    """Runs in the (date, km, minutes, notes) shape the daily brief already expects, oldest first."""
    return [
        (
            run["start_time_local"][:10],
            round((run["distance_m"] or 0) / 1000, 1),
            round((run["duration_s"] or 0) / 60),
            run.get("name") or "",
        )
        for run in reversed(runs)
    ]


def format_pace(seconds_per_km):
    if not seconds_per_km:
        return "–"
    minutes, seconds = divmod(round(seconds_per_km), 60)
    return f"{minutes}:{seconds:02d}"


def format_hours(seconds):
    if not seconds:
        return "–"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}:{minutes:02d}"


def format_number(value):
    return None if value is None else f"{value:.0f}"


def format_status(phrase):
    """Garmin feedback codes like 'PRODUCTIVE_2' -> 'Productive'."""
    if not phrase:
        return None
    base, _, suffix = phrase.rpartition("_")
    return (base if suffix.isdigit() and base else phrase).replace("_", " ").capitalize()
