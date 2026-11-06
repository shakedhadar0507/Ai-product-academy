import html
import json
import os
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    get_flashed_messages,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session,
    url_for,
)
from googleapiclient.discovery import build
import requests

from agents import (
    action_agent,
    brief_agent,
    calendar_agent,
    formatting,
    garmin_agent,
    garmin_sync_agent,
    gmail_agent,
    google_auth,
    push_agent,
    uni_agent,
    voice_agent,
    weather_agent,
)

load_dotenv()

app = Flask(__name__)
# A stable SECRET_KEY keeps the login cookie valid across restarts; without it
# every restart (or every Gunicorn worker) signs cookies with a different key.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    # Static files carry a version in their link (app.css?v=3), so phones may keep them a year.
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=365),
)

# The home-screen app can't send a Basic Auth header, so login is a form that
# sets a 30-day session cookie. The PWA files must load before login.
# The scheduled check authenticates with CRON_SECRET instead.
PUBLIC_PATHS = {
    "/login", "/manifest.webmanifest", "/sw.js", "/privacy", "/healthz",
    "/tasks/check-uni-mail", "/tasks/check-uni", "/tasks/status",
}


def credentials_valid(given_username, given_password):
    username = os.environ.get("DASHBOARD_USERNAME")
    password = os.environ.get("DASHBOARD_PASSWORD")
    return bool(
        username
        and password
        and secrets.compare_digest(given_username or "", username)
        and secrets.compare_digest(given_password or "", password)
    )


@app.before_request
def require_login():
    if request.path in PUBLIC_PATHS or request.path.startswith(("/static/", "/lang/")):
        return None
    if session.get("authenticated"):
        return None
    auth = request.authorization
    if auth and credentials_valid(auth.username, auth.password):
        return None
    if request.method == "GET":
        return redirect(url_for("login"))
    return Response("Authentication required", 401)


@app.route("/login", methods=["GET", "POST"])
def login():
    error_html = ""
    lang = get_lang()
    if request.method == "POST":
        if credentials_valid(request.form.get("username"), request.form.get("password")):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error_html = f"<p class='text-sm text-rose-500'>{tr(lang, 'wrong_login')}</p>"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}" dir="{tr(lang, 'dir')}">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>{tr(lang, 'sign_in')} · Shaked Hub</title>
      {PWA_HEAD_TAGS}
      <link rel="stylesheet" href="/static/app.css?v=3">
    </head>
    <body class="bg-slate-100 min-h-screen flex items-center justify-center p-4">
      <form method="POST" class="bg-white rounded-2xl shadow-md p-8 w-full max-w-sm flex flex-col gap-3">
        <div class="flex items-center justify-between">
          <h1 class="text-xl font-bold text-slate-800">📊 Shaked Hub</h1>
          <a href="/lang/{tr(lang, 'other')}?next=/login" class="text-xs font-semibold text-indigo-600">{tr(lang, 'other_label')}</a>
        </div>
        {error_html}
        <input name="username" autocomplete="username" placeholder="{tr(lang, 'username')}" required
               class="border border-slate-200 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
        <input name="password" type="password" autocomplete="current-password" placeholder="{tr(lang, 'password')}" required
               class="border border-slate-200 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
        <button type="submit"
                class="bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg px-4 py-2.5 text-sm font-medium transition-colors">
          {tr(lang, 'sign_in')}
        </button>
      </form>
    </body>
    </html>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


PRIVACY_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shaked Hub - Privacy</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; line-height: 1.5;">
<h1>Shaked Hub privacy policy</h1>
<p>Shaked Hub is a personal dashboard used only by its owner. It reads the owner's Google Calendar and Gmail
to show events and unread mail, and can create calendar events and email drafts when the owner asks.</p>
<p>Data is used only to display this dashboard. It is not sold, shared or used for advertising, and nothing is
stored beyond short-lived caches and an access token kept on the server.</p>
<p>Access can be revoked at any time from <a href="https://myaccount.google.com/permissions">Google account permissions</a>.</p>
</body></html>"""


@app.route("/privacy")
def privacy():
    return PRIVACY_PAGE


@app.route("/manifest.webmanifest")
def manifest():
    response = send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/sw.js")
def service_worker():
    # Served from the root so the worker's scope covers the whole app.
    response = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


PWA_HEAD_TAGS = """
      <link rel="manifest" href="/manifest.webmanifest">
      <meta name="theme-color" content="#4f46e5">
      <link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
      <meta name="apple-mobile-web-app-capable" content="yes">
      <meta name="mobile-web-app-capable" content="yes">
      <meta name="apple-mobile-web-app-title" content="Shaked Hub">
      <meta name="apple-mobile-web-app-status-bar-style" content="default">
      <script>
        if ('serviceWorker' in navigator) {
          window.addEventListener('load', function() { navigator.serviceWorker.register('/sw.js'); });
        }
      </script>
