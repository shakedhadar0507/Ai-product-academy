"""University alerts: Moodle deadlines (calendar export) and actionable mails from the uni address.

Moodle: Calendar → Import or export calendars → Export → "All courses" + "Recent and next 60 days"
→ Get calendar URL. That URL (with its personal token) goes in MOODLE_ICAL_URL.

Mail: every forwarded mail to the uni address is read once by Claude, which decides whether it
needs action (new assignment, deadline, exam, cancelled class...). Only those become a push.

What was already sent is kept in the Supabase table uni_alerts (see garmin/uni_schema.sql),
so a restart or a redeploy never repeats a notification.
"""
import base64
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import anthropic
import requests
from dotenv import load_dotenv
from googleapiclient.discovery import build

from agents import formatting, google_auth, push_agent

load_dotenv()

ALERTS_TABLE = "uni_alerts"
MOODLE_URL = os.environ.get("MOODLE_URL", "https://moodle.runi.ac.il/my/")
NEW_EVENT_WINDOW = timedelta(days=14)
REMINDERS = [("48h", timedelta(hours=48), "בעוד יומיים"), ("12h", timedelta(hours=12), "היום")]
MAX_PUSHES_PER_RUN = 5
MAIL_LOOKBACK_DAYS = 2

_events_cache = {"at": 0, "events": None}
EVENTS_CACHE_SECONDS = 15 * 60


def is_configured():
    return bool(os.environ.get("MOODLE_ICAL_URL"))


# ---------- Moodle calendar ----------

def _unescape(value):
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_dt(value, params):
    if params.get("VALUE") == "DATE" or len(value) == 8:
        day = datetime.strptime(value[:8], "%Y%m%d")
        # An all-day deadline means the end of that day, Israel time.
        return day.replace(hour=23, minute=59, tzinfo=formatting.ISRAEL_TZ)
    parsed = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.replace(tzinfo=ZoneInfo(params.get("TZID", formatting.DEFAULT_TZ_NAME)))


def parse_ical(text):
    """Return events as dicts: uid, title, course, due (aware datetime), description."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]  # folded line continues the previous one
        else:
            lines.append(raw)

    events, current = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            if current.get("uid") and current.get("due"):
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *raw_params = head.split(";")
            params = dict(p.split("=", 1) for p in raw_params if "=" in p)
            if name == "UID":
                current["uid"] = value
            elif name == "SUMMARY":
                current["title"] = _unescape(value)
            elif name == "CATEGORIES":
                current["course"] = _unescape(value)
            elif name == "DESCRIPTION":
                current["description"] = _unescape(value)
            elif name == "DTSTART":
                current["due"] = _parse_dt(value, params)
    return events


def get_upcoming(days=60):
    """Upcoming Moodle events, soonest first (cached for 15 minutes)."""
    if _events_cache["events"] is None or time.time() - _events_cache["at"] > EVENTS_CACHE_SECONDS:
        response = requests.get(os.environ["MOODLE_ICAL_URL"], timeout=20)
        response.raise_for_status()
        if "BEGIN:VCALENDAR" not in response.text:
            raise ValueError("Moodle did not return a calendar (the export link may have expired)")
        _events_cache.update(at=time.time(), events=parse_ical(response.text))
    now = datetime.now(timezone.utc)
    upcoming = [e for e in _events_cache["events"] if now <= e["due"] <= now + timedelta(days=days)]
    return sorted(upcoming, key=lambda e: e["due"])


def format_due(due):
    local = due.astimezone(formatting.ISRAEL_TZ)
    days = (local.date() - formatting.today_in_tz()).days
    when = {0: "היום", 1: "מחר"}.get(days, local.strftime("%d/%m"))
    return f"{when} {local.strftime('%H:%M')}"


# ---------- Sent-alert log (Supabase) ----------

def _already_sent(keys):
    if not keys:
        return set()
    quoted = ",".join('"' + k.replace('"', "") + '"' for k in keys)
    rows = push_agent.rest("GET", ALERTS_TABLE, params={"select": "key", "key": f"in.({quoted})"})
    return {row["key"] for row in rows}


def _mark_sent(keys):
    if keys:
        push_agent.rest(
            "POST", ALERTS_TABLE,
            params={"on_conflict": "key"},
            payload=[{"key": k} for k in keys],
            prefer="resolution=ignore-duplicates",
        )


# ---------- Mail ----------

def _decode_part(part):
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data)
    charset = "utf-8"
    for header in part.get("headers", []):
        match = re.search(r'charset="?([\w-]+)', header["value"]) if header["name"].lower() == "content-type" else None
        if match:
            charset = match.group(1).lower()
    # Uni mails use iso-8859-8-i, which Python only knows as iso-8859-8.
    charset = {"iso-8859-8-i": "iso-8859-8", "windows-1255": "cp1255"}.get(charset, charset)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _mail_text(payload):
    plain, rich = [], []
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(part.get("parts", []))
        if part.get("mimeType") == "text/plain":
            plain.append(_decode_part(part))
        elif part.get("mimeType") == "text/html":
            rich.append(_decode_part(part))
    if plain:
        return "\n".join(plain)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(rich), flags=re.S | re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


MAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "alert": {"type": "boolean"},
        "title": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["alert", "title", "body"],
    "additionalProperties": False,
}

MAIL_PROMPT = """You read mails sent to a Reichman University student (business & entrepreneurship).
Decide if this mail needs his attention: a new assignment or homework, a deadline or a changed
deadline, an exam or quiz, a cancelled / moved / online class, a room change, a published grade,
or anything he must do or answer. Newsletters, events, marketing, surveys and general
announcements are NOT alerts.

