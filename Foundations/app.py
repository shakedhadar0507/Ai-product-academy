import html
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
    # Static files carry a version in their link (app.css?v=2), so phones may keep them a year.
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
    if request.path in PUBLIC_PATHS or request.path.startswith("/static/"):
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
    if request.method == "POST":
        if credentials_valid(request.form.get("username"), request.form.get("password")):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error_html = "<p class='text-sm text-rose-500'>Wrong username or password.</p>"
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Sign in · Shaked Hub</title>
      {PWA_HEAD_TAGS}
      <link rel="stylesheet" href="/static/app.css?v=2">
    </head>
    <body class="bg-slate-100 min-h-screen flex items-center justify-center p-4">
      <form method="POST" class="bg-white rounded-2xl shadow-md p-8 w-full max-w-sm flex flex-col gap-3">
        <h1 class="text-xl font-bold text-slate-800">📊 Shaked Hub</h1>
        {error_html}
        <input name="username" autocomplete="username" placeholder="Username" required
               class="border border-slate-200 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
        <input name="password" type="password" autocomplete="current-password" placeholder="Password" required
               class="border border-slate-200 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
        <button type="submit"
                class="bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg px-4 py-2.5 text-sm font-medium transition-colors">
          Sign in
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


EMAIL_LABEL_BUCKETS = [
    ("לימודים", "label:לימודים"),
    ("RFS", "label:RFS"),
    ("פיננסים", "label:פיננסים"),
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
            userId="me", id=message["id"], format="metadata", metadataHeaders=["Subject"]
        ).execute()
        samples.append({"id": message["id"], "subject": get_header(detail["payload"]["headers"], "Subject")})
    return max(count, len(samples)), samples


