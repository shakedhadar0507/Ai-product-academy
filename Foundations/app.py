import os
import secrets
import sys
from datetime import date, timedelta

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

from agents import (
    action_agent,
    brief_agent,
    calendar_agent,
    formatting,
    garmin_agent,
    google_auth,
    push_agent,
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
)

# The home-screen app can't send a Basic Auth header, so login is a form that
# sets a 30-day session cookie. The PWA files must load before login.
# The scheduled check authenticates with CRON_SECRET instead.
PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/sw.js", "/privacy", "/tasks/check-uni-mail"}


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
      <script src="https://cdn.tailwindcss.com"></script>
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
    return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")


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
    message_ids = []
    page_token = None
    while True:
        response = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=500
        ).execute()
        message_ids.extend(message["id"] for message in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    subjects = []
    for message_id in message_ids[:sample_size]:
        message = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["Subject"],
        ).execute()
        subjects.append(get_header(message["payload"]["headers"], "Subject"))

    return len(message_ids), subjects


def render_card(icon, title, body_html):
    return f"""
    <section class="bg-white rounded-2xl shadow-md hover:shadow-lg transition-shadow duration-300 p-6 flex flex-col gap-4">
      <h2 class="flex items-center gap-2 text-lg font-semibold text-slate-800">
        <span class="text-2xl">{icon}</span> {title}
      </h2>
      <div class="text-slate-600">{body_html}</div>
    </section>
    """


def render_error(message):
    return f'<p class="text-sm text-rose-500 italic">{message}</p>'


def render_list(items_html):
    return '<ul class="flex flex-col gap-2">' + "".join(
        f'<li class="border-b border-slate-100 last:border-0 pb-2 last:pb-0">{item}</li>' for item in items_html
    ) + "</ul>"


def render_email_bucket(name, count, subjects):
    if count == 0:
        body = "<p class='text-slate-400 text-sm'>No emails</p>"
    else:
        body = render_list([f"<span class='text-sm'>{subject}</span>" for subject in subjects])
    return f"""
    <div>
      <p class="text-sm font-semibold text-slate-700 mb-1">{name} <span class="text-slate-400 font-normal">({count})</span></p>
      {body}
    </div>
    """