If it is an alert, write in Hebrew: "title" is the course or sender plus the topic (max 40 chars),
"body" is what is needed and by when (max 120 chars). Otherwise return alert=false and empty strings.

From: {sender}
Subject: {subject}
Date: {date}

{text}"""


def classify_mail(sender, subject, sent_at, text):
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=1024,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": MAIL_SCHEMA}},
        messages=[{"role": "user", "content": MAIL_PROMPT.format(
            sender=sender, subject=subject, date=sent_at, text=text[:6000]
        )}],
    )
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def _new_mail_alerts():
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("gmail", "v1", credentials=creds)
    listing = service.users().messages().list(
        userId="me", q=f"to:{push_agent.UNI_ADDRESS} newer_than:{MAIL_LOOKBACK_DAYS}d", maxResults=30
    ).execute()
    ids = [m["id"] for m in listing.get("messages", [])]
    sent = _already_sent([f"mail:{i}" for i in ids])

    alerts, checked = [], []
    for message_id in ids:
        key = f"mail:{message_id}"
        if key in sent:
            continue
        message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
        try:
            verdict = classify_mail(
                headers.get("from", ""), headers.get("subject", ""), headers.get("date", ""),
                _mail_text(message["payload"]) or message.get("snippet", ""),
            )
        except Exception as e:
            # Leave it unmarked so the next run tries again.
            print(f"[ERROR] uni mail classify {message_id}: {e}", file=sys.stderr)
            continue
        checked.append(key)
        if verdict["alert"]:
            alerts.append({
                "key": key,
                "title": "📬 " + verdict["title"],
                "body": verdict["body"],
                "url": f"https://mail.google.com/mail/u/0/#all/{message_id}",
            })
    return alerts, checked


# ---------- Moodle alerts ----------

def _moodle_alerts():
    events = get_upcoming()
    now = datetime.now(timezone.utc)
    candidates = {}
    for event in events:
        left = event["due"] - now
        course = f" · {event['course']}" if event.get("course") else ""
        base = {"url": MOODLE_URL}
        candidates[f"ical:{event['uid']}:new"] = event, left <= NEW_EVENT_WINDOW, {
            **base,
            "title": "📚 מטלה חדשה" + course,
            "body": f"{event.get('title', '')} — עד {format_due(event['due'])}",
        }
        for name, window, label in REMINDERS:
            candidates[f"ical:{event['uid']}:{name}"] = event, left <= window, {
                **base,
                "title": f"⏰ {label}" + course,
                "body": f"{event.get('title', '')} — עד {format_due(event['due'])}",
            }

    sent = _already_sent(list(candidates))
    alerts, quiet = [], []
    for event in events:
        due_keys = [k for k in candidates if k.startswith(f"ical:{event['uid']}:") and candidates[k][1]]
        fresh = [k for k in due_keys if k not in sent]
        if not fresh:
            continue
        # One push per event per run: the most urgent reminder wins, the rest are marked as done.
        chosen = fresh[-1]
        alerts.append({"key": chosen, **candidates[chosen][2]})
        quiet.extend(k for k in fresh if k != chosen)
    return alerts, quiet


# ---------- Scheduled run ----------

def run_check():
    alerts, done = [], []
    errors = []
    if is_configured():
        try:
            found, quiet = _moodle_alerts()
            alerts += found
            done += quiet
        except Exception as e:
            print(f"[ERROR] uni moodle: {e}", file=sys.stderr)
            errors.append("moodle")
    try:
        found, checked = _new_mail_alerts()
        alerts += found
        done += checked
    except Exception as e:
        print(f"[ERROR] uni mail: {e}", file=sys.stderr)
        errors.append("mail")

    pushed = 0
    for alert in alerts[:MAX_PUSHES_PER_RUN]:
        push_agent.send_to_all(alert["title"], alert["body"], alert["url"])
        pushed += 1
    if len(alerts) > MAX_PUSHES_PER_RUN:
        push_agent.send_to_all(
            "📚 עוד עדכונים מהלימודים",
            f"יש עוד {len(alerts) - MAX_PUSHES_PER_RUN} עדכונים. הכול בכרטיס הלימודים ב-Shaked Hub.",
            "/",
        )
        pushed += 1
    _mark_sent(done + [a["key"] for a in alerts])
    return {"ok": not errors, "errors": errors, "alerts": len(alerts), "pushed": pushed}