"""

LATITUDE = 32.08
LONGITUDE = 34.78
MARATHON_DATE = date(2026, 11, 1)
USER_TZ_COOKIE = "user_tz"


def get_header(headers, name):
    for header in headers:
        if header["name"].lower() == name.lower():
            return header["value"]
    return "(unknown)"


# (text key, Gmail search). RFS is no longer shown (not relevant any more).
EMAIL_LABEL_BUCKETS = [
    ("studies", "label:לימודים is:unread"),
    ("finance", "label:פיננסים is:unread"),
]
OTHER_EMAIL_QUERY = "in:inbox is:unread category:primary -label:לימודים -label:RFS -label:פיננסים"


def get_email_bucket(service, query, sample_size=3):
    """(count, newest few) for a Gmail search. Gmail's estimate is used for the count, so a
    big label costs one call instead of paging through every message."""
    response = service.users().messages().list(userId="me", q=query, maxResults=sample_size).execute()
    messages = response.get("messages", [])
    count = response.get("resultSizeEstimate", len(messages)) if messages else 0
    samples = []
    for message in messages:
        detail = service.users().messages().get(
            userId="me", id=message["id"], format="metadata", metadataHeaders=["Subject", "From"]
        ).execute()
        headers = detail["payload"]["headers"]
        samples.append({
            "id": message["id"],
            "subject": get_header(headers, "Subject"),
            "from": get_header(headers, "From"),
            "snippet": html.unescape(detail.get("snippet", "")),
        })
    return max(count, len(samples)), samples


LANG_COOKIE = "hub_lang"

# Every text the app shows, in Hebrew and English. The language comes from the hub_lang cookie
# (the עב / EN button in the header) and Hebrew pages are right-to-left.
STRINGS = {
    "he": {
        "dir": "rtl", "other": "en", "other_label": "EN", "sign_out": "התנתק", "refresh": "רענון",
        "q_calendar": "יומן", "q_event": "אירוע", "q_gmail": "Gmail", "q_moodle": "Moodle", "q_garmin": "Garmin",
        "ask_title": "שאל את Claude",
        "ask_sub": "כתוב או הקלט בקשה: לקבוע אירוע ביומן, לנסח טיוטת מייל ועוד.",
        "ask_placeholder": "למשל: תקבע רופא שיניים מחר 15:00-16:00", "send": "שלח",
        "loading": "טוען נתונים…", "saved": "(שמור)",
        "brief": "התקציר היומי", "brief_error": "לא הצלחתי ליצור את התקציר היומי",
        "calendar": "היומן היום", "open_calendar": "פתח יומן", "no_events": "אין אירועים היום.",
        "ai_insight": "תובנת AI", "open_in_calendar": "פתח ב-Google Calendar", "all_day": "כל היום",
        "calendar_error": "לא הצלחתי לטעון את היומן",
        "uni": "מטלות לימודים", "no_tasks": "אין מטלות פתוחות.", "overdue": "לא בוצע", "done": "✓ בוצע", "undo": "בטל",
        "uni_error": "לא הצלחתי לטעון את המטלות",
        "inbox": "מיילים", "open_gmail": "פתח Gmail", "no_mail": "אין מיילים", "open_mail": "פתח מייל",
        "mark_read": "נקרא", "inbox_error": "לא הצלחתי לטעון את המיילים", "other_mail": "כל השאר",
        "studies": "לימודים", "finance": "פיננסים",
        "training": "האימון היום", "todays_plan": "התוכנית להיום", "no_workout": "אין אימון מתוכנן",
        "readiness": "מוכנות", "hrv": "HRV", "sleep": "שינה", "sleep_score": "ציון שינה", "resting_hr": "דופק במנוחה",
        "body_battery": "Body Battery", "night_of": "הלילה של {}", "hrv_status": "מצב HRV: {}",
        "garmin_not_connected": "נתוני גרמין עוד לא מחוברים", "garmin_error": "לא הצלחתי לטעון את נתוני הגרמין",
        "sync_now": "↻ סנכרן עכשיו", "syncing": "מסנכרן…", "synced": "סונכרן {}", "sync_failed": "סנכרון נכשל {}",
        "sync_waiting": "מחכה לסנכרון ראשון", "sync_not_set": "סנכרון מהשרת עוד לא מחובר",
        "volume": "נפח ריצה", "weekly_distance": "קילומטרים בשבוע", "last_run": "ריצה אחרונה",
        "no_runs": "אין ריצות ב-5 השבועות האחרונים.", "km": "ק\"מ",
        "countdown": "ספירה לאחור למרתון", "days_to_go": "ימים", "race_today": "היום המרוץ! 🎉",
        "notifications": "התראות", "push_intro": "קבל התראה במכשיר הזה כשמשהו דורש אותך.",
        "push_enable": "הפעל התראות", "push_on": "ההתראות פועלות במכשיר הזה ✅", "push_test": "שלח התראת בדיקה",
        "push_reconnect": "ההתראות צריכות חיבור מחדש. לחץ כאן פעם אחת.", "push_reenable": "הפעל התראות מחדש",
        "push_home": "באייפון, פתח את Shaked Hub ממסך הבית כדי להפעיל התראות.",
        "push_sent": "ההתראות פועלות ✅ (נשלחה התראת בדיקה)",
        "push_blocked": "ההתראות חסומות. אפשר אותן בהגדרות > עדכונים > Shaked Hub.",
        "push_send_failed": "נשמר, אבל התראת הבדיקה נכשלה. ספר ל-Claude.", "push_failed": "לא הצלחתי להפעיל התראות. נסה שוב.",
        "error": "שגיאה",
        "back": "→ חזרה לדשבורד", "open_in_gmail": "פתח ב-Gmail", "reply_to": "תשובה אל {}", "save_draft": "שמור כטיוטה",
        "confirm_send": "לשלוח את התשובה ל-{}?", "reply_sent": "התשובה אל {} נשלחה.", "reply_drafted": "התשובה אל {} נשמרה כטיוטה ב-Gmail.",
        "reply_failed": "לא הצלחתי לשלוח. נסה שוב או פתח ב-Gmail.",
        "sign_in": "התחברות", "username": "שם משתמש", "password": "סיסמה", "wrong_login": "שם משתמש או סיסמה שגויים.",
        "empty_request": "כתוב בקשה.", "action_failed": "משהו השתבש בביצוע הפעולה.",
    },
    "en": {
        "dir": "ltr", "other": "he", "other_label": "עב", "sign_out": "Sign out", "refresh": "Refresh",
        "q_calendar": "Calendar", "q_event": "Event", "q_gmail": "Gmail", "q_moodle": "Moodle", "q_garmin": "Garmin",
        "ask_title": "Ask Claude",
        "ask_sub": "Type or speak a request: schedule an event, draft an email, and more.",
        "ask_placeholder": "e.g. schedule a dentist appointment tomorrow 15:00-16:00", "send": "Send",
        "loading": "Loading…", "saved": "(saved)",
        "brief": "Daily Brief", "brief_error": "Could not generate the daily brief",
        "calendar": "Today's Calendar", "open_calendar": "Open calendar", "no_events": "No events today.",
        "ai_insight": "AI insight", "open_in_calendar": "Open in Google Calendar", "all_day": "All day",
        "calendar_error": "Could not load the calendar",
        "uni": "University tasks", "no_tasks": "No open tasks.", "overdue": "Missed", "done": "✓ Done", "undo": "Undo",
        "uni_error": "Could not load university tasks",
        "inbox": "Mail", "open_gmail": "Open Gmail", "no_mail": "No mail", "open_mail": "Open mail",
        "mark_read": "Read", "inbox_error": "Could not load mail", "other_mail": "Everything else",
        "studies": "Studies", "finance": "Finance",
        "training": "Today's Training", "todays_plan": "Today's plan", "no_workout": "No workout planned",
        "readiness": "Readiness", "hrv": "HRV", "sleep": "Sleep", "sleep_score": "Sleep score", "resting_hr": "Resting HR",
        "body_battery": "Body Battery", "night_of": "Night of {}", "hrv_status": "HRV status: {}",
        "garmin_not_connected": "Garmin data not connected yet", "garmin_error": "Could not load Garmin data",
        "sync_now": "↻ Sync now", "syncing": "Syncing…", "synced": "Synced {}", "sync_failed": "Sync failed {}",
        "sync_waiting": "Waiting for the first sync", "sync_not_set": "Server sync not connected yet",
        "volume": "Running Volume", "weekly_distance": "Weekly distance", "last_run": "Last run",
        "no_runs": "No runs in the last 5 weeks.", "km": "km",
        "countdown": "Marathon Countdown", "days_to_go": "days to go", "race_today": "Race day is today! 🎉",
        "notifications": "Notifications", "push_intro": "Get an alert on this device when something needs you.",
        "push_enable": "Enable notifications", "push_on": "Notifications are on for this device ✅", "push_test": "Send a test notification",
        "push_reconnect": "Notifications need reconnecting. Tap once.", "push_reenable": "Re-enable notifications",
        "push_home": "On iPhone, open Shaked Hub from the home screen to enable notifications.",
        "push_sent": "Notifications are on ✅ (a test was just sent)",
        "push_blocked": "Notifications are blocked. Allow them in Settings > Notifications > Shaked Hub.",
        "push_send_failed": "Saved, but the test notification failed. Tell Claude.", "push_failed": "Could not enable notifications. Try again.",
        "error": "Error",
        "back": "← Back to dashboard", "open_in_gmail": "Open in Gmail", "reply_to": "Reply to {}", "save_draft": "Save as draft",
        "confirm_send": "Send the reply to {}?", "reply_sent": "Your reply to {} was sent.", "reply_drafted": "Your reply to {} was saved as a Gmail draft.",
        "reply_failed": "Could not send. Try again or open in Gmail.",
        "sign_in": "Sign in", "username": "Username", "password": "Password", "wrong_login": "Wrong username or password.",
        "empty_request": "Please enter a request.", "action_failed": "Something went wrong.",
    },
}


def get_lang():
    lang = request.cookies.get(LANG_COOKIE, "he")
    return lang if lang in STRINGS else "he"


def tr(lang, key, *args):
    text = STRINGS[lang].get(key, key)
    return text.format(*args) if args else text


@app.route("/lang/<lang>")
def set_lang(lang):
    response = redirect(request.args.get("next") or "/")
    if lang in STRINGS:
        response.set_cookie(LANG_COOKIE, lang, max_age=365 * 24 * 3600, samesite="Lax")
    return response


def render_card(icon, title, body_html, link=None, link_text=None, name=None):
    """A dashboard card. `link` adds a tappable button in the header (Gmail, Calendar...)."""
    header_link = (
        f"<a href='{html.escape(link)}' target='_blank' rel='noopener' "
        f"class='shrink-0 text-xs font-medium text-indigo-600 bg-indigo-50 hover:bg-indigo-100 rounded-full px-3 py-1'>{link_text}</a>"
        if link else ""
    )
    data_name = f" data-card='{name}'" if name else ""
    return f"""
    <section{data_name} class="bg-white rounded-2xl shadow-md p-5 flex flex-col gap-3">
      <div class="flex items-center justify-between gap-2">
        <h2 class="flex items-center gap-2 text-lg font-semibold text-slate-800">
          <span class="text-2xl">{icon}</span> {title}
        </h2>
        {header_link}
      </div>
      <div class="text-slate-600">{body_html}</div>
    </section>
    """


def render_error(message):
    return f'<p class="text-sm text-rose-500 italic">{message}</p>'


def render_list(items_html):
    return '<ul class="flex flex-col gap-2">' + "".join(
        f'<li class="border-b border-slate-100 last:border-0 pb-2 last:pb-0">{item}</li>' for item in items_html
    ) + "</ul>"


def render_link(url, text):
    """In-app links open in place; outside links (Gmail, Calendar, Moodle) open in a new tab."""
    target = " target='_blank' rel='noopener'" if url.startswith("http") else ""
    return f"<a href='{html.escape(url)}'{target} class='text-indigo-700 hover:underline'>{text}</a>"


def render_details(summary_html, body_html, extra_class=""):
    """A row that shows one line and opens on tap to show more."""
    return (
        f"<details class='group {extra_class}'>"
        f"<summary class='flex items-center justify-between gap-2 cursor-pointer list-none'>"
        f"<span class='min-w-0 truncate'>{summary_html}</span>"
        f"<span class='shrink-0 text-slate-400 transition-transform group-open:rotate-180'>⌄</span></summary>"
        f"<div class='mt-2 text-sm text-slate-600 flex flex-col gap-2'>{body_html}</div></details>"
    )


GMAIL_URL = "https://mail.google.com/mail/u/0/#inbox"
GMAIL_SEARCH_URL = "https://mail.google.com/mail/u/0/#search/{}"
CALENDAR_URL = "https://calendar.google.com/calendar/r/day"
NEW_EVENT_URL = "https://calendar.google.com/calendar/r/eventedit"
GARMIN_URL = "https://connect.garmin.com/modern/"
BUTTON = "text-xs rounded-lg px-3 py-1.5 font-medium"


def render_email_bucket(lang, name, query, count, samples):
    title = render_link(GMAIL_SEARCH_URL.format(quote(query)), html.escape(name))
    if count == 0:
        body = f"<p class='text-slate-400 text-sm'>{tr(lang, 'no_mail')}</p>"
    else:
        rows = []
        for m in samples:
            details = (
                f"<p class='text-xs text-slate-400'>{html.escape(m['from'])}</p>"
                f"<p dir='auto'>{html.escape(m['snippet'])}</p>"
                f"<div class='flex gap-2'>"
                f"<a href='/mail/{m['id']}' class='{BUTTON} bg-indigo-500 text-white'>{tr(lang, 'open_mail')}</a>"
                f"<button data-mail-action='read' data-id='{m['id']}' class='{BUTTON} bg-slate-100'>{tr(lang, 'mark_read')}</button>"
                f"</div>"
            )
            rows.append(f"<div data-mail='{m['id']}' class='text-sm'>"
                        + render_details(f"<span dir='auto'>{html.escape(m['subject'])}</span>", details) + "</div>")
        body = render_list(rows)
    return f"""
    <div>
      <p class="text-sm font-semibold text-slate-700 mb-1">{title} <span class="text-slate-400 font-normal">({count})</span></p>
      {body}
    </div>
    """


# ---------- cards ----------
# Each card is built on its own (in parallel) and kept in a server cache, so the page opens
# at once with the last version and swaps in fresh cards a few seconds later.

def card_countdown(tz_name, data):
    lang = data["lang"]
    days_remaining = (MARATHON_DATE - formatting.today_in_tz(tz_name)).days
    if days_remaining < 0:
        return ""  # the race is over; the card comes back when a new race date is set
    if days_remaining == 0:
        countdown_html = f"<span class='text-lg font-semibold text-indigo-600'>{tr(lang, 'race_today')}</span>"
    else:
        countdown_html = (
            f"<span class='text-3xl font-bold text-indigo-600'>{days_remaining}</span> "
            f"<span class='text-slate-500'>{tr(lang, 'days_to_go')}</span>"
        )
    return render_card("🏁", tr(lang, "countdown"), countdown_html, name="countdown")


def card_calendar(tz_name, data):
    lang = data["lang"]
    try:
        events = calendar_agent.get_data(tz_name)
        data["calendar"] = events
        insight = calendar_agent.get_insight(events, tz_name)
        if not events:
            events_html = f"<p class='text-slate-400 text-sm'>{tr(lang, 'no_events')}</p>"
        else:
            items = []
            for event in events:
                start, end = event["start"].get("dateTime"), event["end"].get("dateTime")
                time_range = formatting.format_time_range(start, end) if start and end else tr(lang, "all_day")
                title = html.escape(event.get("summary", "(no title)"))
                details = []
                if event.get("location"):
                    details.append(f"<p>📍 {html.escape(event['location'])}</p>")
                if event.get("description"):
                    text = re.sub(r"<[^>]+>", " ", event["description"])
                    details.append(f"<p dir='auto' class='line-clamp-3'>{html.escape(html.unescape(text)[:300])}</p>")
                if event.get("htmlLink"):
                    details.append(
                        f"<a href='{html.escape(event['htmlLink'])}' target='_blank' rel='noopener' "
                        f"class='{BUTTON} bg-indigo-500 text-white self-start'>{tr(lang, 'open_in_calendar')}</a>"
                    )
                items.append(render_details(
                    f"<span class='text-slate-400 tabular-nums'>{time_range}</span> · <span dir='auto'>{title}</span>",
                    "".join(details),
                ))
            events_html = f"<div class='text-sm'>{render_list(items)}</div>"
        insight_html = (
            "<div class='mt-2 bg-indigo-50 rounded-lg px-3 py-2 text-sm text-indigo-900'>"
            + render_details(f"<span class='font-semibold'>✨ {tr(lang, 'ai_insight')}</span>",
                             f"<p dir='auto'>{insight}</p>") + "</div>"
        )
        body = events_html + insight_html
    except Exception as e:
        print(f"[ERROR] calendar: {e}", file=sys.stderr)
        body = render_error(tr(lang, "calendar_error"))
    return render_card("📅", tr(lang, "calendar"), body, CALENDAR_URL, tr(lang, "open_calendar"), name="calendar")


def card_uni(tz_name, data):
    lang = data["lang"]
    if not uni_agent.tasks_configured():
        return ""
    try:
        tasks = uni_agent.list_tasks()
        now = formatting.now_in_tz(tz_name)
        open_tasks = [t for t in tasks if not t["done"]]
        done_tasks = [t for t in tasks if t["done"]]
        if not tasks:
            body = f"<p class='text-slate-400 text-sm'>{tr(lang, 'no_tasks')}</p>"
        else:
            items = []
            for task in open_tasks + done_tasks:
                overdue = not task["done"] and task["due"] and task["due"] < now
                when = uni_agent.format_due(task["due"]) if task["due"] else ""
                if overdue:
                    when = f"<span class='text-rose-600 font-semibold'>{tr(lang, 'overdue')} · {when}</span>"
                else:
                    when = f"<span class='font-semibold text-slate-700'>{when}</span>"
                course = f"<span class='text-slate-400'> · {html.escape(task['course'])}</span>" if task.get("course") else ""
                title = html.escape(task["title"])
                if task["key"].startswith("mail:"):
                    title = render_link("/mail/" + task["key"][5:], title)
                elif task.get("url"):
                    title = render_link(task["url"], title)
                button = tr(lang, "undo") if task["done"] else tr(lang, "done")
                style = "line-through text-slate-400" if task["done"] else ""
                items.append(
                    f"<div class='flex items-start justify-between gap-2 text-sm'>"
                    f"<div dir='auto' class='{style}'>{when} &mdash; {title}{course}</div>"
                    f"<button data-task='{html.escape(task['key'])}' data-done='{'0' if task['done'] else '1'}' "
                    f"class='shrink-0 {BUTTON} bg-slate-100 hover:bg-emerald-100'>{button}</button>"
                    f"</div>"
                )
            body = f"<div id='uni'>{render_list(items)}</div>"
    except Exception as e:
        print(f"[ERROR] uni card: {e}", file=sys.stderr)
        body = render_error(tr(lang, "uni_error"))
    return render_card("📚", tr(lang, "uni"), body, uni_agent.MOODLE_URL, "Moodle", name="uni")


def card_inbox(tz_name, data):
    lang = data["lang"]
    try:
        creds = google_auth.get_credentials(google_auth.SCOPES)
        service = build("gmail", "v1", credentials=creds)
        bucket_data, sections = [], []
        for key, query in EMAIL_LABEL_BUCKETS + [("other_mail", OTHER_EMAIL_QUERY)]:
            count, mails = get_email_bucket(service, query)
            bucket_data.append((key, count, mails))
            sections.append(render_email_bucket(lang, tr(lang, key), query, count, mails))
        data["email"] = bucket_data
        body = "<div class='flex flex-col gap-4'>" + "".join(sections) + "</div>"
    except Exception as e:
        print(f"[ERROR] inbox: {e}", file=sys.stderr)
        body = render_error(tr(lang, "inbox_error"))
    return render_card("📧", tr(lang, "inbox"), body, GMAIL_URL, tr(lang, "open_gmail"), name="inbox")


def _sync_line(lang):
    """When Garmin data last synced from the server, and the sync-now button."""
    state = garmin_sync_agent.status()
    if not garmin_sync_agent.is_configured():
        note = tr(lang, "sync_not_set")
    elif state["running"]:
        note = tr(lang, "syncing")
    elif state["last"]:
        last = state["last"]
        note = tr(lang, "synced" if last.get("ok") else "sync_failed", last["finished_at"])
    else:
        note = tr(lang, "sync_waiting")
    button = (
        f"<button id='garmin-sync' class='{BUTTON} bg-slate-100 hover:bg-indigo-100'>{tr(lang, 'sync_now')}</button>"
        if garmin_sync_agent.is_configured() else ""
    )
    return (
        f"<div class='flex items-center justify-between gap-2 mt-3'>"
        f"<p id='garmin-sync-note' class='text-xs text-slate-400'>{note}</p>{button}</div>"
    )


def card_training(tz_name, data):
    lang = data["lang"]
    today_local = formatting.today_in_tz(tz_name)
    planned = garmin_agent.get_planned_workout(today_local)
    plan_html = (
        f"<p class='text-xs font-semibold text-slate-400 mb-1'>{tr(lang, 'todays_plan')}</p>"
        f"<p dir='auto' class='text-slate-800 font-medium'>{planned or tr(lang, 'no_workout')}</p>"
    )
    garmin_data = None
    if garmin_agent.is_configured():
        try:
            garmin_data = garmin_agent.get_data(today_local)
            data["training"] = garmin_agent.as_training_log(garmin_data["runs"])
        except Exception as e:
            print(f"[ERROR] garmin: {e}", file=sys.stderr)
    data["garmin"] = garmin_data
    if garmin_data is None:
        garmin_html = render_error(
            tr(lang, "garmin_not_connected") if not garmin_agent.is_configured() else tr(lang, "garmin_error")
        )
    else:
        wellness = garmin_data["wellness"] or {}
        stats = [
            (tr(lang, "readiness"), garmin_agent.format_number(wellness.get("training_readiness"))),
            (tr(lang, "hrv"), f"{wellness['hrv_last_night_ms']:.0f} ms" if wellness.get("hrv_last_night_ms") else None),
            (tr(lang, "sleep"), garmin_agent.format_hours(wellness.get("sleep_s")) if wellness.get("sleep_s") else None),
            (tr(lang, "sleep_score"), garmin_agent.format_number(wellness.get("sleep_score"))),
            (tr(lang, "resting_hr"), garmin_agent.format_number(wellness.get("resting_hr"))),
            (tr(lang, "body_battery"), garmin_agent.format_number(wellness.get("body_battery_high"))),
        ]
        stats_html = "".join(
            f"<div class='bg-slate-50 rounded-lg p-2'><p class='text-xs text-slate-400'>{label}</p>"
            f"<p class='font-semibold text-slate-800'>{value if value is not None else '–'}</p></div>"
            for label, value in stats
        )
        hrv_status = wellness.get("hrv_status")
        wellness_date = wellness.get("date")
        caption = " &middot; ".join(
            part for part in [
                tr(lang, "night_of", formatting.format_date(wellness_date)) if wellness_date else None,
                tr(lang, "hrv_status", hrv_status.lower()) if hrv_status else None,
                garmin_agent.format_status(wellness.get("training_status")),
            ] if part
        )
        garmin_html = (
            f"<div class='grid grid-cols-3 gap-2 mt-3'>{stats_html}</div>"
            f"<p class='text-xs text-slate-400 mt-2'>{caption}</p>"
        )
    body = plan_html + garmin_html + _sync_line(lang)
    return render_card("🏃", tr(lang, "training"), body, GARMIN_URL, "Garmin", name="training")


def card_volume(tz_name, data):
    lang = data["lang"]
    garmin_data = data.get("garmin")
    if garmin_data is None:
        return ""
    weeks = garmin_data["weekly_km"]
    max_km = max([km for _, km in weeks] + [1])
    bars = "".join(
        f"<div class='flex items-center gap-2 text-sm'>"
        f"<span class='w-12 text-slate-400'>{week_start.strftime('%d/%m')}</span>"
        f"<div class='flex-1 bg-slate-100 rounded h-3'>"
        f"<div class='bg-indigo-400 h-3 rounded' style='width:{km / max_km * 100:.0f}%'></div></div>"
        f"<span class='w-16 text-end font-medium text-slate-700'>{km} {tr(lang, 'km')}</span></div>"
        for week_start, km in weeks
    )
    runs = garmin_data["runs"]
    if runs:
        last = runs[0]
        hr_html = f" &middot; HR {last['avg_hr']:.0f}" if last.get("avg_hr") else ""
        last_html = (
            f"<p class='text-xs font-semibold text-slate-400 mb-1 mt-4'>{tr(lang, 'last_run')}</p>"
            f"<p class='text-sm'><span class='text-slate-400'>{formatting.format_date(last['start_time_local'][:10])}</span> &mdash; "
            f"<span class='font-medium text-slate-800'>{(last['distance_m'] or 0) / 1000:.1f} {tr(lang, 'km')}</span> "
            f"&middot; {garmin_agent.format_pace(last['avg_pace_s_per_km'])}/km{hr_html}</p>"
        )
    else:
        last_html = f"<p class='text-slate-400 text-sm mt-4'>{tr(lang, 'no_runs')}</p>"
    body = (
        f"<p class='text-xs font-semibold text-slate-400 mb-2'>{tr(lang, 'weekly_distance')}</p>"
        f"<div class='flex flex-col gap-1.5'>{bars}</div>{last_html}"
    )
    return render_card("📈", tr(lang, "volume"), body, GARMIN_URL, "Garmin", name="volume")


def card_brief(tz_name, data):
    lang = data["lang"]
    try:
        brief = brief_agent.get_insight(
            data.get("calendar"), None, data.get("email"), data.get("training"), tz_name
        )
        plain = re.sub(r"<[^>]+>", "", brief)
        preview = html.escape(plain[:90] + ("…" if len(plain) > 90 else ""))
        brief_body = (
            "<details class='group'>"
            "<summary class='flex items-center justify-between gap-3 cursor-pointer list-none'>"
            f"<span class='flex items-center gap-2 font-bold'><span class='text-2xl'>🧭</span> {tr(lang, 'brief')}</span>"
            "<span class='shrink-0 text-xl transition-transform group-open:rotate-180'>⌄</span></summary>"
            f"<p dir='auto' class='text-sm text-indigo-100 mt-1 group-open:hidden'>{preview}</p>"
            f"<p dir='auto' class='leading-relaxed mt-3'>{brief}</p></details>"
        )
    except Exception as e:
        print(f"[ERROR] daily_brief: {e}", file=sys.stderr)
        brief_body = f"<p class='italic text-indigo-100'>🧭 {tr(lang, 'brief_error')}</p>"
    return f"""
    <section data-card="brief" class="md:col-span-2 lg:col-span-3 bg-gradient-to-r from-indigo-600 to-purple-600 text-white rounded-2xl shadow-lg p-5">
      {brief_body}
    </section>
    """


# Page order. The brief needs the other cards' data, so it is built last.
CARD_ORDER = ["brief", "countdown", "calendar", "uni", "inbox", "training", "volume"]
PARALLEL_CARDS = {
    "countdown": card_countdown, "calendar": card_calendar,
    "uni": card_uni, "inbox": card_inbox, "training": card_training,
}
CARDS_FRESH_SECONDS = 120
_cards = {"lock": threading.Lock(), "by_key": {}, "building": set()}
_pool = ThreadPoolExecutor(max_workers=8)


def build_cards(tz_name, lang="he"):
    data = {"lang": lang}
    futures = {name: _pool.submit(fn, tz_name, data) for name, fn in PARALLEL_CARDS.items()}
    cards = {}
    for name, future in futures.items():
        try:
            cards[name] = future.result(timeout=45)
        except Exception as e:
            print(f"[ERROR] card {name}: {type(e).__name__}", file=sys.stderr)
            cards[name] = ""
    cards["volume"] = card_volume(tz_name, data)
    cards["brief"] = card_brief(tz_name, data)
    entry = {"at": time.time(), "cards": cards}
    with _cards["lock"]:
        _cards["by_key"][(tz_name, lang)] = entry
    return entry


def cached_cards(tz_name, lang="he"):
    with _cards["lock"]:
        return _cards["by_key"].get((tz_name, lang))


def refresh_cards_async(tz_name, lang="he"):
    key = (tz_name, lang)
    with _cards["lock"]:
        if key in _cards["building"]:
            return
        _cards["building"].add(key)

    def work():
        try:
            build_cards(tz_name, lang)
        finally:
            with _cards["lock"]:
                _cards["building"].discard(key)

    _pool.submit(work)


def invalidate_cards():
    with _cards["lock"]:
        for entry in _cards["by_key"].values():
            entry["at"] = 0


def cards_html(entry):
    return "".join(entry["cards"].get(name, "") for name in CARD_ORDER)


def _tz():
    return formatting.resolve_tz_name(request.cookies.get(USER_TZ_COOKIE, formatting.DEFAULT_TZ_NAME))


@app.route("/api/cards")
def api_cards():
    """Fresh cards as HTML. ?wait=1 builds them now when the cache is old (used right after open)."""
    tz_name, lang = _tz(), get_lang()
    entry = cached_cards(tz_name, lang)
    if entry is None or (request.args.get("wait") and time.time() - entry["at"] > CARDS_FRESH_SECONDS):
        entry = build_cards(tz_name, lang)
    return jsonify({"html": cards_html(entry), "at": entry["at"], "updated": _updated_text(entry["at"], tz_name)})


def _updated_text(timestamp, tz_name):
    return datetime.fromtimestamp(timestamp, ZoneInfo(tz_name)).strftime("%H:%M")


QUICK_LINKS = [
    ("📅", "q_calendar", CALENDAR_URL),
    ("＋", "q_event", NEW_EVENT_URL),
    ("✉️", "q_gmail", GMAIL_URL),
    ("📚", "q_moodle", None),
    ("⌚", "q_garmin", GARMIN_URL),
]


@app.route("/")
def dashboard():
    tz_name, lang = _tz(), get_lang()
    entry = cached_cards(tz_name, lang)
    if entry is None:
        cards_block = (
            "<section class='md:col-span-2 lg:col-span-3 bg-white rounded-2xl shadow-md p-6 text-slate-400 text-sm'>"
            f"{tr(lang, 'loading')}</section>"
        )
        stale, updated = True, ""
    else:
        cards_block = cards_html(entry)
        stale = time.time() - entry["at"] > CARDS_FRESH_SECONDS
        updated = _updated_text(entry["at"], tz_name)

    action_messages = get_flashed_messages()
    action_result_html = (
        f"<div dir='auto' class='bg-emerald-50 border-s-4 border-emerald-400 rounded-e-lg p-3 text-sm text-emerald-900'>{action_messages[0]}</div>"
        if action_messages else ""
    )

    if voice_agent.is_configured():
        mic_disabled_attr = ""
        mic_extra_class = "bg-slate-100 hover:bg-indigo-100 text-slate-600 hover:text-indigo-600 cursor-pointer"
    else:
        mic_disabled_attr = "disabled"
        mic_extra_class = "bg-slate-50 text-slate-300 cursor-not-allowed"

    quick_links = "".join(
        f"<a href='{html.escape(url or uni_agent.MOODLE_URL)}' target='_blank' rel='noopener' "
        f"class='flex flex-col items-center gap-1 bg-white rounded-2xl shadow-sm py-3 active:scale-95 transition-transform'>"
        f"<span class='text-2xl'>{icon}</span><span class='text-xs text-slate-600'>{tr(lang, key)}</span></a>"
        for icon, key, url in QUICK_LINKS
    )

    push_card = ""
    if push_agent.is_configured():
        try:
            key = push_agent.public_key()
            push_card = render_card("🔔", tr(lang, "notifications"), (
                f"<p id='push-status' class='text-sm text-slate-500'>{tr(lang, 'push_intro')}</p>"
                f"<button id='push-button' data-key='{key}' "
                "class='mt-3 bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg px-4 py-2 text-sm font-medium'>"
                f"{tr(lang, 'push_enable')}</button>"
            ))
        except Exception as e:
            print(f"[ERROR] push key: {type(e).__name__}", file=sys.stderr)

    action_card = f"""
    <section class="md:col-span-2 lg:col-span-3 bg-white rounded-2xl shadow-md p-5 flex flex-col gap-3">
      <div>
        <h2 class="flex items-center gap-2 text-lg font-semibold text-slate-800">
          <span class="text-2xl">🤖</span> {tr(lang, 'ask_title')}
        </h2>
        <p class="text-sm text-slate-400 mt-0.5">{tr(lang, 'ask_sub')}</p>
      </div>
      {action_result_html}
      <form method="POST" action="/agent-action" class="flex flex-col sm:flex-row gap-2">
        <input type="text" name="user_text" id="agent-text-input" required dir="auto"
               placeholder="{tr(lang, 'ask_placeholder')}"
               class="flex-1 border border-slate-200 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
        <div class="flex gap-2">
          <button type="button" id="mic-button" {mic_disabled_attr}
                  class="w-11 h-11 shrink-0 flex items-center justify-center rounded-full text-lg transition-all duration-200 active:scale-90 {mic_extra_class}">
            🎤
          </button>
          <button type="submit"
                  class="flex-1 sm:flex-none bg-indigo-500 hover:bg-indigo-600 active:scale-95 text-white rounded-lg px-5 py-2.5 text-sm font-medium">
            {tr(lang, 'send')}
          </button>
        </div>
      </form>
    </section>
    """

    now_local = formatting.now_in_tz(tz_name)
    header_subtitle = now_local.strftime("%d/%m/%Y")
    js_strings = {k: tr(lang, k) for k in [
        "push_on", "push_test", "push_reconnect", "push_reenable", "push_home", "push_sent",
        "push_blocked", "push_send_failed", "push_failed", "error", "syncing", "saved",
    ]}

    page = DASHBOARD_PAGE
    for marker, value in {
        "{{HEAD}}": PWA_HEAD_TAGS,
        "{{LANG}}": lang,
        "{{DIR}}": tr(lang, "dir"),
        "{{OTHER_LANG}}": tr(lang, "other"),
        "{{OTHER_LABEL}}": tr(lang, "other_label"),
        "{{SIGN_OUT}}": tr(lang, "sign_out"),
        "{{REFRESH}}": tr(lang, "refresh"),
        "{{SUBTITLE}}": header_subtitle,
        "{{UPDATED}}": updated,
        "{{QUICK_LINKS}}": quick_links,
        "{{ACTION_CARD}}": action_card,
        "{{CARDS}}": cards_block,
        "{{PUSH_CARD}}": push_card,
        "{{STALE}}": "1" if stale else "0",
        "{{HAVE_CACHE}}": "1" if entry else "0",
        "{{JS_STRINGS}}": json.dumps(js_strings, ensure_ascii=False),
    }.items():
        page = page.replace(marker, value)
    return page


DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="{{LANG}}" dir="{{DIR}}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <title>Shaked Hub</title>
  {{HEAD}}
  <script>
    (function() {
      try {
        // IANA zone names only contain cookie-safe characters, so the value is stored raw.
        var detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
        var match = document.cookie.match(/(?:^|;\\s*)user_tz=([^;]*)/);
        var current = match ? match[1] : null;
        if (detected && detected !== current) {
          document.cookie = "user_tz=" + detected + ";path=/;max-age=31536000;SameSite=Lax";
          location.reload();
        }
      } catch (e) {}
    })();
  </script>
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body class="bg-slate-100 min-h-screen">
  <header class="bg-white shadow-sm sticky top-0 z-10" style="padding-top: env(safe-area-inset-top)">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between gap-4">
      <div>
        <h1 class="text-xl font-bold text-slate-800">📊 Shaked Hub</h1>
        <p class="text-xs text-slate-400">{{SUBTITLE}} · <span id="updated">{{UPDATED}}</span></p>
      </div>
      <div class="flex items-center gap-3">
        <a href="/lang/{{OTHER_LANG}}" class="text-xs font-semibold text-indigo-600 bg-indigo-50 rounded-full px-3 py-1">{{OTHER_LABEL}}</a>
        <button id="refresh" class="text-xl text-slate-500 active:rotate-180 transition-transform" title="{{REFRESH}}">↻</button>
        <a href="/logout" class="text-xs text-slate-400">{{SIGN_OUT}}</a>
      </div>
    </div>
  </header>
  <main class="max-w-6xl mx-auto px-4 py-4 flex flex-col gap-4">
    <nav class="grid grid-cols-5 gap-2">{{QUICK_LINKS}}</nav>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {{ACTION_CARD}}
    </div>
    <div id="cards" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">{{CARDS}}</div>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">{{PUSH_CARD}}</div>
  </main>
  <script>
    (function() {
      var cards = document.getElementById('cards');
      var updated = document.getElementById('updated');
      var refreshButton = document.getElementById('refresh');
      var STORE = 'hub-cards-{{LANG}}';
      window.HUB_T = {{JS_STRINGS}};

      function show(html, when) {
        cards.innerHTML = html;
        if (when) updated.textContent = when;
      }
      function save(data) {
        try { localStorage.setItem(STORE, JSON.stringify(data)); } catch (e) {}
      }
      // The server had nothing cached (it just woke up): show this phone's last copy right away.
      if ('{{HAVE_CACHE}}' === '0') {
        try {
          var saved = JSON.parse(localStorage.getItem(STORE) || 'null');
          if (saved && saved.html) show(saved.html, saved.updated + ' ' + HUB_T.saved);
        } catch (e) {}
      } else {
        save({ html: cards.innerHTML, updated: updated.textContent });
      }

      var loading = false;
      function load(wait) {
        if (loading) return;
        loading = true;
        refreshButton.classList.add('animate-spin');
        fetch('/api/cards' + (wait ? '?wait=1' : ''), { credentials: 'same-origin' })
          .then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); })
          .then(function(data) { show(data.html, data.updated); save(data); })
          .catch(function() {})
          .finally(function() { loading = false; refreshButton.classList.remove('animate-spin'); });
      }
      if ('{{STALE}}' === '1') load(true);
      refreshButton.addEventListener('click', function() { load(true); });
      // Coming back to the app after a while refreshes it.
      var hiddenAt = 0;
      document.addEventListener('visibilitychange', function() {
        if (document.hidden) { hiddenAt = Date.now(); }
        else if (hiddenAt && Date.now() - hiddenAt > 60000) { load(true); }
      });

      function post(url, body) {
        return fetch(url, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
        }).then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); });
      }

      // Buttons inside the cards (the cards are replaced on refresh, so listen on the container).
      cards.addEventListener('click', function(e) {
        var button = e.target.closest('button');
        if (!button) return;
        if (button.dataset.task) {
          button.disabled = true;
          post('/uni/done', { key: button.dataset.task, done: button.dataset.done === '1' })
            .then(function() { load(true); })
            .catch(function() { button.disabled = false; button.textContent = HUB_T.error; });
        } else if (button.dataset.mailAction) {
          button.disabled = true;
          post('/mail/' + button.dataset.id + '/action', { action: button.dataset.mailAction })
            .then(function() {
              var row = button.closest('[data-mail]');
              if (row) row.style.opacity = '0.4';
              button.textContent = '✓';
            })
            .catch(function() { button.disabled = false; button.textContent = HUB_T.error; });
        } else if (button.id === 'garmin-sync') {
          button.disabled = true;
          button.textContent = HUB_T.syncing;
          post('/garmin/sync').then(function() {
            var tries = 0;
            var timer = setInterval(function() {
              tries += 1;
              fetch('/garmin/status', { credentials: 'same-origin' }).then(function(r) { return r.json(); })
                .then(function(s) {
                  if (!s.running || tries > 40) { clearInterval(timer); load(true); }
                });
            }, 3000);
          }).catch(function() { button.disabled = false; button.textContent = HUB_T.error; });
        }
      });
    })();
  </script>
  <script>
    (function() {
      var button = document.getElementById('push-button');
      var status = document.getElementById('push-status');
      if (!button) return;
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        status.textContent = HUB_T.push_home;
        button.remove();
        return;
      }
      function toBytes(base64) {
        var padded = (base64 + '='.repeat((4 - base64.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
        return Uint8Array.from(atob(padded), function(c) { return c.charCodeAt(0); });
      }
      function sameKey(subscription) {
        try {
          var current = new Uint8Array(subscription.options.applicationServerKey);
          var wanted = toBytes(button.dataset.key);
          return current.length === wanted.length && current.every(function(b, i) { return b === wanted[i]; });
        } catch (e) { return false; }
      }
      navigator.serviceWorker.ready.then(function(reg) {
        return reg.pushManager.getSubscription();
      }).then(function(existing) {
        if (existing && Notification.permission === 'granted' && sameKey(existing)) {
          status.textContent = HUB_T.push_on;
          button.textContent = HUB_T.push_test;
        } else if (existing || Notification.permission === 'granted') {
          status.textContent = HUB_T.push_reconnect;
          button.textContent = HUB_T.push_reenable;
        }
      });
      button.addEventListener('click', function() {
        button.disabled = true;
        Notification.requestPermission().then(function(permission) {
          if (permission !== 'granted') throw new Error('denied');
          return navigator.serviceWorker.ready;
        }).then(function(reg) {
          // Start from a fresh subscription so a changed server key never leaves a stale one behind.
          return reg.pushManager.getSubscription().then(function(existing) {
            return existing ? existing.unsubscribe() : null;
          }).then(function() {
            return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: toBytes(button.dataset.key) });
          });
        }).then(function(subscription) {
          return fetch('/push/subscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(subscription)
          });
        }).then(function(r) {
          return r.json().then(function(body) { if (!r.ok || !body.sent) throw new Error('send'); });
        }).then(function() {
          status.textContent = HUB_T.push_sent;
          button.textContent = HUB_T.push_test;
          button.disabled = false;
        }).catch(function(e) {
          status.textContent = e.message === 'denied' ? HUB_T.push_blocked
            : e.message === 'send' ? HUB_T.push_send_failed : HUB_T.push_failed;
          button.disabled = false;
        });
      });
    })();
  </script>
  <script>
    (function() {
      var micButton = document.getElementById('mic-button');
      var textInput = document.getElementById('agent-text-input');
      if (!micButton || micButton.disabled || !textInput) return;
      var mediaRecorder = null, chunks = [], recording = false;
      var idleClasses = ['bg-slate-100', 'hover:bg-indigo-100', 'text-slate-600', 'hover:text-indigo-600'];
      var recordingClasses = ['bg-rose-500', 'text-white', 'animate-pulse'];
      function setRecordingStyle(isRecording) {
        idleClasses.forEach(function(c) { micButton.classList.toggle(c, !isRecording); });
        recordingClasses.forEach(function(c) { micButton.classList.toggle(c, isRecording); });
      }
      micButton.addEventListener('click', function() {
        if (!recording) {
          navigator.mediaDevices.getUserMedia({ audio: true }).then(function(stream) {
            mediaRecorder = new MediaRecorder(stream);
            chunks = [];
            mediaRecorder.ondataavailable = function(e) { if (e.data.size > 0) chunks.push(e.data); };
            mediaRecorder.onstop = function() {
              stream.getTracks().forEach(function(t) { t.stop(); });
              setRecordingStyle(false);
              micButton.textContent = '⏳';
              micButton.disabled = true;
              var formData = new FormData();
              formData.append('audio', new Blob(chunks, { type: 'audio/webm' }), 'recording.webm');
              fetch('/transcribe', { method: 'POST', body: formData })
                .then(function(r) { return r.json(); })
                .then(function(data) { if (data.text) { textInput.value = data.text; textInput.focus(); } })
                .catch(function() {})
                .finally(function() { micButton.textContent = '🎤'; micButton.disabled = false; });
            };
            mediaRecorder.start();
            recording = true;
            micButton.textContent = '⏹️';
            setRecordingStyle(true);
          }).catch(function() { micButton.title = 'Microphone access denied'; });
        } else {
          mediaRecorder.stop();
          recording = false;
        }
      });
    })();
  </script>
</body>
</html>
"""


