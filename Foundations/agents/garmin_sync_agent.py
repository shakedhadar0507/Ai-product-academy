"""Garmin sync from the server, so fresh data no longer depends on the PC being on.

Pulls the last few days of activities and daily wellness straight from Garmin Connect and
upserts them into the same Supabase tables the PC pipeline fills (laps and per-second FIT
records still come only from the PC).

The Garmin login is a token, never the password. It is read from the Supabase settings row
"config:garmin" (put there once by garmin/upload_garmin_token.py on the PC) or from the
GARMINTOKENS variable. Garmin refreshes the token now and then; the new one is saved back to
Supabase so a restart never falls back to a stale copy.
"""
import json
import os
import sys
import threading
import time
from datetime import date, timedelta

import requests

from agents import formatting, push_agent

SYNC_DAYS = 3
PAUSE_S = 0.5

_state = {"lock": threading.Lock(), "running": False, "last": None}


def _dig(data, *path):
    for key in path:
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list) and isinstance(key, int) and len(data) > key:
            data = data[key]
        else:
            return None
    return data


def _token():
    try:
        stored = push_agent.get_config("garmin")
    except Exception as e:
        print(f"[ERROR] garmin token read: {type(e).__name__}", file=sys.stderr)
        stored = None
    if stored and stored.get("tokens"):
        return stored["tokens"]
    return os.environ.get("GARMINTOKENS", "").strip() or None


def is_configured():
    return push_agent.is_configured() and bool(_token())


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        print(f"[WARN] garmin {fn.__name__}{args}: {type(e).__name__}", file=sys.stderr)
        return None
    finally:
        time.sleep(PAUSE_S)


def activity_row(a):
    distance, moving = a.get("distance"), a.get("movingDuration") or a.get("duration")
    stride = a.get("avgStrideLength")
    return {
        "activity_id": a["activityId"],
        "start_time_local": a.get("startTimeLocal"),
        "type": _dig(a, "activityType", "typeKey"),
        "name": a.get("activityName"),
        "distance_m": distance,
        "duration_s": a.get("duration"),
        "moving_s": a.get("movingDuration"),
        "avg_pace_s_per_km": moving / (distance / 1000) if distance and moving else None,
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "elevation_gain_m": a.get("elevationGain"),
        "avg_cadence_spm": a.get("averageRunningCadenceInStepsPerMinute"),
        "avg_power_w": a.get("avgPower"),
        "aerobic_te": a.get("aerobicTrainingEffect"),
        "anaerobic_te": a.get("anaerobicTrainingEffect"),
        "training_load": a.get("activityTrainingLoad"),
        "vo2max": a.get("vO2MaxValue"),
        "avg_gct_ms": a.get("avgGroundContactTime"),
        "avg_vo_cm": a.get("avgVerticalOscillation"),
        "avg_stride_m": stride / 100 if stride else None,
        "avg_vertical_ratio": a.get("avgVerticalRatio"),
        "avg_gct_balance": a.get("avgGroundContactBalance"),
    }


def wellness_row(garmin, day):
    summary = _safe(garmin.get_user_summary, day) or {}
    hrv = _safe(garmin.get_hrv_data, day) or {}
    sleep = _safe(garmin.get_sleep_data, day) or {}
    readiness = _safe(garmin.get_training_readiness, day) or []
    status = _safe(garmin.get_training_status, day) or {}

    sleep_dto = sleep.get("dailySleepDTO") or {}
    latest = _dig(status, "mostRecentTrainingStatus", "latestTrainingStatusData") or {}
    device_status = next(iter(latest.values()), {}) if latest else {}
    acute = device_status.get("acuteTrainingLoadDTO") or {}
    return {
        "date": day,
        "resting_hr": summary.get("restingHeartRate"),
        "hrv_last_night_ms": _dig(hrv, "hrvSummary", "lastNightAvg"),
        "hrv_weekly_avg_ms": _dig(hrv, "hrvSummary", "weeklyAvg"),
        "hrv_status": _dig(hrv, "hrvSummary", "status"),
        "sleep_score": _dig(sleep_dto, "sleepScores", "overall", "value"),
        "sleep_s": sleep_dto.get("sleepTimeSeconds"),
        "deep_sleep_s": sleep_dto.get("deepSleepSeconds"),
        "light_sleep_s": sleep_dto.get("lightSleepSeconds"),
        "rem_sleep_s": sleep_dto.get("remSleepSeconds"),
        "awake_s": sleep_dto.get("awakeSleepSeconds"),
        "body_battery_high": summary.get("bodyBatteryHighestValue"),
        "body_battery_low": summary.get("bodyBatteryLowestValue"),
        "body_battery_charged": summary.get("bodyBatteryChargedValue"),
        "stress_avg": summary.get("averageStressLevel"),
        "steps": summary.get("totalSteps"),
        "training_readiness": _dig(readiness, 0, "score"),
        "training_status": device_status.get("trainingStatusFeedbackPhrase"),
        "acute_load": acute.get("dailyTrainingLoadAcute"),
        "chronic_load": acute.get("dailyTrainingLoadChronic"),
        "vo2max_running": _dig(status, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue"),
    }


def _upsert(table, rows, conflict):
    if not rows:
        return
    response = requests.post(
        f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/{table}",
        params={"on_conflict": conflict},
        headers={
            "apikey": os.environ["SUPABASE_SECRET_KEY"],
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        data=json.dumps(rows),
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Supabase {table} {response.status_code}: {response.text[:120]}")


def sync(days=SYNC_DAYS):
    """Run one sync. Returns a small result dict (also kept as the last result)."""
    from garminconnect import Garmin  # imported here so the app still starts without it

    started = time.time()
    token = _token()
    if not token:
        return {"ok": False, "error": "no Garmin token yet"}
    garmin = Garmin()
    garmin.login(token)

    end = formatting.today_in_tz()
    start = end - timedelta(days=days - 1)
    activities = garmin.get_activities_by_date(start.isoformat(), end.isoformat()) or []
    _upsert("garmin_activities", [activity_row(a) for a in activities], "activity_id")

    wellness, day = [], start
    while day <= end:
        wellness.append(wellness_row(garmin, day.isoformat()))
        day += timedelta(days=1)
    _upsert("garmin_daily_wellness", wellness, "date")

    refreshed = garmin.client.dumps()
    if refreshed and refreshed != token:
        push_agent.set_config("garmin", {"tokens": refreshed})
    return {
        "ok": True,
        "activities": len(activities),
        "days": len(wellness),
        "seconds": round(time.time() - started),
    }


def run_in_background(on_done=None):
    """Start a sync unless one is running. Returns False if one was already running."""
    with _state["lock"]:
        if _state["running"]:
            return False
        _state["running"] = True

    def work():
        try:
            result = sync()
        except Exception as e:
            print(f"[ERROR] garmin sync: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            result = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        result["finished_at"] = formatting.now_in_tz().strftime("%d/%m %H:%M")
        result["finished_ts"] = time.time()
        _state.update(last=result, running=False)
        if on_done:
            on_done(result)

    threading.Thread(target=work, daemon=True).start()
    return True


def status():
    return {"running": _state["running"], "last": _state["last"]}
