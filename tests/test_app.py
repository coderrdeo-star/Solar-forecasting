import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

# Set testing database path before importing app/database
test_db_dir = tempfile.mkdtemp()
test_db_path = Path(test_db_dir) / "test_solarchip.db"
os.environ["SOLARCHIP_DB_PATH"] = str(test_db_path)
os.environ["PANEL_RATED_WATTS"] = "10.0"
os.environ["LATITUDE"] = "13.0827"
os.environ["LONGITUDE"] = "80.2707"

import app
import database
import ml_models
import weather


class SolarChipDatabaseTestCase(unittest.TestCase):
    def setUp(self):
        database.init_db(test_db_path)
        with database.get_connection(test_db_path) as conn:
            conn.execute("DELETE FROM readings;")
            conn.execute("DELETE FROM predictions;")

    def test_save_and_retrieve_reading(self):
        record = {
            "timestamp": "2026-11-07T10:00:00Z",
            "voltage": 5.2,
            "current": 0.40,
            "power": 2.08,
            "panel_temp": 35.5,
            "ambient_temp": 28.0,
            "humidity": 60.0
        }
        row_id = database.save_reading(record, db_path=test_db_path)
        self.assertGreater(row_id, 0)

        latest = database.get_latest_reading(db_path=test_db_path)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["voltage"], 5.2)
        self.assertEqual(latest["current"], 0.40)
        self.assertEqual(latest["power"], 2.08)
        self.assertEqual(latest["panel_temp"], 35.5)

    def test_calculate_today_energy_trapezoidal(self):
        # Insert two readings 60 seconds apart: 2.0W and 4.0W
        # Avg power = 3.0W, dt = 60s -> Wh = 3.0 * (60/3600) = 0.05 Wh
        database.save_reading({
            "timestamp": "2026-11-07T12:00:00Z",
            "voltage": 5.0, "current": 0.4, "power": 2.0
        }, db_path=test_db_path)
        database.save_reading({
            "timestamp": "2026-11-07T12:01:00Z",
            "voltage": 5.0, "current": 0.8, "power": 4.0
        }, db_path=test_db_path)

        energy = database.calculate_today_energy(
            db_path=test_db_path,
            target_date_prefix="2026-11-07"
        )
        self.assertEqual(energy["reading_count"], 2)
        self.assertAlmostEqual(energy["energy_wh"], 0.05, places=2)
        self.assertAlmostEqual(energy["energy_kwh"], 0.0001, places=4)


