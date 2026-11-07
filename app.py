from datetime import datetime, timezone
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from flask import Flask, jsonify, render_template, request

# Safe environment loading (handles absence of python-dotenv or missing .env)
def load_env_safe() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

load_env_safe()

from database import (
    calculate_today_energy,
    get_latest_reading,
    init_db,
    latest_readings,
    save_reading,
)
from ml_models import (
    DEMO_DISCLAIMER,
    deviation_percent,
    get_panel_rated_watts,
    load_models,
    predict_24h,
    predict_seasonal,
)
from weather import WeatherServiceError, get_hourly_forecast, get_location_config

app = Flask(__name__)

# Initialize database schema safely
init_db()

# Ensure demo models exist on disk
try:
    load_models()
except Exception as e:
    app.logger.warning(f"Initial model check: {e}")


def parse_finite_float(val: Any, field_name: str, min_val: Optional[float] = None, max_val: Optional[float] = None) -> float:
    """Parses and validates a finite float within physical limits."""
    if val is None:
        raise ValueError(f"'{field_name}' cannot be null.")
    try:
        f = float(val)
    except (ValueError, TypeError):
        raise ValueError(f"'{field_name}' must be a valid numeric value.")
    if math.isnan(f) or math.isinf(f):
        raise ValueError(f"'{field_name}' must be a finite number.")
    if min_val is not None and f < min_val:
        raise ValueError(f"'{field_name}' ({f}) is below plausible minimum of {min_val}.")
    if max_val is not None and f > max_val:
        raise ValueError(f"'{field_name}' ({f}) exceeds plausible maximum of {max_val}.")
    return f


@app.route("/")
def home():
    """Renders the main SolarChip responsive dashboard."""
    lat, lon, location_name = get_location_config()
    rated_w = get_panel_rated_watts()
    return render_template(
        "index.html",
        location_name=location_name,
        latitude=lat,
        longitude=lon,
        panel_rated_watts=rated_w
    )


@app.route("/api", methods=["GET"])
def api_index():
    """API catalog and documentation endpoint."""
    return jsonify({
        "name": "SolarChip REST API",
        "version": "1.0.0",
        "description": "Solar panel telemetry ingestion, historical analytics, and demo forecasting API.",
        "endpoints": {
            "GET /": "Web dashboard interface",
            "POST /api/readings": "Ingest live sensor telemetry from ESP32 (voltage, current, panel_temp)",
            "GET /api/readings": "Fetch historical sensor telemetry with ?limit=N",
            "GET /api/readings/latest": "Fetch the single most recent sensor telemetry reading",
            "GET /api/dashboard": "Aggregated summary metrics, freshness status, and recent readings",
            "GET /api/forecast": "24-hour solar irradiance and power/energy generation forecast",
            "GET /api/seasonal": "12-month illustrative seasonal generation estimates",
            "GET /api/status": "System health, sensor staleness, and performance deviation"
        }
    })


