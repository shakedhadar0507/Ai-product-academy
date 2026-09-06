import requests

DEFAULT_LATITUDE = 32.08
DEFAULT_LONGITUDE = 34.78


def get_current_weather(latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    current = response.json()["current"]
    return {
        "temperature": current["temperature_2m"],
        "humidity": current["relative_humidity_2m"],
        "windspeed": current["wind_speed_10m"],
    }
