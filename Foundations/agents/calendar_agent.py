import datetime
import sys
import time
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv
from googleapiclient.discovery import build

from agents import formatting, google_auth

load_dotenv()

CACHE_TTL_SECONDS = 1800

_insight_cache = {}


def get_data(tz_name=formatting.DEFAULT_TZ_NAME, days_ahead=0):
    """Events of today, or of the day `days_ahead` days from today."""
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("calendar", "v3", credentials=creds)

    now = formatting.now_in_tz(tz_name) + datetime.timedelta(days=days_ahead)
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
    location=None,
    tz_name=formatting.DEFAULT_TZ_NAME,
):
    """Create an event, unless the same event (same title, start and end) already exists.

    The duplicate check makes this safe to call again with the same details: the chat used to
    re-run earlier requests and put the same event in the calendar several times."""
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
    if location:
        event_body["location"] = location

    existing = find_duplicate(service, summary, start_dt, end_dt)
    if existing:
        return _event_info(existing) | {"already_existed": True}

    created = service.events().insert(calendarId="primary", body=event_body).execute()
    return _event_info(created) | {"already_existed": False}


def _norm(text):
    return " ".join((text or "").split()).casefold()


def find_duplicate(service, summary, start_dt, end_dt):
    """An existing non-recurring-instance event with the same title, start and end, or None."""
    items = service.events().list(
        calendarId="primary", timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(),
        singleEvents=True, maxResults=50,
    ).execute().get("items", [])
    for item in items:
        if item.get("status") == "cancelled" or _norm(item.get("summary")) != _norm(summary):
            continue
        try:
            s = datetime.datetime.fromisoformat(item["start"]["dateTime"])
            e = datetime.datetime.fromisoformat(item["end"]["dateTime"])
        except (KeyError, ValueError):
            continue
        if s == start_dt and e == end_dt:
            return item
    return None


def list_events(tz_name=formatting.DEFAULT_TZ_NAME, days_ahead=0, query=None, days_span=1):
    """Events from the start of the day `days_ahead` from today, over `days_span` days."""
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("calendar", "v3", credentials=creds)
    day = formatting.now_in_tz(tz_name).replace(hour=0, minute=0, second=0, microsecond=0)
    start = day + datetime.timedelta(days=days_ahead)
    end = start + datetime.timedelta(days=max(1, days_span))
    params = dict(calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
                  singleEvents=True, orderBy="startTime", maxResults=100)
    if query:
        params["q"] = query
    return service.events().list(**params).execute().get("items", [])


def update_event(event_id, tz_name=formatting.DEFAULT_TZ_NAME, summary=None, date=None,
                 start_time=None, end_time=None, location=None, description=None):
    """Change an existing event. Only the fields given are changed; returns the event as Google now has it."""
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("calendar", "v3", credentials=creds)
    current = service.events().get(calendarId="primary", eventId=event_id).execute()
    patch = {}
    if summary:
        patch["summary"] = summary
    if location is not None:
        patch["location"] = location
    if description is not None:
        patch["description"] = description
    if date or start_time or end_time:
        tz = ZoneInfo(tz_name)
        old_start = datetime.datetime.fromisoformat(current["start"]["dateTime"]).astimezone(tz)
        old_end = datetime.datetime.fromisoformat(current["end"]["dateTime"]).astimezone(tz)
        day = date or old_start.strftime("%Y-%m-%d")
        new_start = datetime.datetime.strptime(f"{day} {start_time or old_start.strftime('%H:%M')}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        if end_time:
            new_end = datetime.datetime.strptime(f"{day} {end_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        else:
            new_end = new_start + (old_end - old_start)
        patch["start"] = {"dateTime": new_start.isoformat(), "timeZone": tz_name}
        patch["end"] = {"dateTime": new_end.isoformat(), "timeZone": tz_name}
    if not patch:
        return _event_info(current)
    updated = service.events().patch(calendarId="primary", eventId=event_id, body=patch).execute()
    return _event_info(updated)


def delete_event(event_id):
    """Delete an event and confirm it is gone. Returns the deleted event's details."""
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("calendar", "v3", credentials=creds)
    current = service.events().get(calendarId="primary", eventId=event_id).execute()
    service.events().delete(calendarId="primary", eventId=event_id).execute()
    after = service.events().get(calendarId="primary", eventId=event_id).execute()
    if after.get("status") != "cancelled":
        raise RuntimeError("Google still shows the event after deleting it")
    return _event_info(current)


def _event_info(created):
    return {
        "id": created.get("id"),
        "summary": created.get("summary"),
        "location": created.get("location"),
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