@app.route("/")
def dashboard():
    cards = []
    tz_name = formatting.resolve_tz_name(request.cookies.get(USER_TZ_COOKIE, formatting.DEFAULT_TZ_NAME))

    weather_result = None
    try:
        conditions = weather_agent.get_running_conditions(LATITUDE, LONGITUDE)
        weather_result = weather_agent.get_current_weather(LATITUDE, LONGITUDE)
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
    cards.append(render_card("🌤️", "Run Weather · Tel Aviv", body))

    days_remaining = (MARATHON_DATE - formatting.today_in_tz(tz_name)).days
    if days_remaining > 0:
        countdown_html = (
            f"<span class='text-3xl font-bold text-indigo-600'>{days_remaining}</span> "
            f"<span class='text-slate-500'>days to go</span>"
        )
    elif days_remaining == 0:
        countdown_html = "<span class='text-lg font-semibold text-indigo-600'>Race day is today! 🎉</span>"
    else:
        countdown_html = "<p class='text-slate-400'>The marathon has passed.</p>"
    body = (
        f"<div class='flex flex-col gap-1'>{countdown_html}"
        f"<p class='text-sm text-slate-400'>NYC Marathon &mdash; {MARATHON_DATE.strftime('%d/%m/%Y')}</p></div>"
    )
    cards.append(render_card("🏁", "Marathon Countdown", body))

    if push_agent.is_configured():
        body = (
            "<p id='push-status' class='text-sm text-slate-500'>Get an alert on this device when something needs you.</p>"
            "<button id='push-button' data-key='" + push_agent.public_key() + "' "
            "class='mt-3 bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg px-4 py-2 text-sm font-medium'>"
            "Enable notifications</button>"
        )
        cards.append(render_card("🔔", "Notifications", body))

    calendar_result = None
    try:
        events = calendar_agent.get_data(tz_name)
        calendar_result = events
        insight = calendar_agent.get_insight(events, tz_name)
        if not events:
            events_html = "<p class='text-slate-400'>No events found for today.</p>"
        else:
            items = []
            for event in events:
                time_range = formatting.format_time_range(
                    event["start"].get("dateTime"), event["end"].get("dateTime")
                )
                title = event.get("summary", "(no title)")
                items.append(f"<span class='text-slate-400'>{time_range}</span> &mdash; {title}")
            events_html = render_list(items)
        insight_html = f"""
        <div class="mt-3 bg-indigo-50 border-l-4 border-indigo-400 rounded-r-lg p-3">
          <p class="text-xs uppercase tracking-wide text-indigo-500 font-semibold mb-1">AI Insight</p>
          <p class="text-indigo-900 text-sm">{insight}</p>
        </div>
        """
        body = events_html + insight_html
    except Exception as e:
        print(f"[ERROR] calendar: {e}", file=sys.stderr)
        body = render_error("Could not fetch calendar events")
    cards.append(render_card("📅", "Today's Calendar", body))

    email_result = None
    try:
        creds = google_auth.get_credentials(google_auth.SCOPES)
        service = build("gmail", "v1", credentials=creds)

        bucket_data = []
        sections = []
        for label_name, query in EMAIL_LABEL_BUCKETS:
            count, subjects = get_email_bucket(service, query)
            bucket_data.append((label_name, count, subjects))
            sections.append(render_email_bucket(label_name, count, subjects))

        other_count, other_subjects = get_email_bucket(service, OTHER_EMAIL_QUERY)
        bucket_data.append(("Everything Else", other_count, other_subjects))
        sections.append(render_email_bucket("Everything Else", other_count, other_subjects))

        email_result = bucket_data
        body = "<div class='flex flex-col gap-4'>" + "".join(sections) + "</div>"
    except Exception as e:
        print(f"[ERROR] inbox: {e}", file=sys.stderr)
        body = render_error("Could not fetch emails")
    cards.append(render_card("📧", "Inbox Overview", body))

    training_result = None
    garmin_data = None
    if garmin_agent.is_configured():
        try:
            garmin_data = garmin_agent.get_data(formatting.today_in_tz(tz_name))
            training_result = garmin_agent.as_training_log(garmin_data["runs"])
        except Exception as e:
            print(f"[ERROR] garmin: {e}", file=sys.stderr)

    try:
        brief = brief_agent.get_insight(calendar_result, weather_result, email_result, training_result, tz_name)
        brief_body = f"<p class='text-lg leading-relaxed'>{brief}</p>"
    except Exception as e:
        print(f"[ERROR] daily_brief: {e}", file=sys.stderr)
        brief_body = "<p class='text-lg text-indigo-100 italic'>Could not generate daily brief</p>"

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

    cards.insert(0, f"""
    <section class="md:col-span-2 lg:col-span-3 bg-white rounded-2xl shadow-md hover:shadow-lg transition-shadow duration-300 p-6 flex flex-col gap-4">
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
               class="flex-1 border border-slate-200 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300 transition-shadow">
        <div class="flex gap-2">
          <button type="button" id="mic-button" {mic_disabled_attr} title="{mic_title}"
                  class="w-11 h-11 shrink-0 flex items-center justify-center rounded-full text-lg transition-all duration-200 active:scale-90 {mic_extra_class}">
            🎤
          </button>
          <button type="submit"
                  class="flex-1 sm:flex-none bg-indigo-500 hover:bg-indigo-600 active:scale-95 text-white rounded-lg px-5 py-2.5 text-sm font-medium transition-all duration-150 shadow-sm hover:shadow">
            Send
          </button>
        </div>
      </form>
    </section>
    """)
    cards.insert(0, f"""
    <section class="md:col-span-2 lg:col-span-3 bg-gradient-to-r from-indigo-600 to-purple-600 text-white rounded-2xl shadow-lg hover:shadow-xl transition-shadow duration-300 p-8 flex flex-col gap-3">
      <h2 class="flex items-center gap-2 text-xl font-bold">
        <span class="text-3xl">🧭</span> Daily Brief
      </h2>
      {brief_body}
    </section>
    """)

    today_local = formatting.today_in_tz(tz_name)
    planned = garmin_agent.get_planned_workout(today_local)
    plan_html = (
        f"<p class='text-xs uppercase tracking-wide text-slate-400 font-semibold mb-1'>Today's plan</p>"
        f"<p dir='rtl' class='text-slate-800 font-medium'>{planned or 'אין אימון מתוכנן'}</p>"
    )
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
    body = plan_html + garmin_html

    cards.append(render_card("🏃", "Today's Training", body))

    if garmin_data is not None:
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
        cards.append(render_card("📈", "Running Volume", body))

    now_local = formatting.now_in_tz(tz_name)
    header_subtitle = f"{now_local.strftime('%A, %B')} {now_local.day}, {now_local.year}"

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Personal Dashboard</title>
      {PWA_HEAD_TAGS}
      <script>
        (function() {{
          try {{
            // IANA zone names only ever contain letters, digits, '/', '_', '+', '-' —
            // all valid unencoded in a cookie value — so this is intentionally stored
            // raw, not percent-encoded, and must be read back raw (no decodeURIComponent)
            // for the comparison below to actually match and to match what the server reads.
            var detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
            var match = document.cookie.match(/(?:^|;\\s*)user_tz=([^;]*)/);
            var current = match ? match[1] : null;
            if (detected && detected !== current) {{
              document.cookie = "user_tz=" + detected + ";path=/;max-age=31536000;SameSite=Lax";
              location.reload();
            }}
          }} catch (e) {{}}
        }})();
      </script>
      <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-100 min-h-screen">
      <header class="bg-white shadow-sm">
        <div class="max-w-6xl mx-auto px-6 py-5 flex items-start justify-between gap-4">
          <div>
            <h1 class="text-2xl font-bold text-slate-800">📊 Personal Dashboard</h1>
            <p class="text-sm text-slate-400 mt-0.5">{header_subtitle}</p>
          </div>
          <a href="/logout" class="text-sm text-slate-400 hover:text-slate-600">Sign out</a>
        </div>
      </header>
      <main class="max-w-6xl mx-auto px-6 py-8">
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {"".join(cards)}
        </div>
      </main>
      <script>
        (function() {{
          var button = document.getElementById('push-button');
          var status = document.getElementById('push-status');
          if (!button) return;
          if (!('serviceWorker' in navigator) || !('PushManager' in window)) {{
            status.textContent = 'On iPhone, open Shaked Hub from the home screen to enable notifications.';
            button.remove();
            return;
          }}
          function toBytes(base64) {{
            var padded = (base64 + '='.repeat((4 - base64.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
            return Uint8Array.from(atob(padded), function(c) {{ return c.charCodeAt(0); }});
          }}
          navigator.serviceWorker.ready.then(function(reg) {{
            return reg.pushManager.getSubscription();
          }}).then(function(existing) {{
            if (existing && Notification.permission === 'granted') {{
              status.textContent = 'Notifications are on for this device ✅';
              button.remove();
            }}
          }});
          button.addEventListener('click', function() {{
            button.disabled = true;
            Notification.requestPermission().then(function(permission) {{
              if (permission !== 'granted') throw new Error('denied');
              return navigator.serviceWorker.ready;
            }}).then(function(reg) {{
              return reg.pushManager.subscribe({{ userVisibleOnly: true, applicationServerKey: toBytes(button.dataset.key) }});
            }}).then(function(subscription) {{
              return fetch('/push/subscribe', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(subscription)
              }});
            }}).then(function(r) {{
              if (!r.ok) throw new Error('save failed');
              status.textContent = 'Notifications are on for this device ✅';
              button.remove();
            }}).catch(function(e) {{
              status.textContent = e.message === 'denied'
                ? 'Notifications are blocked. Allow them in Settings > Notifications > Shaked Hub.'
                : 'Could not enable notifications. Try again.';
              button.disabled = false;
            }});
          }});
        }})();
      </script>
      <script>
        (function() {{
          var micButton = document.getElementById('mic-button');
          var textInput = document.getElementById('agent-text-input');
          if (!micButton || micButton.disabled || !textInput) return;

          var mediaRecorder = null;
          var chunks = [];
          var recording = false;
          var idleClasses = ['bg-slate-100', 'hover:bg-indigo-100', 'text-slate-600', 'hover:text-indigo-600'];
          var recordingClasses = ['bg-rose-500', 'text-white', 'animate-pulse'];

          function setRecordingStyle(isRecording) {{
            idleClasses.forEach(function(c) {{ micButton.classList.toggle(c, !isRecording); }});
            recordingClasses.forEach(function(c) {{ micButton.classList.toggle(c, isRecording); }});
          }}

          micButton.addEventListener('click', function() {{
            if (!recording) {{
              navigator.mediaDevices.getUserMedia({{ audio: true }}).then(function(stream) {{
                mediaRecorder = new MediaRecorder(stream);
                chunks = [];
                mediaRecorder.ondataavailable = function(e) {{ if (e.data.size > 0) chunks.push(e.data); }};
                mediaRecorder.onstop = function() {{
                  stream.getTracks().forEach(function(t) {{ t.stop(); }});
                  setRecordingStyle(false);
                  micButton.textContent = '⏳';
                  micButton.disabled = true;
                  var blob = new Blob(chunks, {{ type: 'audio/webm' }});
                  var formData = new FormData();
                  formData.append('audio', blob, 'recording.webm');
                  fetch('/transcribe', {{ method: 'POST', body: formData }})
                    .then(function(r) {{ return r.json(); }})
                    .then(function(data) {{
                      if (data.text) {{ textInput.value = data.text; textInput.focus(); }}
                    }})
                    .catch(function() {{}})
                    .finally(function() {{
                      micButton.textContent = '🎤';
                      micButton.disabled = false;
                    }});
                }};
                mediaRecorder.start();
                recording = true;
                micButton.textContent = '⏹️';
                setRecordingStyle(true);
              }}).catch(function() {{
                micButton.title = 'Microphone access denied';
              }});
            }} else {{
              mediaRecorder.stop();
              recording = false;
            }}
          }});
        }})();
      </script>
    </body>
    </html>
    """


@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    if not push_agent.is_configured():
        return jsonify({"error": "Push not configured"}), 503
    subscription = request.get_json(silent=True) or {}
    if not subscription.get("endpoint"):
        return jsonify({"error": "Missing subscription"}), 400
    try:
        push_agent.save_subscription(subscription)
        push_agent.send_to_all("🔔 Shaked Hub", "ההתראות פועלות במכשיר הזה.")
        return jsonify({"ok": True})
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