def render_card(icon, title, body_html, link=None, link_text=None, name=None):
    """A dashboard card. `link` adds a tappable button in the header (Gmail, Calendar...)."""
    header_link = (
        f"<a href='{html.escape(link)}' target='_blank' rel='noopener' "
        f"class='text-xs font-medium text-indigo-600 bg-indigo-50 hover:bg-indigo-100 rounded-full px-3 py-1'>{link_text}</a>"
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


GMAIL_URL = "https://mail.google.com/mail/u/0/#inbox"
GMAIL_SEARCH_URL = "https://mail.google.com/mail/u/0/#search/{}"
CALENDAR_URL = "https://calendar.google.com/calendar/r/day"
NEW_EVENT_URL = "https://calendar.google.com/calendar/r/eventedit"
GARMIN_URL = "https://connect.garmin.com/modern/"


def render_email_bucket(name, query, count, samples):
    title = render_link(GMAIL_SEARCH_URL.format(quote(query)), html.escape(name))
    if count == 0:
        body = "<p class='text-slate-400 text-sm'>אין מיילים</p>"
    else:
        rows = []
        for m in samples:
            rows.append(
                f"<div class='flex items-start justify-between gap-2' data-mail='{m['id']}'>"
                f"<span class='text-sm'>{render_link('/mail/' + m['id'], html.escape(m['subject']))}</span>"
                f"<span class='shrink-0 flex gap-1'>"
                f"<button data-mail-action='read' data-id='{m['id']}' class='text-xs rounded-lg px-2 py-1 bg-slate-100 hover:bg-indigo-100'>נקרא</button>"
                f"<button data-mail-action='archive' data-id='{m['id']}' class='text-xs rounded-lg px-2 py-1 bg-slate-100 hover:bg-indigo-100'>ארכב</button>"
                f"</span></div>"
            )
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

def card_weather(tz_name, data):
    try:
        conditions = weather_agent.get_running_conditions(LATITUDE, LONGITUDE)
        data["weather"] = weather_agent.get_current_weather(LATITUDE, LONGITUDE)
        advice = weather_agent.recommend_run(
            conditions, garmin_agent.get_planned_workout(formatting.today_in_tz(tz_name))
        )
        air = f"{conditions['aqi']:.0f}" if conditions.get("aqi") is not None else "–"
        pm10 = f"{conditions['pm10']:.0f}" if conditions.get("pm10") is not None else "–"
        stats = [
            ("Temp", f"{conditions['temperature']:.0f}&deg;"),
            ("Feels like", f"{conditions['feels_like']:.0f}&deg;"),
            ("Humidity", f"{conditions['humidity']:.0f}%"),
            ("Dew point", f"{conditions['dew_point']:.0f}&deg;"),
            ("Wind", f"{conditions['windspeed']:.0f} km/h"),
            ("Air (AQI / PM10)", f"{air} / {pm10}"),
        ]
        stats_html = "".join(
            f"<div class='bg-slate-50 rounded-lg p-2'><p class='text-xs text-slate-400'>{label}</p>"
            f"<p class='font-semibold text-slate-800'>{value}</p></div>"
            for label, value in stats
        )
        colors = {
            "good": ("bg-emerald-50", "border-emerald-400", "text-emerald-900"),
            "warn": ("bg-amber-50", "border-amber-400", "text-amber-900"),
            "bad": ("bg-rose-50", "border-rose-400", "text-rose-900"),
        }[advice["level"]]
        tips_html = "".join(f"<li>{tip}</li>" for tip in advice["tips"])
        body = f"""
        <div class="grid grid-cols-3 gap-2">{stats_html}</div>
        <div dir="rtl" class="mt-3 {colors[0]} border-r-4 {colors[1]} rounded-l-lg p-3 {colors[2]}">
          <p class="font-semibold text-sm">{advice["verdict"]}</p>
          <ul class="list-disc pr-5 mt-1 text-sm flex flex-col gap-0.5">{tips_html}</ul>
        </div>
        """
    except Exception as e:
        print(f"[ERROR] weather: {e}", file=sys.stderr)
        body = render_error("Could not fetch weather")
    return render_card("🌤️", "Run Weather · Tel Aviv", body, name="weather")


def card_countdown(tz_name, data):
    days_remaining = (MARATHON_DATE - formatting.today_in_tz(tz_name)).days
    if days_remaining > 0:
        countdown_html = (
            f"<span class='text-3xl font-bold text-indigo-600'>{days_remaining}</span> "
            f"<span class='text-slate-500'>days to go</span>"
        )
    elif days_remaining == 0:
        countdown_html = "<span class='text-lg font-semibold text-indigo-600'>Race day is today! 🎉</span>"
    else:
        return ""  # the race is over; the card comes back when a new race date is set
    body = (
        f"<div class='flex flex-col gap-1'>{countdown_html}"
        f"<p class='text-sm text-slate-400'>NYC Marathon &mdash; {MARATHON_DATE.strftime('%d/%m/%Y')}</p></div>"
    )
    return render_card("🏁", "Marathon Countdown", body, name="countdown")


def card_calendar(tz_name, data):
    try:
        events = calendar_agent.get_data(tz_name)
        data["calendar"] = events
        insight = calendar_agent.get_insight(events, tz_name)
        if not events:
            events_html = "<p class='text-slate-400'>אין אירועים היום.</p>"
        else:
            items = []
            for event in events:
                time_range = formatting.format_time_range(
                    event["start"].get("dateTime"), event["end"].get("dateTime")
                )
                title = html.escape(event.get("summary", "(no title)"))
                if event.get("htmlLink"):
                    title = render_link(event["htmlLink"], title)
                items.append(f"<span class='text-slate-400'>{time_range}</span> &mdash; {title}")
            events_html = render_list(items)
        insight_html = f"""
        <div class="mt-3 bg-indigo-50 border-l-4 border-indigo-400 rounded-r-lg p-3">
          <p class="text-xs uppercase tracking-wide text-indigo-500 font-semibold mb-1">AI Insight</p>
          <p class="text-indigo-900 text-sm">{insight}</p>
        </div>
        <a href="{NEW_EVENT_URL}" target="_blank" rel="noopener"
           class="mt-3 inline-block text-sm text-indigo-600 font-medium">＋ אירוע חדש</a>
        """
        body = events_html + insight_html
    except Exception as e:
        print(f"[ERROR] calendar: {e}", file=sys.stderr)
        body = render_error("Could not fetch calendar events") + (
            f"<p class='text-sm mt-2'>{render_link(CALENDAR_URL, 'פתח את Google Calendar')}</p>"
        )
    return render_card("📅", "Today's Calendar", body, CALENDAR_URL, "פתח יומן", name="calendar")


def card_uni(tz_name, data):
    if not uni_agent.tasks_configured():
        return ""
    try:
        tasks = uni_agent.list_tasks()
        now = formatting.now_in_tz(tz_name)
        open_tasks = [t for t in tasks if not t["done"]]
        done_tasks = [t for t in tasks if t["done"]]
        if not tasks:
            body = "<p class='text-slate-400'>אין מטלות פתוחות.</p>"
        else:
            items = []
            for task in open_tasks + done_tasks:
                overdue = not task["done"] and task["due"] and task["due"] < now
                when = uni_agent.format_due(task["due"]) if task["due"] else ""
                if overdue:
                    when = f"<span class='text-rose-600 font-semibold'>לא בוצע · {when}</span>"
                else:
                    when = f"<span class='font-semibold text-slate-700'>{when}</span>"
                course = f"<span class='text-slate-400'> · {html.escape(task['course'])}</span>" if task.get("course") else ""
                title = html.escape(task["title"])
                if task["key"].startswith("mail:"):
                    title = render_link("/mail/" + task["key"][5:], title)
                elif task.get("url"):
                    title = render_link(task["url"], title)
                button = "בטל" if task["done"] else "✓ בוצע"
                style = "line-through text-slate-400" if task["done"] else ""
                items.append(
                    f"<div class='flex items-start justify-between gap-2'>"
                    f"<div class='{style}'>{when} &mdash; {title}{course}</div>"
                    f"<button data-task='{html.escape(task['key'])}' data-done='{'0' if task['done'] else '1'}' "
                    f"class='shrink-0 text-xs rounded-lg px-2 py-1 bg-slate-100 hover:bg-emerald-100'>{button}</button>"
                    f"</div>"
                )
            body = f"<div id='uni' dir='rtl'>{render_list(items)}</div>"
    except Exception as e:
        print(f"[ERROR] uni card: {e}", file=sys.stderr)
        body = render_error("Could not fetch university tasks")
    return render_card("📚", "University tasks", body, uni_agent.MOODLE_URL, "Moodle", name="uni")


def card_inbox(tz_name, data):
    try:
        creds = google_auth.get_credentials(google_auth.SCOPES)
        service = build("gmail", "v1", credentials=creds)
        bucket_data, sections = [], []
        for label_name, query in EMAIL_LABEL_BUCKETS + [("Everything Else", OTHER_EMAIL_QUERY)]:
            count, subjects = get_email_bucket(service, query)
            bucket_data.append((label_name, count, subjects))
            sections.append(render_email_bucket(label_name, query, count, subjects))
        data["email"] = bucket_data
        body = "<div dir='rtl' class='flex flex-col gap-4'>" + "".join(sections) + "</div>"
    except Exception as e:
        print(f"[ERROR] inbox: {e}", file=sys.stderr)
        body = render_error("Could not fetch emails") + (
            f"<p class='text-sm mt-2'>{render_link(GMAIL_URL, 'פתח את Gmail')}</p>"
        )
    return render_card("📧", "Inbox", body, GMAIL_URL, "פתח Gmail", name="inbox")


def _sync_line():
    """When Garmin data last synced from the server, and the sync-now button."""
    state = garmin_sync_agent.status()
    if not garmin_sync_agent.is_configured():
        note = "סנכרון מהשרת עוד לא מחובר"
    elif state["running"]:
        note = "מסנכרן עכשיו…"
    elif state["last"]:
        last = state["last"]
        note = f"סונכרן {last['finished_at']}" if last.get("ok") else f"סנכרון נכשל {last['finished_at']}"
    else:
        note = "מחכה לסנכרון ראשון"
    button = (
        "<button id='garmin-sync' class='text-xs rounded-lg px-2 py-1 bg-slate-100 hover:bg-indigo-100'>↻ סנכרן עכשיו</button>"
        if garmin_sync_agent.is_configured() else ""
    )
    return (
        f"<div dir='rtl' class='flex items-center justify-between gap-2 mt-3'>"
        f"<p id='garmin-sync-note' class='text-xs text-slate-400'>{note}</p>{button}</div>"
    )


def card_training(tz_name, data):
    today_local = formatting.today_in_tz(tz_name)
    planned = garmin_agent.get_planned_workout(today_local)
    plan_html = (
        f"<p class='text-xs uppercase tracking-wide text-slate-400 font-semibold mb-1'>Today's plan</p>"
        f"<p dir='rtl' class='text-slate-800 font-medium'>{planned or 'אין אימון מתוכנן'}</p>"
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
            "Garmin data not connected yet" if not garmin_agent.is_configured() else "Could not fetch Garmin data"
        )
    else:
        wellness = garmin_data["wellness"] or {}
        stats = [
            ("Readiness", garmin_agent.format_number(wellness.get("training_readiness"))),
            ("HRV", f"{wellness['hrv_last_night_ms']:.0f} ms" if wellness.get("hrv_last_night_ms") else None),
            ("Sleep", garmin_agent.format_hours(wellness.get("sleep_s")) if wellness.get("sleep_s") else None),
            ("Sleep score", garmin_agent.format_number(wellness.get("sleep_score"))),
            ("Resting HR", garmin_agent.format_number(wellness.get("resting_hr"))),
            ("Body Battery", garmin_agent.format_number(wellness.get("body_battery_high"))),
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
                f"Night of {formatting.format_date(wellness_date)}" if wellness_date else None,
                f"HRV status: {hrv_status.lower()}" if hrv_status else None,
                garmin_agent.format_status(wellness.get("training_status")),
            ] if part
        )
        garmin_html = (
            f"<div class='grid grid-cols-3 gap-2 mt-3'>{stats_html}</div>"
            f"<p class='text-xs text-slate-400 mt-2'>{caption}</p>"
        )
    body = plan_html + garmin_html + _sync_line()
    return render_card("🏃", "Today's Training", body, GARMIN_URL, "Garmin", name="training")


