import requests

URL = "https://api.open-meteo.com/v1/forecast"
params = {
    "latitude": 32.08,
    "longitude": 34.78,
    "current_weather": True,
}

response = requests.get(URL, params=params)
data = response.json()

print(data)

temperature = data["current_weather"]["temperature"]
windspeed = data["current_weather"]["windspeed"]
print(f"Current temperature in Tel Aviv: {temperature}°C")
print(f"Wind speed: {windspeed} km/h")