@app.route("/api/readings", methods=["POST"])
def receive_reading():
    """
    Receives and validates incoming sensor telemetry from ESP32.
    Expected JSON payload:
      {
        "voltage": 5.2,        # Volts, required, 0 <= V <= 60
        "current": 0.35,       # Amperes, required, -2 <= I <= 15
        "panel_temp": 38.5,    # °C, optional, -40 <= T <= 125
        "ambient_temp": 28.0,  # °C, optional, -40 <= T <= 80
        "humidity": 65.0,      # %, optional, 0 <= H <= 100
        "timestamp": "..."     # ISO 8601 string, optional (defaults to server UTC)
      }
    """
    if not request.is_json:
        return jsonify({
            "error": "Invalid Content-Type. Request body must be application/json."
        }), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON payload must be an object."}), 400

    # Required fields
    if "voltage" not in data or "current" not in data:
        return jsonify({"error": "Missing required fields: 'voltage' and 'current' are mandatory."}), 400

    try:
        # Physical sanity ranges for low-voltage educational solar panel kit:
        # Voltage: 0V to 60V (standard low-voltage SELV threshold is 60V DC)
        voltage = parse_finite_float(data["voltage"], "voltage", min_val=0.0, max_val=60.0)
        # Current: 0A to 15A (INA219 educational board typically handles up to 3.2A with 0.1 ohm shunt)
        current = parse_finite_float(data["current"], "current", min_val=-2.0, max_val=15.0)

        # Optional fields
        panel_temp = None
        if data.get("panel_temp") is not None:
            panel_temp = parse_finite_float(data["panel_temp"], "panel_temp", min_val=-40.0, max_val=125.0)

        ambient_temp = None
        if data.get("ambient_temp") is not None:
            ambient_temp = parse_finite_float(data["ambient_temp"], "ambient_temp", min_val=-40.0, max_val=80.0)

        humidity = None
        if data.get("humidity") is not None:
            humidity = parse_finite_float(data["humidity"], "humidity", min_val=0.0, max_val=100.0)

        # Validate timestamp if provided
        if data.get("timestamp"):
            raw_ts = str(data["timestamp"]).strip()
            try:
                # Test ISO format parsing
                dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                timestamp = dt.isoformat()
            except ValueError:
                return jsonify({
                    "error": f"Invalid timestamp format '{raw_ts}'. Must be standard ISO-8601 string."
                }), 400
        else:
            timestamp = datetime.now(timezone.utc).isoformat()

        # Calculate instantaneous power: P = V * max(0, I)
        power = voltage * max(0.0, current)

        reading_record = {
            "timestamp": timestamp,
            "voltage": round(voltage, 4),
            "current": round(current, 4),
            "power": round(power, 4),
            "panel_temp": round(panel_temp, 2) if panel_temp is not None else None,
            "ambient_temp": round(ambient_temp, 2) if ambient_temp is not None else None,
            "humidity": round(humidity, 2) if humidity is not None else None
        }

        row_id = save_reading(reading_record)

        return jsonify({
            "message": "Reading recorded successfully",
            "id": row_id,
            "timestamp": timestamp,
            "voltage_v": reading_record["voltage"],
            "current_a": reading_record["current"],
            "power_w": reading_record["power"],
            "panel_temp_c": reading_record["panel_temp"]
        }), 201

    except ValueError as val_err:
        return jsonify({"error": str(val_err)}), 400
    except Exception as exc:
        app.logger.error(f"Error saving reading: {exc}")
        return jsonify({"error": "Failed to persist reading to database."}), 500


@app.route("/api/readings", methods=["GET"])
def get_readings_history():
    """
    Returns historical readings ordered chronologically.
    Query parameters:
      - limit: int, default 100, max 1000
    """
    try:
        limit_param = request.args.get("limit", "100")
        limit = int(limit_param)
        limit = max(1, min(limit, 1000))
    except ValueError:
        return jsonify({"error": "Query parameter 'limit' must be an integer between 1 and 1000."}), 400

    readings = latest_readings(limit)
    return jsonify({
        "count": len(readings),
        "limit": limit,
        "readings": readings
    })


@app.route("/api/readings/latest", methods=["GET"])
def get_latest_reading_endpoint():
    """Returns the single latest reading received from the ESP32."""
    latest = get_latest_reading()
    if not latest:
        return jsonify({
            "latest": None,
            "message": "No sensor readings available yet. Connect ESP32 or send a sample POST to /api/readings."
        })

    # Calculate age in seconds
    age_seconds = None
    try:
        ts_str = latest["timestamp"].replace("Z", "+00:00")
        reading_dt = datetime.fromisoformat(ts_str)
        if reading_dt.tzinfo is None:
            reading_dt = reading_dt.replace(tzinfo=timezone.utc)
        age_seconds = max(0.0, (datetime.now(timezone.utc) - reading_dt).total_seconds())
    except Exception:
        pass

    return jsonify({
        "latest": latest,
        "freshness": {
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "is_fresh": age_seconds is not None and age_seconds <= 60.0
        }
    })


