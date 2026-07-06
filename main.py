"""Water tank monitoring dashboard backend.

Subscribes to MQTT topics for tank distance readings and pump status,
persists them to SQLite, and streams live updates to connected browsers
over a WebSocket. Serves dashboard.html as the root page.
"""

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import paho.mqtt.client as mqtt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("water-dashboard")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("TANK_DB_PATH", str(BASE_DIR / "tank.db")))
DASHBOARD_HTML = BASE_DIR / "dashboard.html"

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
TOPIC_DISTANCE = "water/distance"
TOPIC_PUMP = "water/pump"
TOPIC_PUMP_ALERT = "water/pump_alert"
TOPIC_PUMP_TELEMETRY = "water/pump_telemetry"

EMPTY_DISTANCE_CM = 90.0
FULL_DISTANCE_CM = 15.0
TANK_CAPACITY_LITERS = 500.0

RETENTION_DAYS = 30
RETENTION_CHECK_INTERVAL_SECONDS = 6 * 60 * 60


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_cutoff_iso(**delta_kwargs) -> str:
    """ISO cutoff string in the same format as stored timestamps.

    Stored timestamps use isoformat() with a 'T' separator, so windowing must
    compare against the same format — SQLite's datetime('now', ...) yields a
    space separator, which breaks lexicographic comparison for same-day values.
    """
    return (datetime.now(timezone.utc) - timedelta(**delta_kwargs)).isoformat()


def distance_to_reading(distance_cm: float) -> dict:
    water_height_cm = EMPTY_DISTANCE_CM - distance_cm
    full_span_cm = EMPTY_DISTANCE_CM - FULL_DISTANCE_CM
    percent = (water_height_cm / full_span_cm) * 100
    percent = max(0.0, min(100.0, percent))
    volume_liters = (percent / 100) * TANK_CAPACITY_LITERS
    return {"percent": round(percent, 1), "volume_liters": round(volume_liters, 1)}


class TankState:
    """Latest known readings, shared between the MQTT thread and the API."""

    def __init__(self):
        self.distance: float | None = None
        self.percent: float | None = None
        self.volume_liters: float | None = None
        self.pump_status: str = "unknown"
        self.timestamp: str | None = None
        self.lock = threading.Lock()

    def update_distance(self, distance: float, timestamp: str):
        derived = distance_to_reading(distance)
        with self.lock:
            self.distance = distance
            self.percent = derived["percent"]
            self.volume_liters = derived["volume_liters"]
            self.timestamp = timestamp

    def update_pump(self, status: str):
        with self.lock:
            self.pump_status = status

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "distance": self.distance,
                "percent": self.percent,
                "volume_liters": self.volume_liters,
                "pump_status": self.pump_status,
                "timestamp": self.timestamp,
            }


state = TankState()


class PumpTelemetryState:
    """Latest known Tapo smart plug telemetry (power, voltage, energy)."""

    def __init__(self):
        self.power_w: float | None = None
        self.voltage_v: float | None = None
        self.current_a: float | None = None
        self.energy_today_kwh: float | None = None
        self.energy_month_kwh: float | None = None
        self.overloaded: bool | None = None
        self.timestamp: str | None = None

    def update(self, data: dict):
        self.power_w = data.get("power_w")
        self.voltage_v = data.get("voltage_v")
        self.current_a = data.get("current_a")
        self.energy_today_kwh = data.get("energy_today_kwh")
        self.energy_month_kwh = data.get("energy_month_kwh")
        self.overloaded = data.get("overloaded")
        self.timestamp = data.get("timestamp")

    def snapshot(self) -> dict:
        return {
            "power_w": self.power_w,
            "voltage_v": self.voltage_v,
            "current_a": self.current_a,
            "energy_today_kwh": self.energy_today_kwh,
            "energy_month_kwh": self.energy_month_kwh,
            "overloaded": self.overloaded,
            "timestamp": self.timestamp,
        }


pump_telemetry = PumpTelemetryState()


class ConnectionManager:
    """Tracks connected WebSocket clients and broadcasts updates to all of them."""

    def __init__(self):
        self.active: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.active.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            self.active.discard(ws)

    async def broadcast(self, message: dict):
        payload = json.dumps(message)
        async with self.lock:
            targets = list(self.active)
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                await self.disconnect(ws)


manager = ConnectionManager()

# Event loop reference so the MQTT thread (which runs outside asyncio) can
# safely schedule coroutines back onto the main loop.
main_loop: asyncio.AbstractEventLoop | None = None


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                distance REAL NOT NULL,
                percent REAL NOT NULL,
                volume_liters REAL NOT NULL,
                timestamp TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                value TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp)"
        )
        await db.commit()


async def store_reading(distance: float, percent: float, volume_liters: float, timestamp: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO readings (distance, percent, volume_liters, timestamp) VALUES (?, ?, ?, ?)",
            (distance, percent, volume_liters, timestamp),
        )
        await db.commit()


async def store_audit_log(event: str, value: str, timestamp: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (event, value, timestamp) VALUES (?, ?, ?)",
            (event, value, timestamp),
        )
        await db.commit()