def card_volume(tz_name, data):
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
        f"<span class='w-14 text-right font-medium text-slate-700'>{km} km</span></div>"
        for week_start, km in weeks
    )
    runs = garmin_data["runs"]
    if runs:
        last = runs[0]
        hr_html = f" &middot; HR {last['avg_hr']:.0f}" if last.get("avg_hr") else ""
        last_html = (
            f"<p class='text-xs uppercase tracking-wide text-slate-400 font-semibold mb-1 mt-4'>Last run</p>"
            f"<p class='text-sm'><span class='text-slate-400'>{formatting.format_date(last['start_time_local'][:10])}</span> &mdash; "
            f"<span class='font-medium text-slate-800'>{(last['distance_m'] or 0) / 1000:.1f} km</span> "
            f"&middot; {garmin_agent.format_pace(last['avg_pace_s_per_km'])}/km{hr_html}</p>"
        )
    else:
        last_html = "<p class='text-slate-400 text-sm mt-4'>No runs in the last 5 weeks.</p>"
    body = (
        "<p class='text-xs uppercase tracking-wide text-slate-400 font-semibold mb-2'>Weekly distance</p>"
        f"<div class='flex flex-col gap-1.5'>{bars}</div>{last_html}"
    )
    return render_card("📈", "Running Volume", body, GARMIN_URL, "Garmin", name="volume")


