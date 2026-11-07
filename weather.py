import logging
import os
import time
from typing import Optional, Tuple
import pandas as pd
import requests

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Simple in-memory cache for weather forecasts to prevent exceeding rate limits
_WEATHER_CACHE = {
    "timestamp": 0.0,
    "cache_ttl_seconds": 600.0,  # 10 minutes cache
    "lat": None,
    "lon": None,
    "data": None  # (DataFrame, timezone_str)
}


class WeatherServiceError(Exception):
    """Raised when weather forecast data cannot be fetched or parsed."""
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def get_location_config() -> Tuple[float, float, str]:
    """Retrieves latitude, longitude, and location name with defaults."""
    try:
        lat = float(os.getenv("LATITUDE", "13.0827"))
    except ValueError:
        lat = 13.0827

    try:
        lon = float(os.getenv("LONGITUDE", "80.2707"))
    except ValueError:
        lon = 80.2707

    location_name = os.getenv("LOCATION_NAME", "Chennai, India")
    return lat, lon, location_name


def get_hourly_forecast(force_refresh: bool = False) -> Tuple[pd.DataFrame, str]:
    """
    Fetches 48-hour hourly weather forecast from Open-Meteo for the configured location.
    
    Inputs utilized:
      - shortwave_radiation: Solar irradiance (W/m²)
      - temperature_2m: Ambient surface temperature (°C)
      - cloud_cover: Total cloud cover percentage (0-100%)
      
    Returns:
      (DataFrame with columns ['time', 'radiation', 'ambient_temp', 'cloud_cover'], timezone_name)
      
    Raises:
      WeatherServiceError: If internet is down, request times out, or response is malformed.
    """
    lat, lon, _ = get_location_config()
    now = time.time()

    # Check cache
    if not force_refresh and _WEATHER_CACHE["data"] is not None:
        cached_age = now - _WEATHER_CACHE["timestamp"]
        if cached_age < _WEATHER_CACHE["cache_ttl_seconds"] and _WEATHER_CACHE["lat"] == lat and _WEATHER_CACHE["lon"] == lon:
            cached_frame, cached_tz = _WEATHER_CACHE["data"]
            return cached_frame.copy(), cached_tz

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "shortwave_radiation,temperature_2m,cloud_cover",
        "forecast_days": 2,
        "timezone": "auto"
    }

    try:
        response = requests.get(OPEN_METEO_URL, params=params, timeout=10)
    except requests.exceptions.Timeout:
        logger.warning("Open-Meteo request timed out.")
        raise WeatherServiceError("Weather service request timed out. Check network connection.")
    except requests.exceptions.ConnectionError:
        logger.warning("Failed to connect to Open-Meteo.")
        raise WeatherServiceError("Unable to reach Open-Meteo weather service. Device may be offline.")
    except requests.exceptions.RequestException as err:
        logger.warning(f"Weather request error: {err}")
        raise WeatherServiceError(f"Weather service network error: {err}")

    if response.status_code != 200:
        raise WeatherServiceError(
            f"Weather service returned HTTP {response.status_code}: {response.text[:120]}",
            status_code=response.status_code
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise WeatherServiceError(f"Malformed JSON from weather service: {exc}")

    if "hourly" not in payload:
        raise WeatherServiceError("Weather service response missing 'hourly' forecast block.")

    hourly = payload["hourly"]
    required_keys = ["time", "shortwave_radiation", "temperature_2m", "cloud_cover"]
    missing = [k for k in required_keys if k not in hourly]
    if missing:
        raise WeatherServiceError(f"Weather service response missing hourly fields: {missing}")

    try:
        frame = pd.DataFrame({
            "time": pd.to_datetime(hourly["time"]),
            "radiation": [float(r) if r is not None else 0.0 for r in hourly["shortwave_radiation"]],
            "ambient_temp": [float(t) if t is not None else 25.0 for t in hourly["temperature_2m"]],
            "cloud_cover": [float(c) if c is not None else 0.0 for c in hourly["cloud_cover"]]
        })
    except Exception as exc:
        raise WeatherServiceError(f"Error parsing weather forecast data points: {exc}")

    timezone_str = payload.get("timezone", "UTC")

    # Update cache
    _WEATHER_CACHE["timestamp"] = now
    _WEATHER_CACHE["lat"] = lat
    _WEATHER_CACHE["lon"] = lon
    _WEATHER_CACHE["data"] = (frame.copy(), timezone_str)

    return frame, timezone_str
