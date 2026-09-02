import datetime
import os
import sys

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

sys.stdout.reconfigure(encoding="utf-8")

LATITUDE = 32.08
LONGITUDE = 34.78
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


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


def get_calendar_credentials():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token_file:
            token_file.write(creds.to_json())
    return creds


def get_todays_events():
    creds = get_calendar_credentials()
    service = build("calendar", "v3", credentials=creds)

    now = datetime.datetime.now(datetime.timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    events_result = service.events().list(
        calendarId="primary",
        timeMin=start_of_day,
        timeMax=end_of_day,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return events_result.get("items", [])


def main():
    today = datetime.date.today().isoformat()

    print("=== Morning Briefing ===")
    print(f"Date: {today}")
    print()
    print("Weather in Tel Aviv:")
    try:
        temperature, windspeed = get_weather(LATITUDE, LONGITUDE)
        print(f"  Temperature: {temperature}°C")
        print(f"  Wind speed: {windspeed} km/h")
    except Exception:
        print("Could not fetch weather")
    print()
    print("Today's calendar events:")
    try:
        events = get_todays_events()
        if not events:
            print("  No events found for today.")
        else:
            for event in events:
                start = event["start"].get("dateTime", event["start"].get("date"))
                title = event.get("summary", "(no title)")
                print(f"  {start} - {title}")
    except Exception:
        print("Could not fetch calendar events")


if __name__ == "__main__":
    main()
