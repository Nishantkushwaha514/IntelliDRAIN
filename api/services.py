import logging
import os

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def fetch_live_rainfall(lat: float, lon: float) -> dict:
    """
    Fetches current weather metrics from OpenWeatherMap using coordinates.
    Extracts the exact rainfall accumulation (mm) from the past 1 hour.

    Returns:
        Success: {"success": True, "rainfall_mm": float, "humidity": int,
                  "temperature": float, "raw_data": dict}
        Failure: {"success": False, "error": str}

    Never returns fake or mock data. Callers must handle success=False
    by treating rainfall as unavailable (not as 0.0).
    """
    api_key = os.environ.get(
        "OPENWEATHER_API_KEY",
        getattr(settings, "OPENWEATHER_API_KEY", ""),
    )

    if not api_key:
        logger.warning("OPENWEATHER_API_KEY is not set; weather unavailable.")
        return {"success": False, "error": "OPENWEATHER_API_KEY is not configured"}

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": "metric",
    }

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
            "raw_data": data,
        }

    except requests.exceptions.RequestException as e:
        logger.warning("Weather API request failed: %s", e)
        return {"success": False, "error": str(e)}