async def prune_old_data():
    cutoff = utc_cutoff_iso(days=RETENTION_DAYS)
    async with aiosqlite.connect(DB_PATH) as db:
        readings_cursor = await db.execute(
            "DELETE FROM readings WHERE timestamp < ?", (cutoff,)
        )
        logs_cursor = await db.execute(
            "DELETE FROM audit_log WHERE timestamp < ?", (cutoff,)
        )
        await db.commit()
    logger.info(
        "Pruned %d reading(s) and %d log(s) older than %d days",
        readings_cursor.rowcount, logs_cursor.rowcount, RETENTION_DAYS,
    )


async def retention_loop():
    while True:
        try:
            await prune_old_data()
        except Exception:
            logger.exception("Retention prune failed")
        await asyncio.sleep(RETENTION_CHECK_INTERVAL_SECONDS)


async def handle_distance_message(distance: float):
    timestamp = utc_now_iso()
    state.update_distance(distance, timestamp)
    snapshot = state.snapshot()
    await store_reading(distance, snapshot["percent"], snapshot["volume_liters"], timestamp)
    value = f"{distance:.2f} cm"
    await store_audit_log("reading", value, timestamp)
    await manager.broadcast({**snapshot, "event": "reading", "value": value})


async def handle_pump_message(status: str):
    timestamp = utc_now_iso()
    state.update_pump(status)
    await store_audit_log("pump", status, timestamp)
    await manager.broadcast({**state.snapshot(), "event": "pump", "value": status})


async def handle_pump_alert_message(alert: str):
    timestamp = utc_now_iso()
    await store_audit_log("alert", alert, timestamp)
    await manager.broadcast({**state.snapshot(), "event": "alert", "value": alert, "timestamp": timestamp})


async def handle_pump_telemetry_message(payload: str):
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("Ignoring malformed pump telemetry payload: %r", payload)
        return
    pump_telemetry.update(data)
    await manager.broadcast({"event": "telemetry", **pump_telemetry.snapshot()})


def on_connect(client: mqtt.Client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("Connected to MQTT broker at %s:%s", MQTT_HOST, MQTT_PORT)
        client.subscribe(TOPIC_DISTANCE)
        client.subscribe(TOPIC_PUMP)
        client.subscribe(TOPIC_PUMP_ALERT)
        client.subscribe(TOPIC_PUMP_TELEMETRY)
    else:
        logger.warning("MQTT connect failed with code %s", rc)


def on_disconnect(client: mqtt.Client, userdata, rc, properties=None):
    logger.warning("Disconnected from MQTT broker (rc=%s), will auto-reconnect", rc)


def on_message(client: mqtt.Client, userdata, msg: mqtt.MQTTMessage):
    if main_loop is None:
        return
    payload = msg.payload.decode(errors="ignore").strip()
    try:
        if msg.topic == TOPIC_DISTANCE:
            distance = float(payload)
            asyncio.run_coroutine_threadsafe(handle_distance_message(distance), main_loop)
        elif msg.topic == TOPIC_PUMP:
            asyncio.run_coroutine_threadsafe(handle_pump_message(payload.lower()), main_loop)
        elif msg.topic == TOPIC_PUMP_ALERT:
            asyncio.run_coroutine_threadsafe(handle_pump_alert_message(payload), main_loop)
        elif msg.topic == TOPIC_PUMP_TELEMETRY:
            asyncio.run_coroutine_threadsafe(handle_pump_telemetry_message(payload), main_loop)
    except ValueError:
        logger.warning("Ignoring unparseable payload on %s: %r", msg.topic, payload)


def start_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    await init_db()
    mqtt_client = start_mqtt_client()
    retention_task = asyncio.create_task(retention_loop())
    logger.info("Water dashboard started")
    yield
    retention_task.cancel()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()


app = FastAPI(title="Water Tank Dashboard", lifespan=lifespan)


@app.get("/")
async def root():
    return FileResponse(DASHBOARD_HTML, headers={"Cache-Control": "no-cache"})


@app.get("/api/latest")
async def api_latest():
    return JSONResponse(state.snapshot())


@app.get("/api/pump-telemetry")
async def api_pump_telemetry():
    return JSONResponse(pump_telemetry.snapshot())


@app.get("/api/history")
async def api_history(hours: int = 24):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT distance, percent, volume_liters, timestamp
            FROM readings
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (utc_cutoff_iso(hours=hours),),
        )
        rows = await cursor.fetchall()
    return JSONResponse([dict(row) for row in rows])


