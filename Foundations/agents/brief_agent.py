import sys
import time

import anthropic
from dotenv import load_dotenv

from agents import formatting

load_dotenv()

CACHE_TTL_SECONDS = 600

_insight_cache = {}


def _summarize_calendar(calendar_data):
    if not calendar_data:
        return "No calendar data available."
    lines = [
        f"- {event.get('summary', '(no title)')}: "
        f"{event['start'].get('dateTime', event['start'].get('date'))} to "
        f"{event['end'].get('dateTime', event['end'].get('date'))}"
        for event in calendar_data
    ]
    return "\n".join(lines) if lines else "No events today."


def _summarize_weather(weather_data):
    if not weather_data:
        return "No weather data available."
    return (
        f"Temperature {weather_data['temperature']}°C, "
        f"humidity {weather_data['humidity']}%, "
        f"wind speed {weather_data['windspeed']} km/h."
    )


def _summarize_email(email_summary):
    if not email_summary:
        return "No email data available."
    return "\n".join(f"- {label}: {count} emails" for label, count, _ in email_summary)


def _summarize_training(training_summary):
    if not training_summary:
        return "No training log data available."
    last_date, last_distance, last_duration, last_notes = training_summary[-1]
    total_distance = sum(row[1] for row in training_summary)
    return (
        f"{len(training_summary)} runs logged, {total_distance} km total. "
        f"Most recent: {last_date}, {last_distance} km in {last_duration} min ({last_notes})."
    )


def get_insight(calendar_data, weather_data, email_summary, training_summary, tz_name=formatting.DEFAULT_TZ_NAME):
    cache_key = (tz_name, formatting.today_in_tz(tz_name).isoformat())
    cached = _insight_cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        client = anthropic.Anthropic()

        prompt = (
            "You're writing a short daily brief for someone by combining several "
            "data sources about their day. Some sources may be unavailable — just "
            "work with whatever is available, don't dwell on what's missing. Give a "
            "short, prioritized daily brief in Hebrew, max 3-4 sentences: what "
            "matters most today, any conflicts between the different areas, and one "
            "practical suggestion that ties the day together.\n\n"
            f"Calendar:\n{_summarize_calendar(calendar_data)}\n\n"
            f"Weather:\n{_summarize_weather(weather_data)}\n\n"
            f"Email:\n{_summarize_email(email_summary)}\n\n"
            f"Training log:\n{_summarize_training(training_summary)}"
        )

        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )

        insight = next(block.text for block in response.content if block.type == "text")
    except Exception as e:
        print(f"[ERROR] daily_brief: {e}", file=sys.stderr)
        insight = "Could not generate daily brief"

    _insight_cache[cache_key] = (time.time(), insight)
    return insight
