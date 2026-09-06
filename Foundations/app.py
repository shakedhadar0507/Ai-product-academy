import os
import sqlite3

import requests
from flask import Flask
from googleapiclient.discovery import build

from agents import calendar_agent, google_auth

app = Flask(__name__)

LATITUDE = 32.08
LONGITUDE = 34.78
DB_FILE = "training_log.db"


def get_weather(latitude, longitude):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current_weather": True,
    }
    response = requests.get(url, params=params)
    data = response.json()
    temperature = data["current_weather"]["temperature"]
    windspeed = data["current_weather"]["windspeed"]
    return temperature, windspeed


def get_header(headers, name):
    for header in headers:
        if header["name"].lower() == name.lower():
            return header["value"]
    return "(unknown)"


def get_unread_emails(creds, max_results=5):
    service = build("gmail", "v1", credentials=creds)

    results = service.users().messages().list(
        userId="me",
        labelIds=["UNREAD", "INBOX"],
        maxResults=max_results,
    ).execute()

    message_refs = results.get("messages", [])

    emails = []
    for ref in message_refs:
        message = service.users().messages().get(
            userId="me",
            id=ref["id"],
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        emails.append(message)

    return emails


def get_training_log():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                distance_km REAL NOT NULL,
                duration_min INTEGER NOT NULL,
                notes TEXT
            )
            """
        )
        rows = conn.execute(
            "SELECT date, distance_km, duration_min, notes FROM runs ORDER BY date"
        ).fetchall()
    return rows


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


@app.route("/")
def dashboard():
    cards = []

    try:
        temperature, windspeed = get_weather(LATITUDE, LONGITUDE)
        body = render_list([
            f"Temperature: <span class='font-medium text-slate-800'>{temperature}&deg;C</span>",
            f"Wind speed: <span class='font-medium text-slate-800'>{windspeed} km/h</span>",
        ])
    except Exception:
        body = render_error("Could not fetch weather")
    cards.append(render_card("☀️", "Weather in Tel Aviv", body))

    try:
        events = calendar_agent.get_data()
        insight = calendar_agent.get_insight(events)
        if not events:
            events_html = "<p class='text-slate-400'>No events found for today.</p>"
        else:
            items = []
            for event in events:
                start = event["start"].get("dateTime", event["start"].get("date"))
                title = event.get("summary", "(no title)")
                items.append(f"<span class='text-slate-400'>{start}</span> &mdash; {title}")
            events_html = render_list(items)
        insight_html = f"""
        <div class="mt-3 bg-indigo-50 border-l-4 border-indigo-400 rounded-r-lg p-3">
          <p class="text-xs uppercase tracking-wide text-indigo-500 font-semibold mb-1">AI Insight</p>
          <p class="text-indigo-900 text-sm">{insight}</p>
        </div>
        """
        body = events_html + insight_html
    except Exception:
        body = render_error("Could not fetch calendar events")
    cards.append(render_card("📅", "Today's Calendar", body))

    try:
        creds = google_auth.get_credentials(google_auth.SCOPES)
        emails = get_unread_emails(creds)
        if not emails:
            body = "<p class='text-slate-400'>No unread emails found.</p>"
        else:
            items = []
            for email in emails:
                headers = email["payload"]["headers"]
                sender = get_header(headers, "From")
                subject = get_header(headers, "Subject")
                items.append(f"<span class='font-medium text-slate-800'>{sender}</span><br>{subject}")
            body = render_list(items)
    except Exception:
        body = render_error("Could not fetch unread emails")
    cards.append(render_card("📧", "Unread Emails", body))

    try:
        rows = get_training_log()
        if not rows:
            body = "<p class='text-slate-400'>No runs logged yet.</p>"
        else:
            items = []
            total_distance = 0
            for date, distance_km, duration_min, notes in rows:
                items.append(
                    f"<span class='text-slate-400'>{date}</span> &mdash; "
                    f"<span class='font-medium text-slate-800'>{distance_km} km</span> "
                    f"in {duration_min} min &middot; {notes}"
                )
                total_distance += distance_km
            body = render_list(items) + (
                f"<p class='mt-3 text-sm font-semibold text-slate-700'>"
                f"Total distance: {total_distance} km</p>"
            )
    except Exception:
        body = render_error("Could not fetch training log")
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