def card_brief(tz_name, data):
    try:
        brief = brief_agent.get_insight(
            data.get("calendar"), data.get("weather"), data.get("email"), data.get("training"), tz_name
        )
        brief_body = f"<p class='text-lg leading-relaxed'>{brief}</p>"
    except Exception as e:
        print(f"[ERROR] daily_brief: {e}", file=sys.stderr)
        brief_body = "<p class='text-lg text-indigo-100 italic'>Could not generate daily brief</p>"
    return f"""
    <section data-card="brief" class="md:col-span-2 lg:col-span-3 bg-gradient-to-r from-indigo-600 to-purple-600 text-white rounded-2xl shadow-lg p-6 flex flex-col gap-3">
      <h2 class="flex items-center gap-2 text-xl font-bold"><span class="text-3xl">🧭</span> Daily Brief</h2>
      {brief_body}
    </section>
    """


# Page order. The brief needs the other cards' data, so it is built last.
CARD_ORDER = ["brief", "weather", "countdown", "calendar", "uni", "inbox", "training", "volume"]
PARALLEL_CARDS = {
    "weather": card_weather, "countdown": card_countdown, "calendar": card_calendar,
    "uni": card_uni, "inbox": card_inbox, "training": card_training,
}
CARDS_FRESH_SECONDS = 120
_cards = {"lock": threading.Lock(), "by_tz": {}, "building": set()}
_pool = ThreadPoolExecutor(max_workers=8)