@app.route("/mail/<message_id>/action", methods=["POST"])
def mail_action(message_id):
    if not re.fullmatch(r"[0-9a-fA-F]+", message_id):
        return jsonify({"error": "bad id"}), 404
    action = (request.get_json(silent=True) or {}).get("action")
    remove = {"read": ["UNREAD"], "archive": ["INBOX", "UNREAD"]}.get(action)
    if not remove:
        return jsonify({"error": "unknown action"}), 400
    try:
        service = build("gmail", "v1", credentials=google_auth.get_credentials(google_auth.SCOPES))
        service.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": remove}).execute()
        invalidate_cards()
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[ERROR] mail action: {e}", file=sys.stderr)
        return jsonify({"error": "failed"}), 500


@app.route("/garmin/sync", methods=["POST"])
def garmin_sync_now():
    if not garmin_sync_agent.is_configured():
        return jsonify({"error": "not configured"}), 503
    garmin_sync_agent.run_in_background(_after_garmin_sync)
    return jsonify({"started": True})


@app.route("/garmin/status")
def garmin_status():
    return jsonify(garmin_sync_agent.status())


def _after_garmin_sync(result):
    if result.get("ok"):
        garmin_agent.clear_cache()
        invalidate_cards()


@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    if not push_agent.is_configured():
        return jsonify({"error": "Push not configured"}), 503
    subscription = request.get_json(silent=True) or {}
    if not subscription.get("endpoint"):
        return jsonify({"error": "Missing subscription"}), 400
    try:
        push_agent.save_subscription(subscription)
        sent = push_agent.send_to_all("🔔 Shaked Hub", "ההתראות פועלות במכשיר הזה.")
        return jsonify({"ok": True, "sent": sent})
    except Exception as e:
        print(f"[ERROR] push_subscribe: {e}", file=sys.stderr)
        return jsonify({"error": "Could not save subscription"}), 500


