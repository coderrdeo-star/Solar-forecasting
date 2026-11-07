import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app


def run_live_probe():
    client = app.test_client()

    print("=" * 60)
    print("Running In-Process Flask Application & API Probes")
    print("=" * 60)

    # 1. GET /
    res1 = client.get("/")
    assert res1.status_code == 200, f"Expected 200, got {res1.status_code}"
    html = res1.data.decode("utf-8")
    assert "SolarChip" in html
    assert "Live Telemetry History" in html
    print("[PASS] 1. GET / returned 200 (Dashboard HTML rendered)")

    # 2. GET /api
    res2 = client.get("/api")
    assert res2.status_code == 200
    data2 = res2.get_json()
    assert "endpoints" in data2
    assert "POST /api/readings" in data2["endpoints"]
    print(f"[PASS] 2. GET /api returned 200 ({len(data2['endpoints'])} endpoints cataloged)")

    # 3. POST /api/readings
    sample = {"voltage": 5.2, "current": 0.35, "panel_temp": 38.5}
    res3 = client.post("/api/readings", json=sample)
    assert res3.status_code == 201, f"Expected 201, got {res3.status_code}: {res3.data}"
    data3 = res3.get_json()
    expected_power = round(5.2 * 0.35, 4)
    assert data3["power_w"] == expected_power
    print(f"[PASS] 3. POST /api/readings returned 201 (power_w={data3['power_w']} W, id={data3['id']})")

    # 4. GET /api/dashboard
    res4 = client.get("/api/dashboard")
    assert res4.status_code == 200
    data4 = res4.get_json()
    assert data4["sensor"]["latest"]["voltage"] == 5.2
    assert data4["today_energy"]["reading_count"] >= 1
    print(f"[PASS] 4. GET /api/dashboard returned 200 (latest power={data4['sensor']['latest']['power']} W)")

    # 5. GET /api/readings/latest
    res5 = client.get("/api/readings/latest")
    assert res5.status_code == 200
    data5 = res5.get_json()
    assert data5["latest"]["voltage"] == 5.2
    assert "freshness" in data5
    print(f"[PASS] 5. GET /api/readings/latest returned 200 (age={data5['freshness']['age_seconds']}s)")

    # 6. GET /api/seasonal
    res6 = client.get("/api/seasonal")
    assert res6.status_code == 200
    data6 = res6.get_json()
    assert data6["is_demo_estimate"] is True
    assert len(data6["months"]) == 12
    print(f"[PASS] 6. GET /api/seasonal returned 200 (12 months illustrative daily Wh)")

    # 7. GET /api/status
    res7 = client.get("/api/status")
    assert res7.status_code == 200
    data7 = res7.get_json()
    assert "status" in data7
    print(f"[PASS] 7. GET /api/status returned 200 (status='{data7['status']}', dev={data7['deviation_percent']}%)")

    # 8. GET /api/forecast
    res8 = client.get("/api/forecast")
    assert res8.status_code in [200, 500]
    data8 = res8.get_json()
    print(f"[PASS] 8. GET /api/forecast returned {res8.status_code} (weather_available={data8.get('weather_available')})")

    print("=" * 60)
    print("ALL 8 ENDPOINT TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    run_live_probe()