def build_cards(tz_name):
    data = {}
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
        _cards["by_tz"][tz_name] = entry
    return entry


def cached_cards(tz_name):
    with _cards["lock"]:
        return _cards["by_tz"].get(tz_name)


def refresh_cards_async(tz_name):
    with _cards["lock"]:
        if tz_name in _cards["building"]:
            return
        _cards["building"].add(tz_name)

    def work():
        try:
            build_cards(tz_name)
        finally:
            with _cards["lock"]:
                _cards["building"].discard(tz_name)

    _pool.submit(work)


def invalidate_cards():
    with _cards["lock"]:
        for entry in _cards["by_tz"].values():
            entry["at"] = 0


def cards_html(entry):
    return "".join(entry["cards"].get(name, "") for name in CARD_ORDER)


def _tz():
    return formatting.resolve_tz_name(request.cookies.get(USER_TZ_COOKIE, formatting.DEFAULT_TZ_NAME))


@app.route("/api/cards")
def api_cards():
    """Fresh cards as HTML. ?wait=1 builds them now when the cache is old (used right after open)."""
    tz_name = _tz()
    entry = cached_cards(tz_name)
    if entry is None or (request.args.get("wait") and time.time() - entry["at"] > CARDS_FRESH_SECONDS):
        entry = build_cards(tz_name)
    return jsonify({"html": cards_html(entry), "at": entry["at"], "updated": _updated_text(entry["at"], tz_name)})


def _updated_text(timestamp, tz_name):
    return datetime.fromtimestamp(timestamp, ZoneInfo(tz_name)).strftime("%H:%M")


QUICK_LINKS = [
    ("📅", "יומן", CALENDAR_URL),
    ("＋", "אירוע", NEW_EVENT_URL),
    ("✉️", "Gmail", GMAIL_URL),
    ("📚", "Moodle", None),
    ("⌚", "Garmin", GARMIN_URL),
]


