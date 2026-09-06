import sys

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

LATITUDE = 32.08
LONGITUDE = 34.78


def get_data(latitude=LATITUDE, longitude=LONGITUDE):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
    }
    response = requests.get(url, params=params)
    current = response.json()["current"]
    return {
        "temperature": current["temperature_2m"],
        "humidity": current["relative_humidity_2m"],
        "windspeed": current["wind_speed_10m"],
    }


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