@app.route("/api/dashboard", methods=["GET"])
def get_dashboard_summary():
    """
    Consolidated payload for dashboard initial render and polling.
    Includes latest reading, recent historical points, daily accumulated energy,
    sensor freshness status, and hardware configuration.
    """
    readings = latest_readings(limit=100)
    latest = readings[-1] if readings else None

    # Calculate age and freshness
    sensor_connected = False
    age_seconds = None
    if latest:
        try:
            ts_str = latest["timestamp"].replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
            # Considered live if reading arrived within the last 60 seconds
            sensor_connected = age_seconds <= 60.0
        except Exception:
            pass

    today_energy = calculate_today_energy()
    lat, lon, location_name = get_location_config()
    rated_w = get_panel_rated_watts()

    return jsonify({
        "status": "online",
        "sensor": {
            "connected": sensor_connected,
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "latest": latest
        },
        "today_energy": today_energy,
        "recent_readings": readings,
        "config": {
            "location_name": location_name,
            "latitude": lat,
            "longitude": lon,
            "panel_rated_watts": rated_w
        }
    })


@app.route("/api/forecast", methods=["GET"])
def get_forecast():
    """
    Returns 24-hour solar generation forecast.
    Retrieves weather forecast from Open-Meteo, passes features to the demo
    hourly power model, and calculates hourly energy (Wh) and daily total (kWh).
    
    If the weather API is unreachable, returns a graceful fallback response
    so the dashboard remains fully operational.
    """
    lat, lon, location_name = get_location_config()
    rated_w = get_panel_rated_watts()

    # Check for live panel temperature from latest reading to calibrate hour 0
    latest = get_latest_reading()
    latest_panel_temp = latest.get("panel_temp") if latest else None

    try:
        force_refresh = request.args.get("refresh", "false").lower() == "true"
        weather_frame, timezone_name = get_hourly_forecast(force_refresh=force_refresh)
        forecast_frame = predict_24h(weather_frame, latest_measured_panel_temp=latest_panel_temp)

        hourly = []
        for row in forecast_frame.itertuples():
            hourly.append({
                "time": row.time.isoformat(),
                "hour": int(row.hour),
                "radiation_w_m2": round(float(row.radiation), 1),
                "ambient_temp_c": round(float(row.ambient_temp), 1),
                "cloud_cover_pct": round(float(row.cloud_cover), 1),
                "estimated_panel_temp_c": round(float(row.panel_temp), 1),
                "predicted_power_w": round(float(row.predicted_power_w), 2),
                "predicted_energy_wh": round(float(row.predicted_energy_wh), 2)
            })

        total_wh = round(sum(h["predicted_energy_wh"] for h in hourly), 2)
        total_kwh = round(total_wh / 1000.0, 4)

        return jsonify({
            "status": "success",
            "weather_available": True,
            "timezone": timezone_name,
            "location_name": location_name,
            "latitude": lat,
            "longitude": lon,
            "panel_rated_watts": rated_w,
            "total_forecast_energy_wh": total_wh,
            "total_forecast_energy_kwh": total_kwh,
            "hourly": hourly,
            "is_demo_estimate": True,
            "model_disclaimer": DEMO_DISCLAIMER,
            "inputs_used": [
                "shortwave_radiation (W/m²)",
                "temperature_2m (°C)",
                "cloud_cover (%)",
                "estimated_panel_temp (°C, NOCT thermal approximation)",
                "month of year",
                "hour of day"
            ]
        })

    except WeatherServiceError as w_err:
        app.logger.warning(f"Weather forecast unavailable: {w_err}")
        return jsonify({
            "status": "weather_unavailable",
            "weather_available": False,
            "message": str(w_err),
            "location_name": location_name,
            "total_forecast_energy_wh": 0.0,
            "total_forecast_energy_kwh": 0.0,
            "hourly": [],
            "is_demo_estimate": True,
            "model_disclaimer": DEMO_DISCLAIMER
        }), 200
    except Exception as exc:
        app.logger.error(f"Unexpected forecast failure: {exc}")
        return jsonify({
            "status": "error",
            "weather_available": False,
            "error": "Failed to calculate forecast model.",
            "details": str(exc),
            "hourly": [],
            "total_forecast_energy_wh": 0.0,
            "total_forecast_energy_kwh": 0.0
        }), 500


