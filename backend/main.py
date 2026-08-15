"""
PulseOps - backend/main.py (Phase 2)
======================================
Updated to write snapshots to TimescaleDB instead of in-memory store.
The API surface stays exactly the same - the agent does not need any changes.

CS 413 (Adv. Software Eng.) - This is the Open/Closed Principle:
  The API is open for extension (we added a database) but closed for
  modification (the agent and any existing clients see no difference).
"""

from backend.anomaly import AnomalyDetector
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Any
from datetime import datetime, timezone
from backend.analyzer import RootCauseAnalyzer
from backend.anomaly import AnomalyDetector

from backend.database import init_db, get_db
from backend.models import Metric

app = FastAPI(
    title="PulseOps API",
    description="Ingestion and query API for the PulseOps monitoring agent",
    version="0.2.0",
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
        "version": "0.2.0",
        "time":    datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/metrics", status_code=201)
def ingest_metrics(payload: SnapshotPayload, db: Session = Depends(get_db)):
    """
    Writes the snapshot to TimescaleDB.

    Depends(get_db) is FastAPI's dependency injection.
    FastAPI calls get_db(), passes the session to this function,
    and closes the session automatically when the request is done.
    We never manually open or close DB sessions in route handlers.
    """
    metric = Metric(
        timestamp      = datetime.fromisoformat(payload.timestamp),
        server_id      = payload.server_id,
        cpu            = payload.cpu,
        memory         = payload.memory,
        disk           = payload.disk,
        network        = payload.network,
        processes      = payload.top_processes,
        # Extract convenience columns for fast queries
        cpu_percent    = payload.cpu.get("percent_total"),
        memory_percent = payload.memory.get("ram", {}).get("percent_used"),
    )
    db.add(metric)
    db.commit()

    return {
        "status":    "accepted",
        "server_id": payload.server_id,
        "timestamp": payload.timestamp,
    }


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