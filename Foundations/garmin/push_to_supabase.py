"""Push Garmin data from the local SQLite DB (made by garmin-pipeline) to Supabase,
so the dashboard on Render can read it.

    python Foundations/garmin/push_to_supabase.py              # last 10 days
    python Foundations/garmin/push_to_supabase.py --all        # everything

Needs SUPABASE_URL and SUPABASE_SECRET_KEY in the environment or in the
project's .env (never commit them). The per-second FIT records stay local.
"""

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

DEFAULT_DB = Path(__file__).resolve().parents[2] / "garmin-pipeline" / "data" / "garmin.db"
BATCH_SIZE = 500

ACTIVITY_COLUMNS = [
    "activity_id", "start_time_local", "type", "name", "distance_m", "duration_s", "moving_s",
    "avg_pace_s_per_km", "avg_hr", "max_hr", "elevation_gain_m", "avg_cadence_spm", "avg_power_w",
    "aerobic_te", "anaerobic_te", "training_load", "vo2max", "avg_gct_ms", "avg_vo_cm",
    "avg_stride_m", "avg_vertical_ratio", "avg_gct_balance",
]
LAP_COLUMNS = [
    "activity_id", "lap_index", "start_time", "distance_m", "duration_s", "avg_hr", "max_hr",
    "avg_speed_mps", "avg_cadence_spm", "avg_power_w", "avg_gct_ms", "avg_vo_mm", "avg_stride_mm",
]
WELLNESS_COLUMNS = [
    "date", "resting_hr", "hrv_last_night_ms", "hrv_weekly_avg_ms", "hrv_status", "sleep_score",
    "sleep_s", "deep_sleep_s", "light_sleep_s", "rem_sleep_s", "awake_s", "body_battery_high",
    "body_battery_low", "body_battery_charged", "stress_avg", "steps", "training_readiness",
    "training_status", "acute_load", "chronic_load", "vo2max_running",
]


def fetch_rows(conn, sql, params, columns):
    return [dict(zip(columns, row)) for row in conn.execute(sql, params)]


def upsert(table, rows, conflict_columns):
    url = os.environ["SUPABASE_URL"].rstrip("/")
    headers = {
        "apikey": os.environ["SUPABASE_SECRET_KEY"],
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    for start in range(0, len(rows), BATCH_SIZE):
        response = requests.post(
            f"{url}/rest/v1/{table}",
            params={"on_conflict": conflict_columns},
            headers=headers,
            json=rows[start:start + BATCH_SIZE],
            timeout=30,
        )
        if not response.ok:
            sys.exit(f"Supabase rejected {table}: {response.status_code} {response.text}")
    print(f"{table}: {len(rows)} rows")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--all", action="store_true", help="push the whole history")
    args = parser.parse_args()

    load_dotenv()
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SECRET_KEY"):
        sys.exit("Set SUPABASE_URL and SUPABASE_SECRET_KEY first (in .env).")

    cutoff = "0000-01-01" if args.all else (date.today() - timedelta(days=args.days)).isoformat()
    conn = sqlite3.connect(args.db)

    activities = fetch_rows(
        conn,
        f"SELECT {', '.join(ACTIVITY_COLUMNS)} FROM activities WHERE start_time_local >= ?",
        (cutoff,),
        ACTIVITY_COLUMNS,
    )
    laps = fetch_rows(
        conn,
        f"SELECT {', '.join('l.' + c for c in LAP_COLUMNS)} FROM activity_laps l "
        "JOIN activities a USING (activity_id) WHERE a.start_time_local >= ?",
        (cutoff,),
        LAP_COLUMNS,
    )
    wellness = fetch_rows(
        conn,
        f"SELECT {', '.join(WELLNESS_COLUMNS)} FROM daily_wellness WHERE date >= ?",
        (cutoff,),
        WELLNESS_COLUMNS,
    )

    upsert("garmin_activities", activities, "activity_id")
    upsert("garmin_activity_laps", laps, "activity_id,lap_index")
    upsert("garmin_daily_wellness", wellness, "date")


if __name__ == "__main__":
    main()