@app.get("/api/stats")
async def api_stats(hours: int = 24):
    window = utc_cutoff_iso(hours=hours)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT MIN(percent) AS min_percent, MAX(percent) AS max_percent,
                   AVG(percent) AS avg_percent, COUNT(*) AS readings_count
            FROM readings
            WHERE timestamp >= ?
            """,
            (window,),
        )
        agg = await cursor.fetchone()

        prior_cursor = await db.execute(
            """
            SELECT value, timestamp FROM audit_log
            WHERE event = 'pump' AND timestamp < ?
            ORDER BY timestamp DESC LIMIT 1
            """,
            (window,),
        )
        prior = await prior_cursor.fetchone()

        events_cursor = await db.execute(
            """
            SELECT value, timestamp FROM audit_log
            WHERE event = 'pump' AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (window,),
        )
        events = await events_cursor.fetchall()

    now_dt = datetime.now(timezone.utc)
    window_start_dt = now_dt - timedelta(hours=hours)

    timeline = []
    if prior:
        timeline.append((window_start_dt, prior["value"].lower()))
    for row in events:
        timeline.append((datetime.fromisoformat(row["timestamp"]), row["value"].lower()))

    pump_on_seconds = 0.0
    pump_cycles = 0
    for i, (ts, status) in enumerate(timeline):
        segment_end = timeline[i + 1][0] if i + 1 < len(timeline) else now_dt
        if status == "on":
            pump_on_seconds += (segment_end - ts).total_seconds()
            pump_cycles += 1

    return JSONResponse({
        "min_percent": agg["min_percent"],
        "max_percent": agg["max_percent"],
        "avg_percent": round(agg["avg_percent"], 1) if agg["avg_percent"] is not None else None,
        "readings_count": agg["readings_count"],
        "pump_on_seconds": round(pump_on_seconds),
        "pump_cycles": pump_cycles,
    })


@app.get("/api/pump-history")
async def api_pump_history(hours: int = 24):
    window = utc_cutoff_iso(hours=hours)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        prior_cursor = await db.execute(
            """
            SELECT value, timestamp FROM audit_log
            WHERE event = 'pump' AND timestamp < ?
            ORDER BY timestamp DESC LIMIT 1
            """,
            (window,),
        )
        prior = await prior_cursor.fetchone()

        events_cursor = await db.execute(
            """
            SELECT value, timestamp FROM audit_log
            WHERE event = 'pump' AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (window,),
        )
        events = await events_cursor.fetchall()

    now_iso = utc_now_iso()
    window_start_iso = window

    points = []
    if prior:
        points.append({"timestamp": window_start_iso, "status": prior["value"].lower()})
    elif not events and state.pump_status in ("on", "off"):
        points.append({"timestamp": window_start_iso, "status": state.pump_status})
    for row in events:
        status = row["value"].lower()
        # Skip same-value republishes (e.g. retained-state re-announces on HA
        # restart) so the chart only gets genuine transitions.
        if points and points[-1]["status"] == status:
            continue
        points.append({"timestamp": row["timestamp"], "status": status})
    if points:
        points.append({"timestamp": now_iso, "status": points[-1]["status"]})

    return JSONResponse(points)


@app.get("/api/last-fill-session")
async def api_last_fill_session():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Pump audit entries can include duplicate same-value republishes (e.g. on
        # Home Assistant restart), so scan for the most recent adjacent on->off
        # transition rather than just the latest 'off' row.
        events_cursor = await db.execute(
            "SELECT value, timestamp FROM audit_log WHERE event = 'pump' ORDER BY timestamp DESC LIMIT 50"
        )
        events = await events_cursor.fetchall()

        on_row = None
        off_row = None
        for i in range(len(events) - 1):
            if events[i]["value"].lower() == "off" and events[i + 1]["value"].lower() == "on":
                off_row = events[i]
                on_row = events[i + 1]
                break

        if not on_row or not off_row:
            return JSONResponse({"available": False})

        start_cursor = await db.execute(
            "SELECT distance, percent, volume_liters, timestamp FROM readings WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
            (on_row["timestamp"],),
        )
        start_reading = await start_cursor.fetchone()

        end_cursor = await db.execute(
            "SELECT distance, percent, volume_liters, timestamp FROM readings WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
            (off_row["timestamp"],),
        )
        end_reading = await end_cursor.fetchone()

    if not start_reading or not end_reading:
        return JSONResponse({"available": False})

    duration_seconds = (
        datetime.fromisoformat(end_reading["timestamp"]) - datetime.fromisoformat(start_reading["timestamp"])
    ).total_seconds()
    if duration_seconds <= 0:
        return JSONResponse({"available": False})

    duration_minutes = duration_seconds / 60
    volume_added = end_reading["volume_liters"] - start_reading["volume_liters"]
    distance_dropped = start_reading["distance"] - end_reading["distance"]

    return JSONResponse({
        "available": True,
        "start_timestamp": start_reading["timestamp"],
        "end_timestamp": end_reading["timestamp"],
        "duration_minutes": round(duration_minutes, 1),
        "start_percent": start_reading["percent"],
        "end_percent": end_reading["percent"],
        "volume_added_liters": round(volume_added, 1),
        "rate_l_per_min": round(volume_added / duration_minutes, 1),
        "rate_cm_per_min": round(distance_dropped / duration_minutes, 2),
    })


@app.get("/api/logs")
async def api_logs(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT event, value, timestamp FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    return JSONResponse([dict(row) for row in rows])


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_text(json.dumps({**state.snapshot(), "event": "snapshot", "value": None}))
        while True:
            # Keep the connection alive; clients don't send anything meaningful.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=APP_PORT)