@app.route("/")
def dashboard():
    tz_name = _tz()
    entry = cached_cards(tz_name)
    if entry is None:
        cards_block = (
            "<section class='md:col-span-2 lg:col-span-3 bg-white rounded-2xl shadow-md p-6 text-slate-400 text-sm'>"
            "טוען נתונים…</section>"
        )
        stale = True
        updated = ""
    else:
        cards_block = cards_html(entry)
        stale = time.time() - entry["at"] > CARDS_FRESH_SECONDS
        updated = _updated_text(entry["at"], tz_name)
    have_cache = "1" if entry else "0"

    action_messages = get_flashed_messages()
    action_result_html = (
        f"<div class='bg-emerald-50 border-l-4 border-emerald-400 rounded-r-lg p-3 text-sm text-emerald-900'>{action_messages[0]}</div>"
        if action_messages else ""
    )

    voice_configured = voice_agent.is_configured()
    if voice_configured:
        mic_disabled_attr = ""
        mic_title = "Record a voice request"
        mic_extra_class = "bg-slate-100 hover:bg-indigo-100 text-slate-600 hover:text-indigo-600 cursor-pointer"
    else:
        mic_disabled_attr = "disabled"
        mic_title = "Voice input not configured yet"
        mic_extra_class = "bg-slate-50 text-slate-300 cursor-not-allowed"

    quick_links = "".join(
        f"<a href='{html.escape(url or uni_agent.MOODLE_URL)}' target='_blank' rel='noopener' "
        f"class='flex flex-col items-center gap-1 bg-white rounded-2xl shadow-sm py-3 active:scale-95 transition-transform'>"
        f"<span class='text-2xl'>{icon}</span><span class='text-xs text-slate-600'>{label}</span></a>"
        for icon, label, url in QUICK_LINKS
    )

    push_card = ""
    if push_agent.is_configured():
        try:
            key = push_agent.public_key()
            push_card = render_card("🔔", "Notifications", (
                "<p id='push-status' class='text-sm text-slate-500'>Get an alert on this device when something needs you.</p>"
                f"<button id='push-button' data-key='{key}' "
                "class='mt-3 bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg px-4 py-2 text-sm font-medium'>"
                "Enable notifications</button>"
            ))
        except Exception as e:
            print(f"[ERROR] push key: {type(e).__name__}", file=sys.stderr)

    action_card = f"""
    <section class="md:col-span-2 lg:col-span-3 bg-white rounded-2xl shadow-md p-5 flex flex-col gap-3">
      <div>
        <h2 class="flex items-center gap-2 text-lg font-semibold text-slate-800">
          <span class="text-2xl">🤖</span> What should I do?
        </h2>
        <p class="text-sm text-slate-400 mt-0.5">Type or speak a request — schedule events, draft emails, and more.</p>
      </div>
      {action_result_html}
      <form method="POST" action="/agent-action" class="flex flex-col sm:flex-row gap-2">
        <input type="text" name="user_text" id="agent-text-input" required
               placeholder="e.g. schedule a dentist appointment tomorrow 15:00-16:00"
               class="flex-1 border border-slate-200 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
        <div class="flex gap-2">
          <button type="button" id="mic-button" {mic_disabled_attr} title="{mic_title}"
                  class="w-11 h-11 shrink-0 flex items-center justify-center rounded-full text-lg transition-all duration-200 active:scale-90 {mic_extra_class}">
            🎤
          </button>
          <button type="submit"
                  class="flex-1 sm:flex-none bg-indigo-500 hover:bg-indigo-600 active:scale-95 text-white rounded-lg px-5 py-2.5 text-sm font-medium">
            Send
          </button>
        </div>
      </form>
    </section>
    """

    now_local = formatting.now_in_tz(tz_name)
    header_subtitle = f"{now_local.strftime('%A, %B')} {now_local.day}, {now_local.year}"

    page = DASHBOARD_PAGE
    for marker, value in {
        "{{HEAD}}": PWA_HEAD_TAGS,
        "{{SUBTITLE}}": header_subtitle,
        "{{UPDATED}}": updated,
        "{{QUICK_LINKS}}": quick_links,
        "{{ACTION_CARD}}": action_card,
        "{{CARDS}}": cards_block,
        "{{PUSH_CARD}}": push_card,
        "{{STALE}}": "1" if stale else "0",
        "{{HAVE_CACHE}}": have_cache,
    }.items():
        page = page.replace(marker, value)
    return page


DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="en">
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
  <link rel="stylesheet" href="/static/app.css?v=2">
