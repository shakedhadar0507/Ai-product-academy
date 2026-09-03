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
        rows = conn.execute(
            "SELECT date, distance_km, duration_min, notes FROM runs ORDER BY date"
        ).fetchall()
    return rows


@app.route("/")
def dashboard():
    html = ["<h1>Dashboard</h1>"]

    html.append("<h2>Weather in Tel Aviv</h2>")
    try:
        temperature, windspeed = get_weather(LATITUDE, LONGITUDE)
        html.append(f"<ul><li>Temperature: {temperature}&deg;C</li><li>Wind speed: {windspeed} km/h</li></ul>")
    except Exception:
        html.append("<p>Could not fetch weather</p>")

    html.append("<h2>Today's Calendar Events</h2>")
    try:
        events = calendar_agent.get_data()
        insight = calendar_agent.get_insight(events)
        if not events:
            html.append("<p>No events found for today.</p>")
        else:
            items = []
            for event in events:
                start = event["start"].get("dateTime", event["start"].get("date"))
                title = event.get("summary", "(no title)")
                items.append(f"<li>{start} - {title}</li>")
            html.append("<ul>" + "".join(items) + "</ul>")
        html.append(f"<p><em>Insight: {insight}</em></p>")
    except Exception:
        html.append("<p>Could not fetch calendar events</p>")

    html.append("<h2>Unread Emails</h2>")
    try:
        creds = google_auth.get_credentials(google_auth.SCOPES)
        emails = get_unread_emails(creds)
        if not emails:
            html.append("<p>No unread emails found.</p>")
        else:
            items = []
            for email in emails:
                headers = email["payload"]["headers"]
                sender = get_header(headers, "From")
                subject = get_header(headers, "Subject")
                items.append(f"<li>{sender} - {subject}</li>")
            html.append("<ul>" + "".join(items) + "</ul>")
    except Exception:
        html.append("<p>Could not fetch unread emails</p>")

    html.append("<h2>Training Log</h2>")
    try:
        rows = get_training_log()
        if not rows:
            html.append("<p>No runs logged yet.</p>")
        else:
            items = []
            total_distance = 0
            for date, distance_km, duration_min, notes in rows:
                items.append(f"<li>{date} - {distance_km} km in {duration_min} min - {notes}</li>")
                total_distance += distance_km
            html.append("<ul>" + "".join(items) + "</ul>")
            html.append(f"<p>Total distance: {total_distance} km</p>")
    except Exception:
        html.append("<p>Could not fetch training log</p>")

    return "\n".join(html)


if __name__ == "__main__":
    app.run(port=5000)
