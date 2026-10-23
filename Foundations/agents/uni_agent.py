"""University alerts: Moodle deadlines (calendar export) and every mail to the uni address.

Moodle: Calendar → Import or export calendars → Export → "All courses" + "Recent and next 60 days"
→ Get calendar URL. That URL (with its personal token) goes in MOODLE_ICAL_URL.

Mail: every forwarded mail to the uni address (newsletters included) is read once by Claude,
which writes a short Hebrew push and pulls out a deadline when there is one.

Anything with a deadline becomes a task in the Supabase table uni_tasks. Tasks get a push when
they appear, halfway to the deadline, two days before, on the day, and once after it if they were
not marked done on the dashboard. Every push sent is logged in uni_alerts, so nothing repeats
(see garmin/uni_schema.sql).
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import anthropic
import requests
from dotenv import load_dotenv
from googleapiclient.discovery import build

from agents import formatting, gmail_agent, google_auth, push_agent

load_dotenv()

TASKS_TABLE = "uni_tasks"
ALERTS_TABLE = "uni_alerts"
MOODLE_URL = os.environ.get("MOODLE_URL", "https://moodle.runi.ac.il/my/")
DASHBOARD_URL = "/#uni"
MAX_PUSHES_PER_RUN = 5
MAIL_LOOKBACK_DAYS = 2
MIDPOINT_MIN_LEAD = timedelta(days=5)
OVERDUE_GRACE = timedelta(hours=2)
OVERDUE_WINDOW = timedelta(days=3)
# A reminder the checker missed by more than this (the server was down) is skipped, not sent late.
STALE_AFTER = timedelta(hours=6)

KIND_ICONS = {
    "assignment": "📚", "exam": "📝", "cancellation": "🚫", "grade": "🎓",
    "newsletter": "📰", "other": "📬", "moodle": "📚",
}

_events_cache = {"at": 0, "events": None}
EVENTS_CACHE_SECONDS = 5 * 60


def is_configured():
    return bool(os.environ.get("MOODLE_ICAL_URL"))


def tasks_configured():
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SECRET_KEY"))


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
    """Upcoming Moodle events, soonest first (cached for 5 minutes)."""
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
    when = {0: "היום", 1: "מחר", -1: "אתמול"}.get(days, local.strftime("%d/%m"))
    return f"{when} {local.strftime('%H:%M')}"


def _parse_ts(value):
    return datetime.fromisoformat(value) if value else None


# ---------- Supabase tables ----------

def _in_filter(keys):
    return "in.(" + ",".join('"' + k.replace('"', "") + '"' for k in keys) + ")"


def _already_sent(keys):
    if not keys:
        return set()
    sent = set()
    keys = list(keys)
    for start in range(0, len(keys), 100):
        rows = push_agent.rest("GET", ALERTS_TABLE, params={"select": "key", "key": _in_filter(keys[start:start + 100])})
        sent.update(row["key"] for row in rows)
    return sent


def _mark_sent(keys):
    if keys:
        push_agent.rest(
            "POST", ALERTS_TABLE,
            params={"on_conflict": "key"},
            payload=[{"key": k} for k in sorted(set(keys))],
            prefer="resolution=ignore-duplicates",
        )


def _get_tasks(keys):
    if not keys:
        return {}
    rows = push_agent.rest("GET", TASKS_TABLE, params={"select": "*", "key": _in_filter(keys)})
    return {row["key"]: row for row in rows}


def _save_tasks(tasks):
    """Insert or update tasks. Only the listed columns change, so done/first_seen survive."""
    if tasks:
        push_agent.rest(
            "POST", TASKS_TABLE,
            params={"on_conflict": "key"},
            payload=tasks,
            prefer="resolution=merge-duplicates",
        )


def list_tasks():
    """Open tasks plus the ones marked done in the last week, soonest deadline first."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = push_agent.rest("GET", TASKS_TABLE, params={
        "select": "*",
        "or": f'(done.is.false,done_at.gte."{since}")',
        "order": "due.asc.nullslast",
        "limit": "200",
    })
    for row in rows:
        row["due"] = _parse_ts(row.get("due"))
    return rows


def set_done(key, done):
    push_agent.rest(
        "PATCH", TASKS_TABLE,
        params={"key": f"eq.{key}"},
        payload={"done": done, "done_at": datetime.now(timezone.utc).isoformat() if done else None},
    )


