import sys
import time

import anthropic
from dotenv import load_dotenv
from googleapiclient.discovery import build

from agents import formatting, google_auth

load_dotenv()

CACHE_TTL_SECONDS = 600

_insight_cache = {}


def get_data():
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("calendar", "v3", credentials=creds)

    now = formatting.now_in_israel()
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


def get_insight(data):
    today = formatting.today_in_israel().isoformat()
    cached = _insight_cache.get(today)
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

    _insight_cache[today] = (time.time(), insight)
    return insight
