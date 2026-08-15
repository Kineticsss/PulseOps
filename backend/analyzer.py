"""
PulseOps - backend/analyzer.py
================================
AI-powered root cause analysis using the Claude API.

CS 414 (Artificial Intelligence):
  This module is where the AI layer lives. When an anomaly is detected,
  we do not just show the user raw numbers — we ask an LLM to reason
  about what those numbers mean in context and suggest likely causes.

  This is PulseOps's core research contribution:
    Existing tools (Datadog, Grafana) tell you WHAT is wrong.
    PulseOps tells you WHY — with traceable, human-readable reasoning.

  The key engineering challenge is PROMPT DESIGN:
    - Too little context → vague, useless AI response
    - Too much context → expensive, slow, and the model loses focus
    - We send: anomaly report + last 10 minutes of metric trends
      That is enough for the model to reason about causality.

CS 413 (Adv. Software Eng.):
  The analyzer is isolated from the API layer.
  main.py calls analyzer.py — it does not know HOW the AI works.
  If we switch from Claude to GPT-4 or a local model later,
  only analyzer.py changes. Nothing else in the codebase touches it.
"""

import os
import anthropic
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text


# How many minutes of metric history to include in the AI prompt.
# 10 minutes = 120 readings at 5s interval.
# Enough to show trends leading up to the anomaly without overwhelming the model.
CONTEXT_WINDOW_MINUTES = 10


