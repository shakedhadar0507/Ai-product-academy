import sys
import time

import anthropic
from dotenv import load_dotenv

from agents import weather_agent

load_dotenv()

CACHE_TTL_SECONDS = 600

_insight_cache = {}


def get_data():
    return weather_agent.get_current_weather()


def get_insight(data):
    cached = _insight_cache.get("insight")
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        client = anthropic.Anthropic()

        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=300,
            output_config={"effort": "low"},
            messages=[{
                "role": "user",
                "content": (
                    "Today's weather: temperature "
                    f"{data['temperature']}°C, humidity {data['humidity']}%, "
                    f"wind speed {data['windspeed']} km/h. Give a short, practical "
                    "recommendation in Hebrew on whether today is good for running "
                    "and, if so, what time of day is best. Max 2 sentences."
                ),
            }],
        )

        insight = next(block.text for block in response.content if block.type == "text")
    except Exception as e:
        print(f"[ERROR] running_insight: {e}", file=sys.stderr)
        insight = "Could not generate running insight"

    _insight_cache["insight"] = (time.time(), insight)
    return insight
