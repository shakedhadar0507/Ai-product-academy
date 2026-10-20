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


def public_key():
    return os.environ.get("VAPID_PUBLIC_KEY", "")


def _rest(method, params=None, payload=None, prefer=None):
    headers = {"apikey": os.environ["SUPABASE_SECRET_KEY"], "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    response = requests.request(
        method,
        f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/{TABLE}",
        params=params,
        json=payload,
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    return response.json() if response.content else None


def save_subscription(subscription):
    _rest(
        "POST",
        params={"on_conflict": "endpoint"},
        payload={"endpoint": subscription["endpoint"], "subscription": subscription},
        prefer="resolution=merge-duplicates",
    )


def send_to_all(title, body, url="/"):
    """Send one notification to every saved device. Returns how many devices got it."""
    sent = 0
    for row in _rest("GET", params={"select": "endpoint,subscription"}):
        try:
            webpush(
                subscription_info=row["subscription"],
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                vapid_claims={"sub": "mailto:" + os.environ.get("VAPID_CONTACT", "shaked.hadar0507@gmail.com")},
            )
            sent += 1
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                # The device unsubscribed or the app was removed; forget it.
                _rest("DELETE", params={"endpoint": f"eq.{row['endpoint']}"})
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
