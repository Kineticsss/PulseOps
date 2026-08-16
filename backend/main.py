"""
PulseOps - backend/main.py (Phase 2)
======================================
Updated to write snapshots to TimescaleDB instead of in-memory store.
The API surface stays exactly the same - the agent does not need any changes.

CS 413 (Adv. Software Eng.) - This is the Open/Closed Principle:
  The API is open for extension (we added a database) but closed for
  modification (the agent and any existing clients see no difference).
"""

from dotenv import load_dotenv
load_dotenv()

from backend.anomaly import AnomalyDetector
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Any
from datetime import datetime, timezone
from backend.analyzer import RootCauseAnalyzer
from backend.anomaly import AnomalyDetector
from backend.database import init_db, get_db
from backend.models import Metric
from backend.alerting import AlertManager
from backend.anomaly import AnomalyDetector
from backend.auth import generate_api_key, verify_api_key, hash_key, bearer_scheme
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security

app = FastAPI(
    title="PulseOps API",
    description="Ingestion and query API for the PulseOps monitoring agent",
    version="0.7.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup():
    """
    Runs once when the API starts.
    Creates tables if they do not exist yet.
    Safe to run multiple times - uses IF NOT EXISTS internally.
    """
    init_db()


# ── Models ────────────────────────────────────────────────────────────────────
class SnapshotPayload(BaseModel):
    server_id:     str
    timestamp:     str
    cpu:           dict[str, Any]
    memory:        dict[str, Any]
    disk:          dict[str, Any]
    network:       dict[str, Any]
    top_processes: list[dict[str, Any]]


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "service": "PulseOps API",
        "status":  "running",
        "version": "0.7.0",
        "time":    datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/metrics", status_code=201)
def ingest_metrics(
    payload: SnapshotPayload,
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
):
    """Ingest metrics — requires valid API key in Authorization header."""
    # Verify key
    auth = verify_api_key(credentials, db)

    # Ensure server_id in payload matches the registered server
    if auth["server_id"] != payload.server_id:
        raise HTTPException(
            status_code=403,
            detail=f"API key is registered to '{auth['server_id']}', not '{payload.server_id}'"
        )

    metric = Metric(
        timestamp      = datetime.fromisoformat(payload.timestamp),
        server_id      = payload.server_id,
        cpu            = payload.cpu,
        memory         = payload.memory,
        disk           = payload.disk,
        network        = payload.network,
        processes      = payload.top_processes,
        cpu_percent    = payload.cpu.get("percent_total"),
        memory_percent = payload.memory.get("ram", {}).get("percent_used"),
    )
    db.add(metric)
    db.commit()

    try:
        detector = AnomalyDetector(db, payload.server_id)
        anomaly  = detector.analyze_latest()
        if anomaly and anomaly.get("is_anomaly"):
            alerter = AlertManager(db)
            alerter.trigger(payload.server_id, anomaly)
    except Exception as e:
        print(f"[Alert] Alert check error: {e}")

    return {"status": "accepted", "server_id": payload.server_id}


@app.get("/api/servers")
def list_servers(db: Session = Depends(get_db)):
    """Lists all servers with their latest CPU and memory readings."""
    rows = db.execute(text("""
        SELECT DISTINCT ON (server_id)
            server_id,
            timestamp,
            cpu_percent,
            memory_percent
        FROM metrics
        ORDER BY server_id, timestamp DESC
    """)).fetchall()

    return [
        {
            "server_id":             r.server_id,
            "latest_timestamp":      r.timestamp.isoformat(),
            "latest_cpu_percent":    r.cpu_percent,
            "latest_memory_percent": r.memory_percent,
        }
        for r in rows
    ]


@app.get("/api/metrics/{server_id}")
def get_metrics(server_id: str, limit: int = 20, db: Session = Depends(get_db)):
    """Returns the last N snapshots for a server, newest first."""
    rows = db.execute(text("""
        SELECT timestamp, cpu, memory, disk, network, processes,
               cpu_percent, memory_percent
        FROM metrics
        WHERE server_id = :server_id
        ORDER BY timestamp DESC
        LIMIT :limit
    """), {"server_id": server_id, "limit": limit}).fetchall()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No data for '{server_id}'. Is the agent running?"
        )

    return {
        "server_id": server_id,
        "returned":  len(rows),
        "snapshots": [dict(r._mapping) for r in rows],
    }


@app.get("/api/metrics/{server_id}/latest")
def get_latest(server_id: str, db: Session = Depends(get_db)):
    """Returns the single most recent snapshot for a server."""
    row = db.execute(text("""
        SELECT timestamp, cpu, memory, disk, network, processes,
               cpu_percent, memory_percent
        FROM metrics
        WHERE server_id = :server_id
        ORDER BY timestamp DESC
        LIMIT 1
    """), {"server_id": server_id}).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"No data for '{server_id}'")

    return dict(row._mapping)