class SolarChipAPITestCase(unittest.TestCase):
    def setUp(self):
        database.init_db(test_db_path)
        with database.get_connection(test_db_path) as conn:
            conn.execute("DELETE FROM readings;")
        self.client = app.app.test_client()

    def test_home_page(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"SolarChip", res.data)
        self.assertIn(b"Live Telemetry History", res.data)

    def test_api_catalog(self):
        res = self.client.get("/api")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("endpoints", data)
        self.assertIn("POST /api/readings", data["endpoints"])

    def test_post_valid_reading(self):
        payload = {
            "voltage": 5.25,
            "current": 0.38,
            "panel_temp": 39.2
        }
        res = self.client.post("/api/readings", json=payload)
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertEqual(data["message"], "Reading recorded successfully")
        self.assertAlmostEqual(data["power_w"], 5.25 * 0.38, places=3)
        self.assertIn("timestamp", data)

    def test_post_reading_non_json(self):
        res = self.client.post("/api/readings", data="raw-text", content_type="text/plain")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Content-Type", res.get_json()["error"])

    def test_post_reading_missing_fields(self):
        res = self.client.post("/api/readings", json={"voltage": 5.0})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Missing required fields", res.get_json()["error"])

    def test_post_reading_non_numeric(self):
        res = self.client.post("/api/readings", json={"voltage": "five", "current": 0.4})
        self.assertEqual(res.status_code, 400)
        self.assertIn("numeric", res.get_json()["error"])

    def test_post_reading_nan_and_inf(self):
        # NaN
        res_nan = self.client.post("/api/readings", json={"voltage": float("nan"), "current": 0.4})
        self.assertEqual(res_nan.status_code, 400)
        self.assertIn("finite", res_nan.get_json()["error"])

        # Inf
        res_inf = self.client.post("/api/readings", json={"voltage": 5.0, "current": float("inf")})
        self.assertEqual(res_inf.status_code, 400)
        self.assertIn("finite", res_inf.get_json()["error"])

    def test_post_reading_out_of_bounds(self):
        # Negative voltage
        res = self.client.post("/api/readings", json={"voltage": -2.0, "current": 0.4})
        self.assertEqual(res.status_code, 400)

        # Voltage > 60V
        res2 = self.client.post("/api/readings", json={"voltage": 75.0, "current": 0.4})
        self.assertEqual(res2.status_code, 400)

        # Unreasonable panel temp (> 125 C)
        res3 = self.client.post("/api/readings", json={"voltage": 5.0, "current": 0.4, "panel_temp": 180.0})
        self.assertEqual(res3.status_code, 400)

    def test_post_reading_invalid_timestamp(self):
        res = self.client.post("/api/readings", json={
            "voltage": 5.0, "current": 0.4, "timestamp": "not-a-timestamp"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("timestamp", res.get_json()["error"])

    def test_readings_history_and_latest(self):
        # Initially empty
        res_empty = self.client.get("/api/readings/latest")
        self.assertEqual(res_empty.status_code, 200)
        self.assertIsNone(res_empty.get_json()["latest"])

        # Add 3 readings
        for i in range(3):
            self.client.post("/api/readings", json={"voltage": 5.0 + i, "current": 0.5})

        # Test latest
        res_latest = self.client.get("/api/readings/latest")
        self.assertEqual(res_latest.status_code, 200)
        latest_data = res_latest.get_json()
        self.assertEqual(latest_data["latest"]["voltage"], 7.0)
        self.assertIn("freshness", latest_data)

        # Test history with limit
        res_hist = self.client.get("/api/readings?limit=2")
        self.assertEqual(res_hist.status_code, 200)
        hist_data = res_hist.get_json()
        self.assertEqual(hist_data["count"], 2)

    def test_dashboard_endpoint(self):
        self.client.post("/api/readings", json={"voltage": 5.0, "current": 0.5})
        res = self.client.get("/api/dashboard")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "online")
        self.assertIn("sensor", data)
        self.assertIn("today_energy", data)
        self.assertIn("config", data)

    def test_seasonal_endpoint(self):
        res = self.client.get("/api/seasonal")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["is_demo_estimate"])
        self.assertEqual(len(data["months"]), 12)
        self.assertIn("disclaimer", data)
        for m in data["months"]:
            self.assertIn("estimated_daily_wh", m)
            self.assertIn("estimated_daily_kwh", m)

    def test_forecast_weather_offline_graceful_fallback(self):
        # Mock weather service failure
        with patch("app.get_hourly_forecast") as mock_weather:
            mock_weather.side_effect = weather.WeatherServiceError("Offline connection error")
            res = self.client.get("/api/forecast")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertFalse(data["weather_available"])
            self.assertEqual(data["status"], "weather_unavailable")
            self.assertEqual(data["total_forecast_energy_wh"], 0.0)

    def test_forecast_with_mocked_weather(self):
        times = pd.date_range("2026-11-07 00:00:00", periods=24, freq="h")
        mock_frame = pd.DataFrame({
            "time": times,
            "radiation": [0, 0, 0, 0, 0, 50, 200, 450, 700, 850, 800, 650, 400, 200, 50, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            "ambient_temp": [24.0] * 24,
            "cloud_cover": [10.0] * 24
        })
        with patch("app.get_hourly_forecast") as mock_weather:
            mock_weather.return_value = (mock_frame, "Asia/Kolkata")
            res = self.client.get("/api/forecast")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["weather_available"])
            self.assertTrue(data["is_demo_estimate"])
            self.assertEqual(len(data["hourly"]), 24)
            self.assertGreater(data["total_forecast_energy_wh"], 0.0)
            self.assertGreater(data["total_forecast_energy_kwh"], 0.0)

    def test_status_endpoint_empty_and_active(self):
        # Empty
        res1 = self.client.get("/api/status")
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.get_json()["status"], "waiting_for_sensor_data")

        # Active
        self.client.post("/api/readings", json={"voltage": 5.0, "current": 1.0})
        res2 = self.client.get("/api/status")
        self.assertEqual(res2.status_code, 200)
        self.assertIn("deviation_percent", res2.get_json())


class MLModelsTestCase(unittest.TestCase):
    def test_deviation_percent(self):
        # Zero expected
        self.assertEqual(ml_models.deviation_percent(0.0, 5.0), 0.0)
        # Power exceeds expected
        self.assertEqual(ml_models.deviation_percent(10.0, 12.0), 0.0)
        # 50% drop
        self.assertEqual(ml_models.deviation_percent(10.0, 5.0), 50.0)

    def test_predict_24h_thermal_logic(self):
        times = pd.date_range("2026-11-07 00:00:00", periods=24, freq="h")
        frame = pd.DataFrame({
            "time": times,
            "radiation": [400.0] * 24,
            "ambient_temp": [30.0] * 24,
            "cloud_cover": [20.0] * 24
        })
        # Test with live panel temperature calibration for hour 0
        pred_df = ml_models.predict_24h(frame, latest_measured_panel_temp=45.0)
        self.assertEqual(len(pred_df), 24)
        # Hour 0 panel temp should match the live measurement
        self.assertEqual(pred_df.iloc[0]["panel_temp"], 45.0)
        # Hour 1 should use thermal formula: ambient + radiation * 0.025 = 30 + 400 * 0.025 = 40.0
        self.assertEqual(pred_df.iloc[1]["panel_temp"], 40.0)
        # Power and Wh should be non-negative
        self.assertTrue((pred_df["predicted_power_w"] >= 0).all())
        self.assertTrue((pred_df["predicted_energy_wh"] >= 0).all())


if __name__ == "__main__":
    unittest.main()
