import sys

import anthropic
from dotenv import load_dotenv

from agents import weather_agent

load_dotenv()


def get_data():
    return weather_agent.get_current_weather()


def get_insight(data):
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

        return next(block.text for block in response.content if block.type == "text")
    except Exception as e:
        print(f"[ERROR] running_insight: {e}", file=sys.stderr)
        return "Could not generate running insight"