@app.route("/tasks/check-uni-mail", methods=["POST"])
def check_uni_mail():
    expected = os.environ.get("CRON_SECRET")
    if not expected or not secrets.compare_digest(request.headers.get("X-Cron-Secret", ""), expected):
        return Response("Forbidden", 403)
    try:
        return jsonify(push_agent.check_uni_mail())
    except Exception as e:
        print(f"[ERROR] check_uni_mail: {e}", file=sys.stderr)
        return jsonify({"error": "Check failed"}), 500


MAIL_PAGE = """
<!DOCTYPE html>
<html lang="{lang}" dir="{dir}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject} · Shaked Hub</title>
  {head}
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body class="bg-slate-100 min-h-screen p-4">
  <main class="max-w-2xl mx-auto flex flex-col gap-4">
    <div class="flex items-center justify-between">
      <a href="/" class="text-sm text-indigo-600">{t_back}</a>
      <a href="{web_url}" target="_blank" rel="noopener" class="text-sm text-indigo-600">{t_open_gmail}</a>
    </div>
    {notice}
    <section class="bg-white rounded-2xl shadow-md p-5 flex flex-col gap-2">
      <h1 class="text-lg font-semibold text-slate-800">{subject}</h1>
      <p class="text-sm text-slate-500">{sender} · {date}</p>
      <div dir="auto" class="mt-2 text-sm text-slate-700 whitespace-pre-wrap break-words">{text}</div>
    </section>
    <form method="POST" class="bg-white rounded-2xl shadow-md p-5 flex flex-col gap-3"
          onsubmit="return event.submitter.value !== 'send' || confirm('{t_confirm}');">
      <p class="text-sm font-semibold text-slate-700">{t_reply_to}</p>
      <textarea name="body" required rows="6" dir="auto"
                class="border border-slate-200 rounded-lg p-3 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300"></textarea>
      <div class="flex gap-2">
        <button name="action" value="send" class="flex-1 bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg px-4 py-2.5 text-sm font-medium">{t_send}</button>
        <button name="action" value="draft" class="flex-1 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-lg px-4 py-2.5 text-sm font-medium">{t_draft}</button>
      </div>
    </form>
  </main>
</body>
</html>
"""


