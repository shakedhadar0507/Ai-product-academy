"""Web Push notifications for the home-screen app, plus the weekly university-mail check.

Subscriptions live in the Supabase table push_subscriptions (see garmin/push_schema.sql),
because Render's disk is wiped on every deploy.
"""
import json
import os
import sys

import requests
from dotenv import load_dotenv
from googleapiclient.discovery import build
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from agents import google_auth

load_dotenv()

TABLE = "push_subscriptions"
UNI_ADDRESS = "shaked.hadar@post.runi.ac.il"
UNI_CHECK_DAYS = 7


def is_configured():
    return bool(
        os.environ.get("VAPID_PUBLIC_KEY")
        and os.environ.get("VAPID_PRIVATE_KEY")
        and os.environ.get("SUPABASE_URL")
        and os.environ.get("SUPABASE_SECRET_KEY")
    )


def _clean_key(name):
    """Env value without stray quotes, spaces or a pasted "NAME=" prefix."""
    value = os.environ.get(name, "").strip().strip("'\"").strip()
    return value.removeprefix(f"{name}=").strip()


def public_key():
    return _clean_key("VAPID_PUBLIC_KEY")


def private_key_problem():
    """None if VAPID_PRIVATE_KEY can sign pushes, else a short reason (never the key itself)."""
    try:
        Vapid.from_string(_clean_key("VAPID_PRIVATE_KEY"))
        return None
    except Exception as e:
        return f"VAPID_PRIVATE_KEY invalid ({type(e).__name__})"


def rest(method, table, params=None, payload=None, prefer=None):
    """Call the Supabase REST API for one table with the server-side secret key."""
    headers = {"apikey": os.environ["SUPABASE_SECRET_KEY"], "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    response = requests.request(
        method,
        f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/{table}",
        params=params,
        json=payload,
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    return response.json() if response.content else None


def save_subscription(subscription):
    rest(
        "POST",
        TABLE,
        params={"on_conflict": "endpoint"},
        payload={"endpoint": subscription["endpoint"], "subscription": subscription},
        prefer="resolution=merge-duplicates",
    )


def send_to_all(title, body, url="/"):
    """Send one notification to every saved device. Returns how many devices got it."""
    sent = 0
    for row in rest("GET", TABLE, params={"select": "endpoint,subscription"}):
        try:
            webpush(
                subscription_info=row["subscription"],
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=_clean_key("VAPID_PRIVATE_KEY"),
                vapid_claims={"sub": "mailto:" + os.environ.get("VAPID_CONTACT", "shaked.hadar0507@gmail.com")},
            )
            sent += 1
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                # The device unsubscribed or the app was removed; forget it.
                rest("DELETE", TABLE, params={"endpoint": f"eq.{row['endpoint']}"})
            else:
                print(f"[ERROR] push to {row['endpoint'][:40]}: {e}", file=sys.stderr)
    return sent


def check_uni_mail():
    """Alert if no forwarded university mail reached Gmail in the last week."""
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("gmail", "v1", credentials=creds)
    result = service.users().messages().list(
        userId="me", q=f"to:{UNI_ADDRESS} newer_than:{UNI_CHECK_DAYS}d", maxResults=1
    ).execute()
    count = len(result.get("messages", []))
    if count:
        return {"ok": True, "recent": count, "sent": 0}
    sent = send_to_all(
        "📭 לא הגיעו מיילים מהלימודים",
        f"שבוע שלם לא הגיע לג'ימייל אף מייל מ-{UNI_ADDRESS}. כדאי לבדוק את ההעברה באאוטלוק.",
        "https://outlook.office.com/mail/options/mail/forwarding",
    )
    return {"ok": True, "recent": 0, "sent": sent}
