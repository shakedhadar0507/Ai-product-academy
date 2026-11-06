"""Web Push notifications for the home-screen app, plus the weekly university-mail check.

Subscriptions live in the Supabase table push_subscriptions (see garmin/push_schema.sql),
because Render's disk is wiped on every deploy.
"""
import base64
import json
import os
import sys
import threading

import requests
from dotenv import load_dotenv
from googleapiclient.discovery import build
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from agents import google_auth

load_dotenv()

TABLE = "push_subscriptions"
UNI_ADDRESS = "shaked.hadar@post.runi.ac.il"
UNI_CHECK_DAYS = 7


# Rows whose endpoint starts with this prefix hold server settings (the push key, the Garmin
# login), not devices. They live here so no extra table or Render variable is needed.
CONFIG_PREFIX = "config:"
DEVICES_ONLY = {"endpoint": f"not.like.{CONFIG_PREFIX}*"}

_keys = {"lock": threading.Lock(), "pair": None, "source": None}


def is_configured():
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SECRET_KEY"))


def _clean_key(name):
    """Env value without stray quotes, spaces or a pasted "NAME=" prefix."""
    value = os.environ.get(name, "").strip().strip("'\"").strip()
    return value.removeprefix(f"{name}=").strip()


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _public_from_private(private_b64):
    vapid = Vapid.from_string(private_b64)
    return _b64url(vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    ))


def get_config(name):
    rows = rest("GET", TABLE, params={"select": "subscription", "endpoint": f"eq.{CONFIG_PREFIX}{name}"})
    return rows[0]["subscription"] if rows else None


def set_config(name, value):
    rest(
        "POST",
        TABLE,
        params={"on_conflict": "endpoint"},
        payload={"endpoint": f"{CONFIG_PREFIX}{name}", "subscription": value},
        prefer="resolution=merge-duplicates",
    )


def key_pair():
    """(public, private) VAPID keys. A valid VAPID_PRIVATE_KEY on Render wins; otherwise the
    server makes its own pair once and keeps it in Supabase, so nothing has to be pasted."""
    with _keys["lock"]:
        if _keys["pair"]:
            return _keys["pair"]
        private = _clean_key("VAPID_PRIVATE_KEY")
        try:
            pair, source = (_public_from_private(private), private), "render"
        except Exception:
            stored = get_config("vapid")
            if stored:
                pair, source = (stored["public"], stored["private"]), "server"
            else:
                key = ec.generate_private_key(ec.SECP256R1())
                private = _b64url(key.private_numbers().private_value.to_bytes(32, "big"))
                pair, source = (_public_from_private(private), private), "server"
                set_config("vapid", {"public": pair[0], "private": pair[1]})
                # Devices saved so far signed up with another key and can't get pushes from this one.
                rest("DELETE", TABLE, params=DEVICES_ONLY)
        _keys.update(pair=pair, source=source)
        return pair


def key_source():
    return _keys["source"]


def public_key():
    return key_pair()[0]


def private_key_problem():
    """None if pushes can be signed, else a short reason (never the key itself)."""
    try:
        Vapid.from_string(key_pair()[1])
        return None
    except Exception as e:
        return f"push key unavailable ({type(e).__name__})"


def device_count():
    return len(rest("GET", TABLE, params={"select": "endpoint", **DEVICES_ONLY}))


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
    private = key_pair()[1]
    for row in rest("GET", TABLE, params={"select": "endpoint,subscription", **DEVICES_ONLY}):
        try:
            webpush(
                subscription_info=row["subscription"],
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=private,
                vapid_claims={"sub": "mailto:" + os.environ.get("VAPID_CONTACT", "shaked.hadar0507@gmail.com")},
            )
            sent += 1
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (401, 403, 404, 410):
                # The device unsubscribed, the app was removed, or it signed up with an older
                # key (Apple answers 403). It re-subscribes from the dashboard; forget this one.
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