@app.route("/api/seasonal", methods=["GET"])
def get_seasonal_estimates():
    """
    Returns 12-month illustrative seasonal estimates.
    Clearly labeled as an illustrative model based on 12 sample climate reference points.
    """
    rated_w = get_panel_rated_watts()
    # 12 representative monthly climate regimes for tropical/subtropical reference
    months_reference = [
        {"month": 1, "name": "Jan", "radiation": 420, "temp": 25, "cloud": 25},
        {"month": 2, "name": "Feb", "radiation": 470, "temp": 27, "cloud": 22},
        {"month": 3, "name": "Mar", "radiation": 520, "temp": 30, "cloud": 20},
        {"month": 4, "name": "Apr", "radiation": 560, "temp": 33, "cloud": 25},
        {"month": 5, "name": "May", "radiation": 510, "temp": 34, "cloud": 35},
        {"month": 6, "name": "Jun", "radiation": 360, "temp": 31, "cloud": 65},
        {"month": 7, "name": "Jul", "radiation": 300, "temp": 29, "cloud": 75},
        {"month": 8, "name": "Aug", "radiation": 320, "temp": 29, "cloud": 70},
        {"month": 9, "name": "Sep", "radiation": 380, "temp": 29, "cloud": 55},
        {"month": 10, "name": "Oct", "radiation": 400, "temp": 28, "cloud": 45},
        {"month": 11, "name": "Nov", "radiation": 390, "temp": 26, "cloud": 35},
        {"month": 12, "name": "Dec", "radiation": 410, "temp": 25, "cloud": 28}
    ]

    estimates = []
    for m in months_reference:
        daily_wh = predict_seasonal(
            month=m["month"],
            avg_radiation=m["radiation"],
            avg_temp=m["temp"],
            avg_cloud=m["cloud"]
        )
        estimates.append({
            "month_num": m["month"],
            "month": m["name"],
            "avg_radiation_w_m2": m["radiation"],
            "avg_temp_c": m["temp"],
            "avg_cloud_pct": m["cloud"],
            "estimated_daily_wh": round(daily_wh, 2),
            "estimated_daily_kwh": round(daily_wh / 1000.0, 4)
        })

    return jsonify({
        "status": "success",
        "panel_rated_watts": rated_w,
        "is_demo_estimate": True,
        "model_type": "Illustrative 12-month climate prototype",
        "disclaimer": (
            "DEMO ONLY: Seasonal values are illustrative daily energy estimates trained on 12 sample "
            "reference points. They represent seasonal trends (e.g. monsoon cloud attenuation) but "
            "must not be interpreted as validated site-specific seasonal forecasts without historical empirical data."
        ),
        "months": estimates
    })


@app.route("/api/status", methods=["GET"])
def get_system_status():
    """
    Evaluates system operational status, telemetry freshness,
    and performance deviation vs configured rating.
    """
    rated_w = get_panel_rated_watts()
    latest = get_latest_reading()

    if not latest:
        return jsonify({
            "status": "waiting_for_sensor_data",
            "operational_state": "idle",
            "actual_power_w": 0.0,
            "reference_power_w": rated_w,
            "deviation_percent": None,
            "message": "No ESP32 readings received yet. Awaiting initial hardware connection."
        })

    # Compute sensor freshness
    age_seconds = None
    try:
        ts_str = latest["timestamp"].replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_seconds = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        pass

    is_stale = age_seconds is not None and age_seconds > 60.0
    actual_power = float(latest["power"])

    # Calculate deviation from reference
    # Note: during night, power is naturally 0.
    deviation = deviation_percent(rated_w, actual_power)

    if is_stale:
        status_code = "sensor_stale"
        message = f"Sensor data is stale (last reading received {int(age_seconds)}s ago). Check ESP32 power or Wi-Fi."
    elif actual_power == 0.0:
        status_code = "zero_generation"
        message = "Panel generating 0 W (typical at night, indoors, or under complete shading)."
    elif deviation >= 30.0:
        status_code = "possible_deviation"
        message = f"Actual power ({actual_power:.1f} W) is {deviation:.1f}% below peak rating ({rated_w:.1f} W). Verify shading, soiling, or tilt."
    else:
        status_code = "nominal"
        message = f"Operating normally ({actual_power:.1f} W generated)."

    return jsonify({
        "status": status_code,
        "is_stale": is_stale,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "actual_power_w": round(actual_power, 2),
        "reference_power_w": rated_w,
        "deviation_percent": round(deviation, 1) if deviation is not None else 0.0,
        "message": message
    })


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(f"Starting SolarChip Server on http://{host}:{port} (debug={debug})")
    app.run(host=host, port=port, debug=debug)
