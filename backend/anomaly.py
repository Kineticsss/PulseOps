"""
PulseOps - backend/anomaly.py
==============================
Anomaly detection using Z-score statistics on rolling windows.

CS 412 (Data Science):
  We cannot know if a reading is "abnormal" without first knowing what
  "normal" looks like for that specific server. A CPU spike to 80% might
  be catastrophic for a web server and perfectly normal for a build server.

  This is why we build a per-server baseline from historical data, then
  measure how far each new reading deviates from that baseline.

  Z-score = (current_value - mean) / standard_deviation
  It answers: "How many standard deviations away from normal is this?"

  Industry thresholds:
    |z| > 2 → mild anomaly (worth watching)
    |z| > 3 → severe anomaly (alert)

  Why Z-score before Isolation Forest?
    Z-score is interpretable — you can explain exactly why something was
    flagged. "CPU is 3.4 standard deviations above its 1-hour average"
    is a sentence a non-technical person can understand.
    Isolation Forest (Phase 4) handles multivariate anomalies better
    but is a black box by comparison.
"""

import statistics
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text


# How many recent snapshots to use for the baseline calculation.
# 720 snapshots at 5s interval = 1 hour of history.
# Too short: baseline is too sensitive to recent spikes.
# Too long: baseline adapts too slowly to legitimate load changes.
BASELINE_WINDOW = 720

# Z-score thresholds
MILD_THRESHOLD   = 2.0
SEVERE_THRESHOLD = 3.0


