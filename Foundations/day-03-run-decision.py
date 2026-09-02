import requests

URL = "https://api.open-meteo.com/v1/forecast"


def get_weather(latitude, longitude):
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current_weather": True,
    }
    response = requests.get(URL, params=params)
    data = response.json()
    temperature = data["current_weather"]["temperature"]
    windspeed = data["current_weather"]["windspeed"]
    return temperature, windspeed


def should_run_outside(temperature, windspeed):
    if temperature > 32 or windspeed > 30:
        return False
    else:
        return True


temperature, windspeed = get_weather(32.08, 34.78)

if should_run_outside(temperature, windspeed):
    print("Good conditions to run outside today")
else:
    print("Not ideal for running today")

print(f"Temperature: {temperature}°C")
print(f"Wind speed: {windspeed} km/h")
