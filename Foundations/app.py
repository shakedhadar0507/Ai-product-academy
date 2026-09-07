import os
import secrets
import sqlite3
import sys
from datetime import date

from dotenv import load_dotenv
from flask import Flask, Response, redirect, request, url_for
from googleapiclient.discovery import build

from agents import brief_agent, calendar_agent, formatting, google_auth, running_agent, weather_agent

load_dotenv()

app = Flask(__name__)


@app.before_request
def require_basic_auth():
    username = os.environ.get("DASHBOARD_USERNAME")
    password = os.environ.get("DASHBOARD_PASSWORD")
    auth = request.authorization
    if (
        not username
        or not password
        or not auth
        or not secrets.compare_digest(auth.username or "", username)
        or not secrets.compare_digest(auth.password or "", password)
    ):
        return Response(
            "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Personal Dashboard"'}
        )

LATITUDE = 32.08
LONGITUDE = 34.78
DB_FILE = "training_log.db"
MARATHON_DATE = date(2026, 11, 1)


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


RUNS_TABLE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        distance_km REAL NOT NULL,
        duration_min INTEGER NOT NULL,
        notes TEXT
    )
"""


def get_training_log():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(RUNS_TABLE_SCHEMA)
        rows = conn.execute(
            "SELECT date, distance_km, duration_min, notes FROM runs ORDER BY date"
        ).fetchall()
    return rows


def add_training_run(run_date, distance_km, duration_min, notes):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(RUNS_TABLE_SCHEMA)
        conn.execute(
            "INSERT INTO runs (date, distance_km, duration_min, notes) VALUES (?, ?, ?, ?)",
            (run_date, distance_km, duration_min, notes),
        )


def render_card(icon, title, body_html):
    return f"""
    <section class="bg-white rounded-2xl shadow-md p-6 flex flex-col gap-4">
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

    weather_result = None
    try:
        weather_result = weather_agent.get_current_weather(LATITUDE, LONGITUDE)
        body = render_list([
            f"Temperature: <span class='font-medium text-slate-800'>{weather_result['temperature']}&deg;C</span>",
            f"Wind speed: <span class='font-medium text-slate-800'>{weather_result['windspeed']} km/h</span>",
        ])
    except Exception as e:
        print(f"[ERROR] weather: {e}", file=sys.stderr)
        body = render_error("Could not fetch weather")
    cards.append(render_card("☀️", "Weather in Tel Aviv", body))

    days_remaining = (MARATHON_DATE - formatting.today_in_israel()).days
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

    calendar_result = None
    try:
        events = calendar_agent.get_data()
        calendar_result = events
        insight = calendar_agent.get_insight(events)
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
    try:
        rows = get_training_log()
        training_result = rows
        if not rows:
            body = "<p class='text-slate-400'>No runs logged yet.</p>"
        else:
            items = []
            total_distance = 0
            for run_date, distance_km, duration_min, notes in rows:
                items.append(
                    f"<span class='text-slate-400'>{formatting.format_date(run_date)}</span> &mdash; "
                    f"<span class='font-medium text-slate-800'>{distance_km} km</span> "
                    f"in {duration_min} min &middot; {notes}"
                )
                total_distance += distance_km
            body = render_list(items) + (
                f"<p class='mt-3 text-sm font-semibold text-slate-700'>"
                f"Total distance: {total_distance} km</p>"
            )
    except Exception as e:
        print(f"[ERROR] training_log: {e}", file=sys.stderr)
        body = render_error("Could not fetch training log")

    try:
        brief = brief_agent.get_insight(calendar_result, weather_result, email_result, training_result)
        brief_body = f"<p class='text-lg leading-relaxed'>{brief}</p>"
    except Exception as e:
        print(f"[ERROR] daily_brief: {e}", file=sys.stderr)
        brief_body = "<p class='text-lg text-indigo-100 italic'>Could not generate daily brief</p>"
    cards.insert(0, f"""
    <section class="md:col-span-2 lg:col-span-3 bg-gradient-to-r from-indigo-600 to-purple-600 text-white rounded-2xl shadow-lg p-8 flex flex-col gap-3">
      <h2 class="flex items-center gap-2 text-xl font-bold">
        <span class="text-3xl">🧭</span> Daily Brief
      </h2>
      {brief_body}
    </section>
    """)

    try:
        weather_data = running_agent.get_data()
        running_insight = running_agent.get_insight(weather_data)
        insight_html = f"""
        <div class="mt-3 bg-emerald-50 border-l-4 border-emerald-400 rounded-r-lg p-3">
          <p class="text-xs uppercase tracking-wide text-emerald-600 font-semibold mb-1">Should You Run Today?</p>
          <p class="text-emerald-900 text-sm">{running_insight}</p>
        </div>
        """
    except Exception as e:
        print(f"[ERROR] running_insight: {e}", file=sys.stderr)
        insight_html = render_error("Could not generate running insight")
    body += insight_html

    body += """
    <form method="POST" action="/add-run" class="mt-4 border-t border-slate-100 pt-4 flex flex-col gap-2">
      <div class="grid grid-cols-2 gap-2">
        <input type="date" name="date" required
               class="col-span-2 border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
        <input type="number" step="0.01" min="0" name="distance_km" placeholder="Distance (km)" required
               class="border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
        <input type="number" min="0" name="duration_min" placeholder="Duration (min)" required
               class="border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
        <input type="text" name="notes" placeholder="Notes"
               class="col-span-2 border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
      </div>
      <button type="submit"
              class="bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg px-4 py-2 text-sm font-medium transition-colors">
        Add Run
      </button>
    </form>
    """
    cards.append(render_card("🏃", "Training Log", body))

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Personal Dashboard</title>
      <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-100 min-h-screen">
      <header class="bg-white shadow-sm">
        <div class="max-w-6xl mx-auto px-6 py-5">
          <h1 class="text-2xl font-bold text-slate-800">📊 Personal Dashboard</h1>
        </div>
      </header>
      <main class="max-w-6xl mx-auto px-6 py-8">
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {"".join(cards)}
        </div>
      </main>
    </body>
    </html>
    """


@app.route("/add-run", methods=["POST"])
def add_run():
    run_date = request.form["date"]
    distance_km = float(request.form["distance_km"])
    duration_min = int(request.form["duration_min"])
    notes = request.form.get("notes", "")
    add_training_run(run_date, distance_km, duration_min, notes)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
