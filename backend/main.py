"""
PulseOps - backend/main.py
===========================
Ingestion API: receives snapshots from agents, stores and exposes them.

CS 413 (Adv. Software Eng.):
  FastAPI validates incoming JSON via Pydantic automatically.
  Bad requests are rejected before your code runs.
  Visit /docs when running for a free interactive API explorer.

Run with:
  uvicorn backend.main:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any
from datetime import datetime, timezone
from collections import defaultdict

app = FastAPI(
    title="PulseOps API",
    description="Ingestion and query API for the PulseOps monitoring agent",
    version="0.1.0",
)

MAX_SNAPSHOTS_PER_SERVER = 200
metrics_store: dict[str, list] = defaultdict(list)


class SnapshotPayload(BaseModel):
    server_id:     str
    timestamp:     str
    cpu:           dict[str, Any]
    memory:        dict[str, Any]
    disk:          dict[str, Any]
    network:       dict[str, Any]
    top_processes: list[dict[str, Any]]

class ServerSummary(BaseModel):
    server_id:             str
    snapshot_count:        int
    latest_timestamp:      str | None
    latest_cpu_percent:    float | None
    latest_memory_percent: float | None


@app.get("/")
def root():
    """Health check."""
    return {
        "service": "PulseOps API",
        "status":  "running",
        "version": "0.1.0",
        "time":    datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/metrics", status_code=201)
def ingest_metrics(payload: SnapshotPayload):
    """
    201 Created = correct status for storing a new resource.
    200 OK = processed your request (slightly different semantics).
    """
    data = payload.model_dump()
    metrics_store[payload.server_id].append(data)
    if len(metrics_store[payload.server_id]) > MAX_SNAPSHOTS_PER_SERVER:
        metrics_store[payload.server_id] = metrics_store[payload.server_id][-MAX_SNAPSHOTS_PER_SERVER:]
    return {
        "status":           "accepted",
        "server_id":        payload.server_id,
        "stored_snapshots": len(metrics_store[payload.server_id]),
    }


@app.get("/api/servers")
def list_servers() -> list[ServerSummary]:
    """Lists all servers that have sent at least one snapshot."""
    summaries = []
    for server_id, snapshots in metrics_store.items():
        if not snapshots:
            continue
        latest = snapshots[-1]
        summaries.append(ServerSummary(
            server_id             = server_id,
            snapshot_count        = len(snapshots),
            latest_timestamp      = latest.get("timestamp"),
            latest_cpu_percent    = latest.get("cpu", {}).get("percent_total"),
            latest_memory_percent = latest.get("memory", {}).get("ram", {}).get("percent_used"),
        ))
    return summaries


@app.get("/api/metrics/{server_id}")
def get_metrics(server_id: str, limit: int = 20):
    """
    Path param  : /api/metrics/{server_id}
    Query param : ?limit=20 (optional, default 20)
    FastAPI extracts both from the function signature automatically.
    """
    if server_id not in metrics_store:
        raise HTTPException(status_code=404, detail=f"No data for '{server_id}'. Is the agent running?")
    snapshots = metrics_store[server_id]
    return {
        "server_id":    server_id,
        "total_stored": len(snapshots),
        "returned":     min(limit, len(snapshots)),
        "snapshots":    snapshots[-limit:],
    }


@app.get("/api/metrics/{server_id}/latest")
def get_latest(server_id: str):
    """Most recent snapshot only. Good for a live status widget."""
    if server_id not in metrics_store or not metrics_store[server_id]:
        raise HTTPException(status_code=404, detail=f"No data for '{server_id}'")
    return metrics_store[server_id][-1]