</head>
<body class="bg-slate-100 min-h-screen">
  <header class="bg-white shadow-sm sticky top-0 z-10" style="padding-top: env(safe-area-inset-top)">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between gap-4">
      <div>
        <h1 class="text-xl font-bold text-slate-800">📊 Shaked Hub</h1>
        <p class="text-xs text-slate-400">{{SUBTITLE}} · <span id="updated">{{UPDATED}}</span></p>
      </div>
      <div class="flex items-center gap-3">
        <button id="refresh" class="text-xl text-slate-500 active:rotate-180 transition-transform" title="Refresh">↻</button>
        <a href="/logout" class="text-xs text-slate-400">Sign out</a>
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
      var STORE = 'hub-cards';

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
          if (saved && saved.html) show(saved.html, saved.updated + ' (שמור)');
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
            .catch(function() { button.disabled = false; button.textContent = 'שגיאה'; });
        } else if (button.dataset.mailAction) {
          button.disabled = true;
          post('/mail/' + button.dataset.id + '/action', { action: button.dataset.mailAction })
            .then(function() {
              var row = button.closest('[data-mail]');
              if (row) row.style.opacity = '0.4';
              button.textContent = '✓';
            })
            .catch(function() { button.disabled = false; button.textContent = 'שגיאה'; });
        } else if (button.id === 'garmin-sync') {
          button.disabled = true;
          button.textContent = 'מסנכרן…';
          post('/garmin/sync').then(function() {
            var tries = 0;
            var timer = setInterval(function() {
              tries += 1;
              fetch('/garmin/status', { credentials: 'same-origin' }).then(function(r) { return r.json(); })
                .then(function(s) {
                  if (!s.running || tries > 40) { clearInterval(timer); load(true); }
                });
            }, 3000);
          }).catch(function() { button.disabled = false; button.textContent = 'שגיאה'; });
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
        status.textContent = 'On iPhone, open Shaked Hub from the home screen to enable notifications.';
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
          status.textContent = 'Notifications are on for this device ✅';
          button.textContent = 'Send a test notification';
        } else if (existing || Notification.permission === 'granted') {
          status.textContent = 'ההתראות צריכות חיבור מחדש. לחץ כאן פעם אחת.';
          button.textContent = 'הפעל התראות מחדש';
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
          status.textContent = 'Notifications are on for this device ✅ (a test was just sent)';
          button.textContent = 'Send a test notification';
          button.disabled = false;
        }).catch(function(e) {
          status.textContent = e.message === 'denied'
            ? 'Notifications are blocked. Allow them in Settings > Notifications > Shaked Hub.'
            : e.message === 'send' ? 'Saved, but the test notification failed. Tell Claude.' : 'Could not enable notifications. Try again.';
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
<html lang="he" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject} · Shaked Hub</title>
  {head}
  <link rel="stylesheet" href="/static/app.css?v=2">
</head>
<body class="bg-slate-100 min-h-screen p-4">
  <main class="max-w-2xl mx-auto flex flex-col gap-4">
    <div class="flex items-center justify-between">
      <a href="/" class="text-sm text-indigo-600">→ חזרה לדשבורד</a>
      <a href="{web_url}" target="_blank" rel="noopener" class="text-sm text-indigo-600">פתח ב-Gmail</a>
    </div>
    {notice}
    <section class="bg-white rounded-2xl shadow-md p-5 flex flex-col gap-2">
      <h1 class="text-lg font-semibold text-slate-800">{subject}</h1>
      <p class="text-sm text-slate-500">{sender} · {date}</p>
      <div dir="auto" class="mt-2 text-sm text-slate-700 whitespace-pre-wrap break-words">{text}</div>
    </section>
    <form method="POST" class="bg-white rounded-2xl shadow-md p-5 flex flex-col gap-3"
          onsubmit="return event.submitter.value !== 'send' || confirm('לשלוח את התשובה ל-{reply_to_js}?');">
      <p class="text-sm font-semibold text-slate-700">תשובה אל {reply_to}</p>
      <textarea name="body" required rows="6" dir="auto"
                class="border border-slate-200 rounded-lg p-3 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300"></textarea>
      <div class="flex gap-2">
        <button name="action" value="send" class="flex-1 bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg px-4 py-2.5 text-sm font-medium">שלח</button>
        <button name="action" value="draft" class="flex-1 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-lg px-4 py-2.5 text-sm font-medium">שמור כטיוטה</button>
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
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        send = request.form.get("action") == "send"
        try:
            to = gmail_agent.reply(message_id, body, send=send)
            done = "נשלחה" if send else "נשמרה כטיוטה ב-Gmail"
            notice = f"<div class='bg-emerald-50 border-r-4 border-emerald-400 rounded-l-lg p-3 text-sm text-emerald-900'>התשובה אל {html.escape(to)} {done}.</div>"
        except Exception as e:
            print(f"[ERROR] mail reply: {e}", file=sys.stderr)
            notice = "<div class='bg-rose-50 border-r-4 border-rose-400 rounded-l-lg p-3 text-sm text-rose-900'>לא הצלחתי לשלוח. נסה שוב או פתח ב-Gmail.</div>"
    try:
        message = gmail_agent.get_message(message_id)
    except Exception as e:
        print(f"[ERROR] mail view: {e}", file=sys.stderr)
        return redirect(gmail_agent.GMAIL_WEB_URL.format(message_id))
    return MAIL_PAGE.format(
        head=PWA_HEAD_TAGS,
        notice=notice,
        subject=html.escape(message["subject"]),
        sender=html.escape(message["from"]),
        date=html.escape(message["date"]),
        text=html.escape(message["text"][:20000]),
        web_url=html.escape(message["web_url"]),
        reply_to=html.escape(message["reply_to"]),
        reply_to_js=html.escape(message["reply_to"]).replace("'", "").replace("\\", ""),
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
        flash("Please enter a request.")
        return redirect(url_for("dashboard"))

    try:
        result = action_agent.handle_request(user_text, tz_name)
    except Exception as e:
        print(f"[ERROR] agent_action: {e}", file=sys.stderr)
        result = "משהו השתבש בביצוע הפעולה."

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
