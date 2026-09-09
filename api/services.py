import os
import requests
from django.conf import settings

def fetch_live_rainfall(lat: float, lon: float) -> dict:
    """
    Fetches current weather metrics from OpenWeatherMap using coordinates.
    Extracts the exact rainfall accumulation (mm) from the past 1 hour.
    """
    api_key = os.environ.get("OPENWEATHER_API_KEY", getattr(settings, "OPENWEATHER_API_KEY", "PLACEHOLDER_KEY"))

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": "metric"
    }

    response = None
    try:
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()

        rain_data = data.get("rain", {})
        rainfall_1h = rain_data.get("1h", 0.0)

        return {
            "success": True,
            "rainfall_mm": float(rainfall_1h),
            "humidity": data.get("main", {}).get("humidity", 0),
            "temperature": data.get("main", {}).get("temp", 0.0),
            "raw_data": data
        }

    except requests.exceptions.RequestException as e:
        # DEBUG: prints the real reason the weather call failed (temporary, added during debugging)
        print(f"[WEATHER ERROR] {e}")
        if response is not None:
            print(f"[WEATHER ERROR] Status: {response.status_code}, Body: {response.text}")

        if api_key == "PLACEHOLDER_KEY" or (response is not None and response.status_code == 401):
            return {
                "success": True,
                "rainfall_mm": 12.5,
                "humidity": 85,
                "temperature": 26.5,
                "note": "Using mock development data (Invalid/Missing API Key)"
            }

        return {
            "success": False,
            "error": str(e)
        }