class RootCauseAnalyzer:
    """
    Fetches metric context, builds a structured prompt, calls Claude API,
    and returns a plain-English root cause analysis.
    """

    def __init__(self, db: Session):
        self.db = db
        # The Anthropic client reads ANTHROPIC_API_KEY from the environment.
        # Never hardcode API keys in source code — they end up in Git.
        self.client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY")
        )

    def get_recent_trend(self, server_id: str, minutes: int = CONTEXT_WINDOW_MINUTES) -> list:
        """
        Fetches the last N minutes of metrics to show the AI
        how values changed BEFORE and DURING the anomaly.

        CS 414 - Why trends matter for root cause analysis:
          A CPU spike that appeared suddenly points to a different cause
          than one that gradually climbed over 10 minutes.
          Sudden = likely a single event (cron job fired, traffic spike, OOM killer)
          Gradual = likely a resource leak (memory leak, connection pool exhaustion)
          The trend is the diagnostic clue.
        """
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        rows = self.db.execute(text("""
            SELECT
                timestamp,
                cpu_percent,
                memory_percent,
                network
            FROM metrics
            WHERE server_id = :server_id
              AND timestamp >= :since
            ORDER BY timestamp ASC
        """), {"server_id": server_id, "since": since}).fetchall()

        return [
            {
                "timestamp":      r.timestamp.strftime("%H:%M:%S"),
                "cpu_percent":    r.cpu_percent,
                "memory_percent": r.memory_percent,
                "bytes_recv_per_sec": (r.network or {}).get("bytes_recv_per_sec"),
                "bytes_sent_per_sec": (r.network or {}).get("bytes_sent_per_sec"),
            }
            for r in rows
        ]

    def get_top_processes(self, server_id: str) -> list:
        """
        Fetches the top processes from the most recent snapshot.
        Processes are critical context — a runaway process is often the cause
        of CPU anomalies, and a memory-hungry process explains memory spikes.
        """
        row = self.db.execute(text("""
            SELECT processes
            FROM metrics
            WHERE server_id = :server_id
            ORDER BY timestamp DESC
            LIMIT 1
        """), {"server_id": server_id}).fetchone()

        if not row or not row.processes:
            return []

        return [
            {
                "name":           p.get("name"),
                "cpu_percent":    p.get("cpu_percent"),
                "memory_percent": p.get("memory_percent"),
                "status":         p.get("status"),
                "num_threads":    p.get("num_threads"),
            }
            for p in (row.processes or [])[:5]  # top 5 is enough context
        ]

    def build_prompt(self, server_id: str, anomaly_report: dict, trend: list, processes: list) -> str:
        """
        CS 414 - Prompt Engineering:
          The quality of the AI's output depends entirely on the quality
          of the prompt. Good prompt design follows these principles:

          1. ROLE: Tell the model what kind of expert it is.
             "You are a senior DevOps engineer" → response uses DevOps reasoning.

          2. CONTEXT: Give structured, specific data — not prose descriptions.
             Raw numbers are more useful to the model than "CPU was high."

          3. TASK: Be explicit about what you want.
             "Identify the most likely root cause" is clearer than "analyze this."

          4. FORMAT: Specify the output structure.
             We want JSON so we can parse specific fields (cause, confidence,
             actions) and display them separately in the UI.

          5. CONSTRAINTS: Limit scope to avoid hallucination.
             "Base your analysis only on the data provided" prevents the model
             from inventing causes that have no basis in the metrics.
        """
        # Format trend as a readable table for the model
        trend_lines = "\n".join([
            f"  {r['timestamp']} | CPU: {r['cpu_percent']:5.1f}% | "
            f"MEM: {r['memory_percent']:5.1f}% | "
            f"NET_IN: {int(r['bytes_recv_per_sec'] or 0):>8} B/s | "
            f"NET_OUT: {int(r['bytes_sent_per_sec'] or 0):>8} B/s"
            for r in trend
        ])

        process_lines = "\n".join([
            f"  {p['name']} | CPU: {p['cpu_percent']}% | MEM: {p['memory_percent']}% | "
            f"Status: {p['status']} | Threads: {p['num_threads']}"
            for p in processes
        ])

        anomalous_metrics = [
            f"{metric}: {data['current']}% (baseline mean: {data['baseline_mean']}%, "
            f"z-score: {data['z_score']}, severity: {data['status']})"
            for metric, data in anomaly_report.get("metrics", {}).items()
            if data["status"] != "normal"
        ]

        return f"""You are a senior DevOps engineer and systems reliability expert.
A monitoring system has detected anomalies on server "{server_id}".
Analyze the data below and identify the most likely root cause.

=== ANOMALY SUMMARY ===
Overall severity: {anomaly_report.get("overall", "unknown").upper()}
Detected at: {anomaly_report.get("timestamp", "unknown")}
Anomalous metrics:
{chr(10).join(f"  - {m}" for m in anomalous_metrics)}

=== METRIC TREND (last {CONTEXT_WINDOW_MINUTES} minutes) ===
  Time     | CPU      | Memory   | Network In    | Network Out
{trend_lines if trend_lines else "  No trend data available."}

=== TOP PROCESSES (at time of anomaly) ===
{process_lines if process_lines else "  No process data available."}

=== YOUR TASK ===
Based ONLY on the data provided above, respond with a JSON object in this exact format:
{{
  "summary": "One sentence describing what is happening on this server right now.",
  "likely_cause": "The single most probable root cause, explained in plain English.",
  "confidence": "high | medium | low",
  "confidence_reason": "Why you are or are not confident in this diagnosis.",
  "supporting_evidence": ["Evidence point 1 from the data", "Evidence point 2", "Evidence point 3"],
  "recommended_actions": ["Immediate action 1", "Immediate action 2", "Follow-up action"],
  "escalate": true or false
}}

Rules:
- Base your analysis only on the data provided. Do not invent causes not supported by the metrics.
- "escalate": true if the issue requires immediate human intervention.
- Be specific. Reference actual numbers from the data in your reasoning.
- Respond with ONLY the JSON object. No preamble, no explanation outside the JSON."""

    def analyze(self, server_id: str, anomaly_report: dict) -> dict:
        """
        Orchestrates the full analysis:
          1. Fetch metric trend
          2. Fetch top processes
          3. Build prompt
          4. Call Claude API
          5. Parse and return structured response

        CS 414 - Model choice:
          We use claude-sonnet-4-6 — fast enough for real-time use
          (response in 2-5 seconds) and smart enough for systems reasoning.
          Claude Opus would give better analysis but is slower and more expensive.
          For an alerting system, latency matters — a 30-second analysis
          defeats the purpose of real-time monitoring.
        """
        if not os.getenv("ANTHROPIC_API_KEY"):
            return {
                "error": "ANTHROPIC_API_KEY not set.",
                "hint":  "Run: $env:ANTHROPIC_API_KEY='your-key-here' in PowerShell"
            }

        trend     = self.get_recent_trend(server_id)
        processes = self.get_top_processes(server_id)
        prompt    = self.build_prompt(server_id, anomaly_report, trend, processes)

        try:
            response = self.client.messages.create(
                model      = "claude-sonnet-4-6",
                max_tokens = 1024,
                messages   = [{"role": "user", "content": prompt}],
            )

            raw_text = response.content[0].text.strip()

            # Strip markdown code fences if the model wrapped the JSON
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]

            import json
            analysis = json.loads(raw_text)

            return {
                "server_id":        server_id,
                "analyzed_at":      datetime.now(timezone.utc).isoformat(),
                "anomaly_severity": anomaly_report.get("overall"),
                "analysis":         analysis,
                "context": {
                    "trend_readings":    len(trend),
                    "processes_checked": len(processes),
                    "context_minutes":   CONTEXT_WINDOW_MINUTES,
                }
            }

        except Exception as e:
            return {
                "error":   f"Analysis failed: {str(e)}",
                "server_id": server_id,
            }