@app.route("/mail/<message_id>", methods=["GET", "POST"])
def mail_view(message_id):
    if not re.fullmatch(r"[0-9a-fA-F]+", message_id):
        return Response("Not found", 404)
    notice = ""
    lang = get_lang()
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        send = request.form.get("action") == "send"
        try:
            to = gmail_agent.reply(message_id, body, send=send)
            done = tr(lang, "reply_sent" if send else "reply_drafted", html.escape(to))
            notice = f"<div class='bg-emerald-50 border-s-4 border-emerald-400 rounded-e-lg p-3 text-sm text-emerald-900'>{done}</div>"
        except Exception as e:
            print(f"[ERROR] mail reply: {e}", file=sys.stderr)
            notice = f"<div class='bg-rose-50 border-s-4 border-rose-400 rounded-e-lg p-3 text-sm text-rose-900'>{tr(lang, 'reply_failed')}</div>"
    try:
        message = gmail_agent.get_message(message_id)
    except Exception as e:
        print(f"[ERROR] mail view: {e}", file=sys.stderr)
        return redirect(gmail_agent.GMAIL_WEB_URL.format(message_id))
    reply_to_js = html.escape(message["reply_to"]).replace("'", "").replace("\\", "")
    return MAIL_PAGE.format(
        head=PWA_HEAD_TAGS,
        lang=lang,
        dir=tr(lang, "dir"),
        t_back=tr(lang, "back"),
        t_open_gmail=tr(lang, "open_in_gmail"),
        t_confirm=tr(lang, "confirm_send", reply_to_js),
        t_reply_to=tr(lang, "reply_to", html.escape(message["reply_to"])),
        t_send=tr(lang, "send"),
        t_draft=tr(lang, "save_draft"),
        notice=notice,
        subject=html.escape(message["subject"]),
        sender=html.escape(message["from"]),
        date=html.escape(message["date"]),
        text=html.escape(message["text"][:20000]),
        web_url=html.escape(message["web_url"]),
    )