class AnomalyDetector:
    """
    Computes rolling baselines and detects anomalies for a given server.

    CS 412 - Rolling Window Baseline:
      Instead of comparing against an all-time average (which changes as
      the server ages), we compare against a recent window. This means
      the baseline automatically adapts to legitimate long-term changes
      like a server taking on more traffic over time.
    """

    def __init__(self, db: Session, server_id: str):
        self.db = db
        self.server_id = server_id

    def get_baseline(self) -> dict | None:
        """
        Fetches the last BASELINE_WINDOW readings and computes:
          - mean (average value)
          - stdev (how spread out the values are)
          - min / max (observed range)

        Returns None if there is not enough data to form a baseline.
        We require at least 10 readings before making any judgments.
        """
        rows = self.db.execute(text("""
            SELECT cpu_percent, memory_percent
            FROM metrics
            WHERE server_id = :server_id
              AND cpu_percent IS NOT NULL
              AND memory_percent IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT :window
        """), {"server_id": self.server_id, "window": BASELINE_WINDOW}).fetchall()

        if len(rows) < 10:
            return None  # Not enough data yet

        cpu_values    = [r.cpu_percent    for r in rows]
        memory_values = [r.memory_percent for r in rows]

        return {
            "sample_count": len(rows),
            "cpu": {
                "mean":  statistics.mean(cpu_values),
                "stdev": statistics.stdev(cpu_values) if len(cpu_values) > 1 else 0,
                "min":   min(cpu_values),
                "max":   max(cpu_values),
            },
            "memory": {
                "mean":  statistics.mean(memory_values),
                "stdev": statistics.stdev(memory_values) if len(memory_values) > 1 else 0,
                "min":   min(memory_values),
                "max":   max(memory_values),
            },
        }

    def z_score(self, value: float, mean: float, stdev: float) -> float | None:
        """
        Computes how many standard deviations `value` is from `mean`.

        CS 412 - Z-score formula:
          z = (x - μ) / σ
          where x = current value, μ = mean, σ = standard deviation

        Returns None if stdev is 0 (all values were identical — no variance,
        so we cannot compute a meaningful z-score).
        """
        if stdev == 0:
            return None
        return (value - mean) / stdev

    def classify(self, z: float | None) -> str:
        """
        Maps a z-score to a human-readable severity label.
        Absolute value because we care about both high AND low anomalies.
        A CPU drop to near 0% on a busy server can also indicate a problem
        (process crashed, traffic stopped routing, etc.).
        """
        if z is None:
            return "unknown"
        abs_z = abs(z)
        if abs_z >= SEVERE_THRESHOLD:
            return "severe"
        if abs_z >= MILD_THRESHOLD:
            return "mild"
        return "normal"

    def analyze_latest(self) -> dict | None:
        """
        Fetches the most recent snapshot, compares it against the baseline,
        and returns an anomaly report.

        Returns None if there is not enough historical data yet.
        """
        baseline = self.get_baseline()
        if baseline is None:
            return None

        # Get the latest reading
        latest = self.db.execute(text("""
            SELECT timestamp, cpu_percent, memory_percent
            FROM metrics
            WHERE server_id = :server_id
              AND cpu_percent IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 1
        """), {"server_id": self.server_id}).fetchone()

        if not latest:
            return None

        # Compute z-scores
        cpu_z    = self.z_score(latest.cpu_percent,    baseline["cpu"]["mean"],    baseline["cpu"]["stdev"])
        memory_z = self.z_score(latest.memory_percent, baseline["memory"]["mean"], baseline["memory"]["stdev"])

        cpu_status    = self.classify(cpu_z)
        memory_status = self.classify(memory_z)

        # Overall status = worst of all metrics
        severity_rank = {"normal": 0, "mild": 1, "severe": 2, "unknown": 0}
        overall = max([cpu_status, memory_status], key=lambda s: severity_rank[s])

        return {
            "server_id":  self.server_id,
            "timestamp":  latest.timestamp.isoformat(),
            "overall":    overall,
            "is_anomaly": overall in ("mild", "severe"),
            "metrics": {
                "cpu": {
                    "current":  latest.cpu_percent,
                    "baseline_mean":  round(baseline["cpu"]["mean"], 2),
                    "baseline_stdev": round(baseline["cpu"]["stdev"], 2),
                    "z_score":  round(cpu_z, 2) if cpu_z is not None else None,
                    "status":   cpu_status,
                },
                "memory": {
                    "current":  latest.memory_percent,
                    "baseline_mean":  round(baseline["memory"]["mean"], 2),
                    "baseline_stdev": round(baseline["memory"]["stdev"], 2),
                    "z_score":  round(memory_z, 2) if memory_z is not None else None,
                    "status":   memory_status,
                },
            },
            "baseline_sample_count": baseline["sample_count"],
        }

    def get_anomaly_history(self, hours: int = 1) -> list:
        """
        Scans the last N hours of readings and flags which ones were anomalous.
        Returns a list of anomalous snapshots with their z-scores.

        CS 412 - Retrospective Analysis:
          This lets you look back and ask "when did things start going wrong?"
          rather than only knowing about anomalies at the moment they happen.
        """
        baseline = self.get_baseline()
        if baseline is None:
            return []

        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows = self.db.execute(text("""
            SELECT timestamp, cpu_percent, memory_percent
            FROM metrics
            WHERE server_id   = :server_id
              AND timestamp   >= :since
              AND cpu_percent IS NOT NULL
            ORDER BY timestamp ASC
        """), {"server_id": self.server_id, "since": since}).fetchall()

        anomalies = []
        for row in rows:
            cpu_z    = self.z_score(row.cpu_percent,    baseline["cpu"]["mean"],    baseline["cpu"]["stdev"])
            memory_z = self.z_score(row.memory_percent, baseline["memory"]["mean"], baseline["memory"]["stdev"])

            cpu_status    = self.classify(cpu_z)
            memory_status = self.classify(memory_z)

            if cpu_status != "normal" or memory_status != "normal":
                anomalies.append({
                    "timestamp":     row.timestamp.isoformat(),
                    "cpu_percent":   row.cpu_percent,
                    "cpu_z_score":   round(cpu_z, 2) if cpu_z is not None else None,
                    "cpu_status":    cpu_status,
                    "memory_percent": row.memory_percent,
                    "memory_z_score": round(memory_z, 2) if memory_z is not None else None,
                    "memory_status": memory_status,
                })

        return anomalies
