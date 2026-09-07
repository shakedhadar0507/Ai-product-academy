import datetime
import sys
import time
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv
from googleapiclient.discovery import build

from agents import formatting, google_auth

load_dotenv()

CACHE_TTL_SECONDS = 600

_insight_cache = {}


def get_data(tz_name=formatting.DEFAULT_TZ_NAME):
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("calendar", "v3", credentials=creds)

    now = formatting.now_in_tz(tz_name)
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


COLOR_NAME_TO_ID = {
    "lavender": "1",
    "sage": "2",
    "grape": "3",
    "purple": "3",
    "flamingo": "4",
    "pink": "4",
    "banana": "5",
    "yellow": "5",
    "tangerine": "6",
    "orange": "6",
    "peacock": "7",
    "blue": "7",
    "graphite": "8",
    "gray": "8",
    "grey": "8",
    "blueberry": "9",
    "basil": "10",
    "green": "10",
    "tomato": "11",
    "red": "11",
}


def resolve_color_id(color_name):
    """Map a plain color name to a Google Calendar colorId, or None if unrecognized."""
    if not color_name:
        return None
    return COLOR_NAME_TO_ID.get(color_name.strip().lower())


def create_event(
    summary,
    date,
    start_time,
    end_time,
    description="",
    color=None,
    recurrence=None,
    attendees=None,
    tz_name=formatting.DEFAULT_TZ_NAME,
):
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("calendar", "v3", credentials=creds)

    tz = ZoneInfo(tz_name)
    start_dt = datetime.datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    end_dt = datetime.datetime.strptime(f"{date} {end_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)

    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
    }

    color_id = resolve_color_id(color)
    if color_id:
        event_body["colorId"] = color_id
    if recurrence:
        event_body["recurrence"] = [recurrence]
    if attendees:
        event_body["attendees"] = [{"email": email} for email in attendees]

    created = service.events().insert(calendarId="primary", body=event_body).execute()
    return {
        "id": created.get("id"),
        "summary": created.get("summary"),
        "htmlLink": created.get("htmlLink"),
        "start": created.get("start", {}).get("dateTime"),
        "end": created.get("end", {}).get("dateTime"),
        "colorId": created.get("colorId"),
        "recurrence": created.get("recurrence"),
        "attendees": [a.get("email") for a in created.get("attendees", [])] or None,
    }


def get_insight(data, tz_name=formatting.DEFAULT_TZ_NAME):
    cache_key = (tz_name, formatting.today_in_tz(tz_name).isoformat())
    cached = _insight_cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        client = anthropic.Anthropic()

        events_summary = "\n".join(
            f"- {event.get('summary', '(no title)')}: "
            f"{event['start'].get('dateTime', event['start'].get('date'))} to "
            f"{event['end'].get('dateTime', event['end'].get('date'))}"
            for event in data
        ) or "(no events today)"

        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=300,
            output_config={"effort": "low"},
            messages=[{
                "role": "user",
                "content": (
                    "Here is today's calendar. Focus only on events that genuinely "
                    "need attention: scheduling conflicts, back-to-back meetings with "
                    "no buffer between them, or significant events that need prep. "
                    "Briefly acknowledge trivial or passive items (like reading a "
                    "newsletter) without analyzing them, or skip them entirely if "
                    "there's nothing notable. If nothing needs attention, say so in "
                    "one short sentence instead of manufacturing a suggestion. Give "
                    "one calibrated, practical insight in Hebrew, max 2-3 "
                    "sentences:\n\n" + events_summary
                ),
            }],
        )

        insight = next(block.text for block in response.content if block.type == "text")
    except Exception as e:
        print(f"[ERROR] calendar_insight: {e}", file=sys.stderr)
        insight = "Could not generate insight"

    _insight_cache[cache_key] = (time.time(), insight)
    return insight