@app.route("/uni/done", methods=["POST"])
def uni_done():
    body = request.get_json(silent=True)
    if body is not None:
        key, done = body.get("key", ""), bool(body.get("done"))
    else:
        key, done = request.form.get("key", ""), request.form.get("done") == "1"
    ok = False
    if key:
        try:
            uni_agent.set_done(key, done)
            invalidate_cards()
            ok = True
        except Exception as e:
            print(f"[ERROR] uni_done: {e}", file=sys.stderr)
    if body is not None:
        return (jsonify({"ok": True}), 200) if ok else (jsonify({"error": "failed"}), 500)
    return redirect("/#uni")


@app.route("/tasks/check-uni", methods=["POST"])
def check_uni():
    expected = os.environ.get("CRON_SECRET")
    if not expected or not secrets.compare_digest(request.headers.get("X-Cron-Secret", ""), expected):
        return Response("Forbidden", 403)
    # The check reads mail with Claude and can outlast Gunicorn's 30-second request limit,
    # so it runs in the background and the response reports the previous run.
    with _uni_check["lock"]:
        running = _uni_check["thread"] is not None and _uni_check["thread"].is_alive()
        if not running:
            _uni_check["thread"] = threading.Thread(target=_run_uni_check, daemon=True)
            _uni_check["thread"].start()
    return jsonify({"started": not running, "last_run": _uni_check["last"]})