@app.get("/api/anomalies/{server_id}/latest")
def get_latest_anomaly(server_id: str, db: Session = Depends(get_db)):
    """
    Analyzes the most recent snapshot against the server's baseline.
    Returns a severity rating and z-scores for each metric.

    CS 412 - This is the core Data Science output:
      Instead of raw numbers, we return meaning:
        "CPU is 3.2 standard deviations above normal — severe anomaly."
    """
    detector = AnomalyDetector(db, server_id)
    result   = detector.analyze_latest()

    if result is None:
        raise HTTPException(
            status_code=202,
            detail="Not enough data yet. Keep the agent running — need at least 10 readings."
        )
    return result


@app.get("/api/anomalies/{server_id}/history")
def get_anomaly_history(server_id: str, hours: int = 1, db: Session = Depends(get_db)):
    """
    Returns all anomalous readings in the last N hours.
    Useful for seeing when a problem started, not just that it exists now.
    """
    detector  = AnomalyDetector(db, server_id)
    anomalies = detector.get_anomaly_history(hours=hours)
    return {
        "server_id":     server_id,
        "hours_scanned": hours,
        "anomaly_count": len(anomalies),
        "anomalies":     anomalies,
    }


@app.get("/api/baseline/{server_id}")
def get_baseline(server_id: str, db: Session = Depends(get_db)):
    """
    Returns the current rolling baseline for a server.
    Shows what "normal" looks like right now for this specific server.
    """
    detector = AnomalyDetector(db, server_id)
    baseline = detector.get_baseline()

    if baseline is None:
        raise HTTPException(
            status_code=202,
            detail="Not enough data yet. Need at least 10 readings to establish a baseline."
        )
    return {"server_id": server_id, **baseline}

@app.post("/api/analyze/{server_id}")
def analyze_server(server_id: str, db: Session = Depends(get_db)):
    """
    Triggers AI root cause analysis for the current anomaly on a server.

    CS 414 - Why POST and not GET?
      This endpoint has a side effect: it calls an external API (Claude)
      which costs money and takes time. GET requests should be safe to
      call repeatedly with no consequences (idempotent).
      POST signals to clients that this is an action, not a simple read.
    """
    # First check if there is actually an anomaly to analyze
    detector = AnomalyDetector(db, server_id)
    anomaly  = detector.analyze_latest()

    if anomaly is None:
        raise HTTPException(
            status_code=202,
            detail="Not enough baseline data yet. Keep the agent running."
        )

    if not anomaly.get("is_anomaly"):
        return {
            "server_id": server_id,
            "message":   "No anomaly detected. Server is behaving normally.",
            "anomaly":   anomaly,
        }

    # Anomaly confirmed — run AI analysis
    analyzer = RootCauseAnalyzer(db)
    result   = analyzer.analyze(server_id, anomaly)
    return result

@app.post("/api/servers/register", status_code=201)
def register_server(
    server_id: str,
    description: str = "",
    db: Session = Depends(get_db)
):
    """
    Registers a new server and returns its API key.
    The raw key is shown ONCE — store it in your agent's .env file immediately.
    """
    existing = db.execute(text(
        "SELECT server_id FROM servers WHERE server_id = :id"
    ), {"id": server_id}).fetchone()

    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Server '{server_id}' is already registered. Use /rotate to get a new key."
        )

    raw_key, hashed_key = generate_api_key()

    db.execute(text("""
        INSERT INTO servers (server_id, api_key_hash, is_active, registered_at, description)
        VALUES (:server_id, :hash, true, :now, :desc)
    """), {
        "server_id": server_id,
        "hash":      hashed_key,
        "now":       datetime.now(timezone.utc),
        "desc":      description,
    })
    db.commit()

    return {
        "server_id":  server_id,
        "api_key":    raw_key,
        "warning":    "Store this key in your agent .env as AGENT_API_KEY. It will not be shown again.",
    }


@app.post("/api/servers/{server_id}/rotate")
def rotate_key(server_id: str, db: Session = Depends(get_db)):
    """
    Revokes the current API key and issues a new one.
    The old key stops working immediately.
    Update your agent's .env with the new key right away.
    """
    server = db.execute(text(
        "SELECT server_id FROM servers WHERE server_id = :id"
    ), {"id": server_id}).fetchone()

    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{server_id}' not found.")

    raw_key, hashed_key = generate_api_key()

    db.execute(text("""
        UPDATE servers
        SET api_key_hash    = :hash,
            is_active       = true,
            last_rotated_at = :now
        WHERE server_id = :id
    """), {"hash": hashed_key, "now": datetime.now(timezone.utc), "id": server_id})
    db.commit()

    return {
        "server_id":  server_id,
        "api_key":    raw_key,
        "rotated_at": datetime.now(timezone.utc).isoformat(),
        "warning":    "Old key is now invalid. Update your agent .env immediately.",
    }


@app.get("/api/servers/{server_id}/status")
def server_status(server_id: str, db: Session = Depends(get_db)):
    """Returns registration status for a server."""
    server = db.execute(text("""
        SELECT server_id, is_active, registered_at, last_rotated_at, description
        FROM servers WHERE server_id = :id
    """), {"id": server_id}).fetchone()

    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{server_id}' not found.")

    return dict(server._mapping)