# ---------- Mail ----------

MAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["assignment", "exam", "cancellation", "grade", "newsletter", "other"]},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "due": {"type": "string"},
    },
    "required": ["kind", "title", "body", "due"],
    "additionalProperties": False,
}

MAIL_PROMPT = """You read mails sent to a Reichman University student (business & entrepreneurship)
and turn each one into a phone notification.

"kind": assignment (homework, submission, project), exam (exam or quiz), cancellation (class
cancelled, moved, online, room change), grade (grade published), newsletter (newsletters, events,
announcements, surveys), or other.
"title": in Hebrew, the course or sender plus the topic, max 40 characters.
"body": in Hebrew, the one thing he needs to know or do, and by when, max 120 characters.
"due": the deadline or exam time as YYYY-MM-DDTHH:MM in Israel time (YYYY-MM-DD when there is no
hour), only for assignment or exam; otherwise an empty string. The mail was sent on {date}.

From: {sender}
Subject: {subject}

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


def _parse_mail_due(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if len(value) == 10:
        parsed = parsed.replace(hour=23, minute=59)
    return parsed.replace(tzinfo=formatting.ISRAEL_TZ) if parsed.tzinfo is None else parsed


def _check_mail():
    """Push for every new uni mail. Returns (alerts, processed keys, new tasks)."""
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("gmail", "v1", credentials=creds)
    listing = service.users().messages().list(
        userId="me", q=f"to:{push_agent.UNI_ADDRESS} newer_than:{MAIL_LOOKBACK_DAYS}d", maxResults=30
    ).execute()
    ids = [m["id"] for m in listing.get("messages", [])]
    sent = _already_sent([f"mail:{i}" for i in ids])

    alerts, processed, tasks = [], [], []
    for message_id in reversed(ids):  # oldest first
        key = f"mail:{message_id}"
        if key in sent:
            continue
        message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
        try:
            verdict = classify_mail(
                headers.get("from", ""), headers.get("subject", ""), headers.get("date", ""),
                gmail_agent.mail_text(message["payload"]) or message.get("snippet", ""),
            )
        except Exception as e:
            # Leave it unmarked so the next run tries again.
            print(f"[ERROR] uni mail classify {message_id}: {e}", file=sys.stderr)
            continue
        processed.append(key)
        url = f"/mail/{message_id}"
        due = _parse_mail_due(verdict["due"]) if verdict["kind"] in ("assignment", "exam") else None
        if due and due > datetime.now(timezone.utc):
            tasks.append({
                "key": key, "source": "mail", "kind": verdict["kind"], "title": verdict["title"],
                "course": None, "due": due.isoformat(), "url": url,
            })
            url = DASHBOARD_URL
        alerts.append({
            "key": key,
            "title": f"{KIND_ICONS[verdict['kind']]} {verdict['title']}",
            "body": verdict["body"],
            "url": url,
        })
    return alerts, processed, tasks


# ---------- Moodle ----------

def _check_moodle():
    """New Moodle deadlines and changed due dates. Returns (alerts, tasks to save)."""
    events = get_upcoming()
    known = _get_tasks([f"ical:{e['uid']}" for e in events])
    alerts, tasks = [], []
    for event in events:
        key = f"ical:{event['uid']}"
        course = f" · {event['course']}" if event.get("course") else ""
        text = f"{event.get('title', '')} — עד {format_due(event['due'])}"
        old = known.get(key)
        if old is None:
            alerts.append({"key": f"{key}:new", "title": "📚 חדש במודל" + course, "body": text, "url": DASHBOARD_URL})
        elif _parse_ts(old["due"]) != event["due"]:
            alerts.append({
                "key": f"{key}:moved:{event['due'].isoformat()}",
                "title": "🔁 המועד השתנה" + course, "body": text, "url": DASHBOARD_URL,
            })
        tasks.append({
            "key": key, "source": "moodle", "kind": "moodle", "title": event.get("title", ""),
            "course": event.get("course"), "due": event["due"].isoformat(), "url": MOODLE_URL,
        })
    return alerts, tasks


# ---------- Reminders ----------

def _stages(task, now):
    """Reminder stages of an open task whose time has come, in order: (name, time, title)."""
    due, first_seen = task["due"], task["first_seen"]
    lead = due - first_seen
    stages = []
    if lead >= MIDPOINT_MIN_LEAD:
        stages.append(("mid", first_seen + lead / 2, f"⏳ עבר חצי מהזמן · נשארו {(due - now).days} ימים"))
    stages.append(("48h", due - timedelta(hours=48), "⏰ בעוד יומיים"))
    stages.append(("12h", due - timedelta(hours=12), "⏰ היום"))
    if now - due <= OVERDUE_WINDOW:
        stages.append(("over", due + OVERDUE_GRACE, "⚠️ עבר המועד, לא סומן כבוצע"))
    return [stage for stage in stages if stage[1] <= now]


def _reminders(fresh_keys):
    """One push per open task per run (the latest stage wins). Returns (alerts, quiet keys)."""
    now = datetime.now(timezone.utc)
    tasks = [t for t in list_tasks() if not t["done"] and t["due"]]
    candidates = {}
    for task in tasks:
        task["first_seen"] = _parse_ts(task["first_seen"])
        due_tag = task["due"].isoformat()
        candidates[task["key"]] = [(f"{task['key']}:{name}:{due_tag}", at, title) for name, at, title in _stages(task, now)]
    sent = _already_sent([k for stages in candidates.values() for k, _, _ in stages])

    alerts, quiet = [], []
    for task in tasks:
        pending = [stage for stage in candidates[task["key"]] if stage[0] not in sent]
        if not pending:
            continue
        key, at, title = pending[-1]
        if task["key"] in fresh_keys or now - at > STALE_AFTER:
            # Just announced or changed this run, or missed while the server was down: skip.
            quiet.extend(k for k, _, _ in pending)
            continue
        quiet.extend(k for k, _, _ in pending[:-1])
        course = f" · {task['course']}" if task.get("course") else ""
        alerts.append({
            "key": key, "title": title + course,
            "body": f"{task['title']} — עד {format_due(task['due'])}. לסמן בוצע בדשבורד.",
            "url": DASHBOARD_URL,
        })
    return alerts, quiet


# ---------- Scheduled run ----------

def run_check():
    alerts, done, errors = [], [], []
    fresh = set()
    if is_configured():
        try:
            found, tasks = _check_moodle()
            _save_tasks(tasks)
            alerts += found
            fresh.update(a["key"].split(":new")[0].split(":moved:")[0] for a in found)
        except Exception as e:
            print(f"[ERROR] uni moodle: {e}", file=sys.stderr)
            errors.append("moodle")
    try:
        found, processed, tasks = _check_mail()
        _save_tasks(tasks)
        alerts += found
        done += processed
        fresh.update(t["key"] for t in tasks)
    except Exception as e:
        print(f"[ERROR] uni mail: {e}", file=sys.stderr)
        errors.append("mail")
    try:
        found, quiet = _reminders(fresh)
        alerts += found
        done += quiet
    except Exception as e:
        print(f"[ERROR] uni reminders: {e}", file=sys.stderr)
        errors.append("reminders")

    for alert in alerts[:MAX_PUSHES_PER_RUN]:
        push_agent.send_to_all(alert["title"], alert["body"], alert["url"])
    if len(alerts) > MAX_PUSHES_PER_RUN:
        push_agent.send_to_all(
            "📚 עוד עדכונים מהלימודים",
            f"יש עוד {len(alerts) - MAX_PUSHES_PER_RUN} עדכונים. הכול בכרטיס הלימודים ב-Shaked Hub.",
            DASHBOARD_URL,
        )
    _mark_sent(done + [a["key"] for a in alerts])
    return {"ok": not errors, "errors": errors, "alerts": len(alerts), "setup": setup_status()}


def setup_status():
    """What is configured, so a scheduled run's response shows what still needs fixing."""
    status = {"moodle_link": is_configured()}
    try:
        anthropic.Anthropic().models.retrieve("claude-opus-5-5")
        status["anthropic_key"] = "ok"
    except anthropic.AuthenticationError:
        status["anthropic_key"] = "invalid"
    except Exception as e:
        status["anthropic_key"] = f"error: {type(e).__name__}"
    try:
        status["push_devices"] = len(push_agent.rest("GET", push_agent.TABLE, params={"select": "endpoint"}))
    except Exception as e:
        status["push_devices"] = f"error: {type(e).__name__}"
    return status
