"""Give Shaked Hub's server the Garmin login, so it can sync without the PC. Run once on the PC:

    garmin-pipeline\\.venv\\Scripts\\python.exe Foundations\\garmin\\upload_garmin_token.py

It reads the saved Garmin token (~/.garminconnect, made by `python -m garmin_sync.auth`),
refreshes it, and stores it in Supabase (push_subscriptions row "config:garmin", readable
only with the secret key). No password is read or stored. Uses SUPABASE_URL and
SUPABASE_SECRET_KEY from the project's .env, like push_to_supabase.py.
"""
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv()

if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SECRET_KEY"):
    sys.exit("Set SUPABASE_URL and SUPABASE_SECRET_KEY first (in .env).")

garmin = Garmin()
garmin.login(os.environ.get("GARMIN_TOKEN_DIR", "~/.garminconnect"))
print(f"Garmin token OK for {garmin.get_full_name()}.")

response = requests.post(
    f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/push_subscriptions",
    params={"on_conflict": "endpoint"},
    headers={
        "apikey": os.environ["SUPABASE_SECRET_KEY"],
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    },
    json={"endpoint": "config:garmin", "subscription": {"tokens": garmin.client.dumps()}},
    timeout=30,
)
if not response.ok:
    sys.exit(f"Supabase rejected the token: {response.status_code} {response.text}")
print("Saved. Shaked Hub now syncs Garmin by itself every 2 hours.")
