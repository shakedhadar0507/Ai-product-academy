import datetime

import anthropic
from dotenv import load_dotenv
from googleapiclient.discovery import build

from agents import google_auth

load_dotenv()


def get_data():
    creds = google_auth.get_credentials(google_auth.SCOPES)
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


def get_insight(data):
    try:
        client = anthropic.Anthropic()

        events_summary = "\n".join(
            f"- {event.get('summary', '(no title)')} at "
            f"{event['start'].get('dateTime', event['start'].get('date'))}"
            for event in data
        ) or "(no events today)"

        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=300,
            output_config={"effort": "low"},
            messages=[{
                "role": "user",
                "content": (
                    "Given these calendar events for today, give one practical, "
                    "specific suggestion in Hebrew about time management or "
                    "conflicts, max 2 sentences:\n\n" + events_summary
                ),
            }],
        )

        return next(block.text for block in response.content if block.type == "text")
    except Exception:
        return "Could not generate insight"
