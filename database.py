from contextlib import contextmanager
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "solarchip.db"


def get_db_path(custom_path: Optional[Path] = None) -> Path:
    if custom_path:
        return Path(custom_path)
    env_path = os.getenv("SOLARCHIP_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def create_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = get_db_path(db_path)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


@contextmanager
def get_connection(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    """Yields an SQLite connection that automatically commits and closes."""
    conn = create_connection(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None) -> None:
    """Initialize database tables and indexes safely."""
    path = get_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                voltage REAL NOT NULL,
                current REAL NOT NULL,
                power REAL NOT NULL,
                panel_temp REAL,
                ambient_temp REAL,
                humidity REAL
            );
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_readings_timestamp
            ON readings(timestamp);
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                forecast_type TEXT NOT NULL,
                target_time TEXT,
                predicted_wh REAL,
                predicted_power_w REAL,
                note TEXT
            );
        """)


def save_reading(data: Dict[str, Any], db_path: Optional[Path] = None) -> int:
    """
    Saves a sensor reading to the database.
    Expects timestamp in ISO format, and numeric voltage, current, power.
    """
    ts = data.get("timestamp") or datetime.now(timezone.utc).isoformat()
    with get_connection(db_path) as conn:
        cursor = conn.execute("""
            INSERT INTO readings
            (timestamp, voltage, current, power, panel_temp, ambient_temp, humidity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            ts,
            float(data["voltage"]),
            float(data["current"]),
            float(data["power"]),
            float(data["panel_temp"]) if data.get("panel_temp") is not None else None,
            float(data["ambient_temp"]) if data.get("ambient_temp") is not None else None,
            float(data["humidity"]) if data.get("humidity") is not None else None
        ))
        return cursor.lastrowid


def latest_readings(limit: int = 100, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Returns the latest N readings ordered chronologically (oldest to newest).
    """
    limit = max(1, min(limit, 5000))
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, timestamp, voltage, current, power, panel_temp, ambient_temp, humidity
            FROM readings
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_latest_reading(db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Returns the most recent reading or None if table is empty."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, timestamp, voltage, current, power, panel_temp, ambient_temp, humidity
            FROM readings
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


def calculate_today_energy(
    db_path: Optional[Path] = None,
    target_date_prefix: Optional[str] = None,
    max_gap_seconds: float = 300.0
) -> Dict[str, Any]:
    """
    Calculates accumulated energy generated today using trapezoidal integration.
    Only integrates across intervals <= max_gap_seconds (default 5 minutes)
    to avoid spurious accumulation during power outages or downtime.
    Returns:
        {
            "energy_wh": float,
            "energy_kwh": float,
            "reading_count": int,
            "date": str
        }
    """
    if target_date_prefix is None:
        target_date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT timestamp, power
            FROM readings
            WHERE timestamp LIKE ?
            ORDER BY timestamp ASC, id ASC
            """,
            (f"{target_date_prefix}%",)
        ).fetchall()

    if not rows:
        return {
            "energy_wh": 0.0,
            "energy_kwh": 0.0,
            "reading_count": 0,
            "date": target_date_prefix
        }

    total_wh = 0.0
    parsed_points = []
    for row in rows:
        try:
            ts_str = row["timestamp"].replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            p = max(0.0, float(row["power"]))
            parsed_points.append((dt.timestamp(), p))
        except (ValueError, TypeError):
            continue

    for i in range(1, len(parsed_points)):
        t_prev, p_prev = parsed_points[i - 1]
        t_curr, p_curr = parsed_points[i]
        dt_seconds = t_curr - t_prev
        if 0 < dt_seconds <= max_gap_seconds:
            avg_power = (p_prev + p_curr) / 2.0
            total_wh += avg_power * (dt_seconds / 3600.0)

    total_wh = round(total_wh, 3)
    return {
        "energy_wh": total_wh,
        "energy_kwh": round(total_wh / 1000.0, 4),
        "reading_count": len(rows),
        "date": target_date_prefix
    }


def save_prediction(
    created_at: str,
    forecast_type: str,
    target_time: Optional[str],
    predicted_wh: float,
    predicted_power_w: float,
    note: str = "",
    db_path: Optional[Path] = None
) -> int:
    with get_connection(db_path) as conn:
        cursor = conn.execute("""
            INSERT INTO predictions
            (created_at, forecast_type, target_time, predicted_wh, predicted_power_w, note)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (created_at, forecast_type, target_time, predicted_wh, predicted_power_w, note))
        return cursor.lastrowid