_uni_check = {"lock": threading.Lock(), "thread": None, "last": None}


def _run_uni_check():
    try:
        result = uni_agent.run_check()
    except Exception as e:
        print(f"[ERROR] check_uni: {e}", file=sys.stderr)
        result = {"ok": False, "errors": [type(e).__name__]}
    result["finished_at"] = formatting.now_in_tz().strftime("%d/%m %H:%M")
    _uni_check["last"] = result


@app.route("/agent-action", methods=["POST"])
def agent_action():
    tz_name = formatting.resolve_tz_name(request.cookies.get(USER_TZ_COOKIE, formatting.DEFAULT_TZ_NAME))
    user_text = request.form.get("user_text", "").strip()
    if not user_text:
        flash(tr(get_lang(), "empty_request"))
        return redirect(url_for("dashboard"))

    try:
        result = action_agent.handle_request(user_text, tz_name)
    except Exception as e:
        print(f"[ERROR] agent_action: {e}", file=sys.stderr)
        result = tr(get_lang(), "action_failed")

    flash(result)
    return redirect(url_for("dashboard"))


@app.route("/transcribe", methods=["POST"])
def transcribe():
    if not voice_agent.is_configured():
        return jsonify({"error": "Voice input not configured"}), 503
    if "audio" not in request.files:
        return jsonify({"error": "No audio provided"}), 400

    try:
        text = voice_agent.transcribe(request.files["audio"])
        return jsonify({"text": text})
    except Exception as e:
        print(f"[ERROR] transcribe: {e}", file=sys.stderr)
        return jsonify({"error": "Could not transcribe audio"}), 500



# ---------- background work ----------
# Render's free plan sleeps after 15 minutes without outside traffic, and GitHub's scheduled
# runs come only every few hours. So the server keeps itself awake: every 10 minutes it calls
# its own public address and runs the university check, and every 2 hours it syncs Garmin.

PING_EVERY = 10 * 60
UNI_EVERY = 10 * 60
GARMIN_EVERY = 2 * 60 * 60
_background = {"started_at": time.time(), "ticks": 0, "ping": None, "last_uni": 0, "last_garmin": 0, "last_ping": 0}


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


def _ping_self():
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if not base:
        return
    try:
        response = requests.get(base.rstrip("/") + "/healthz", timeout=30)
        _background["ping"] = {"status": response.status_code, "at": formatting.now_in_tz().strftime("%d/%m %H:%M")}
    except Exception as e:
        _background["ping"] = {"error": type(e).__name__, "at": formatting.now_in_tz().strftime("%d/%m %H:%M")}


def _start_uni_check():
    with _uni_check["lock"]:
        if _uni_check["thread"] is not None and _uni_check["thread"].is_alive():
            return False
        _uni_check["thread"] = threading.Thread(target=_run_uni_check, daemon=True)
        _uni_check["thread"].start()
        return True


def _background_loop():
    time.sleep(15)
    try:
        refresh_cards_async(formatting.DEFAULT_TZ_NAME)
    except Exception as e:
        print(f"[ERROR] warm cards: {e}", file=sys.stderr)
    while True:
        now = time.time()
        _background["ticks"] += 1
        try:
            if now - _background["last_ping"] >= PING_EVERY:
                _background["last_ping"] = now
                _ping_self()
            if os.environ.get("CRON_SECRET") and now - _background["last_uni"] >= UNI_EVERY:
                _background["last_uni"] = now
                _start_uni_check()
            if garmin_sync_agent.is_configured() and now - _background["last_garmin"] >= GARMIN_EVERY:
                _background["last_garmin"] = now
                garmin_sync_agent.run_in_background(_after_garmin_sync)
        except Exception as e:
            print(f"[ERROR] background: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(60)


_background_owner = {"pid": None, "lock": threading.Lock()}


@app.before_request
def start_background():
    """Start the loop in the process that serves requests. Started lazily (not on import),
    because with a preloading server the import happens in a parent process whose threads
    never reach the worker."""
    if os.environ.get("HUB_NO_BACKGROUND") or _background_owner["pid"] == os.getpid():
        return None
    with _background_owner["lock"]:
        if _background_owner["pid"] != os.getpid():
            _background_owner["pid"] = os.getpid()
            _background["started_at"] = time.time()
            threading.Thread(target=_background_loop, daemon=True, name="hub-background").start()
    return None


@app.route("/tasks/status", methods=["GET", "POST"])
def task_status():
    """Health of every connection, for the scheduled GitHub run to print (no secrets in it)."""
    expected = os.environ.get("CRON_SECRET")
    if not expected or not secrets.compare_digest(request.headers.get("X-Cron-Secret", ""), expected):
        return Response("Forbidden", 403)
    report = {
        "server": {
            "pid": os.getpid(),
            "uptime_min": round((time.time() - _background["started_at"]) / 60),
            "threads": bool(request.environ.get("wsgi.multithread")),
            "background_ticks": _background["ticks"],
            "background_pid": _background_owner["pid"],
            "self_ping": _background["ping"],
        },
    }

    def check(name, fn):
        try:
            report[name] = fn()
        except Exception as e:
            report[name] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}

    def google():
        creds = google_auth.get_credentials(google_auth.SCOPES)
        gmail = build("gmail", "v1", credentials=creds)
        gmail.users().labels().list(userId="me").execute()
        build("calendar", "v3", credentials=creds).calendarList().list(maxResults=1).execute()
        return {"gmail": "ok", "calendar": "ok"}

    def push():
        return {"source": push_agent.key_source() or (push_agent.key_pair() and push_agent.key_source()),
                "problem": push_agent.private_key_problem(), "devices": push_agent.device_count()}

    def garmin():
        return {"in_supabase": garmin_agent.latest_dates(), "cloud_sync": garmin_sync_agent.is_configured(),
                **garmin_sync_agent.status()}

    check("google", google)
    check("push", push)
    check("garmin", garmin)
    report["uni_last_run"] = _uni_check["last"]
    entry = cached_cards(formatting.DEFAULT_TZ_NAME)
    report["cards_cache_age_s"] = round(time.time() - entry["at"]) if entry else None
    return jsonify(report)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
