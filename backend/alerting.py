"""
PulseOps - backend/alerting.py
================================
Handles alert delivery via Slack webhook and Gmail SMTP.

CS 413 (Adv. Software Eng.) - Security principles applied:
  - All credentials loaded from environment variables via .env
  - Gmail App Password used — not your real password
  - Slack webhook scoped to one channel only
  - Cooldown stored in DB, not memory — survives server restarts
  - Alert failures are logged but never crash the ingestion pipeline

CS 413 - Why separate from main.py?
  AlertManager has one responsibility: deliver alerts.
  main.py should not know HOW alerts are sent — only THAT they are sent.
  Swapping Slack for PagerDuty later means only this file changes.
"""

import os
import smtplib
import httpx
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text

COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "5"))

SEVERITY_EMOJI = {"mild": "⚠️", "severe": "🔴"}


class AlertManager:
    """
    Checks cooldown, sends Slack + email alerts, logs to DB.

    CS 413 - Fail-safe design:
      If Slack fails, we still attempt email.
      If email fails, we still log the attempt to DB.
      Nothing here can crash the metric ingestion pipeline.
    """

    def __init__(self, db: Session):
        self.db         = db
        self.slack_url  = os.getenv("SLACK_WEBHOOK_URL")
        self.email_from = os.getenv("ALERT_EMAIL_SENDER")
        self.email_pass = os.getenv("ALERT_EMAIL_PASSWORD")
        self.email_to   = os.getenv("ALERT_EMAIL_RECIPIENT")

    # ── Cooldown ──────────────────────────────────────────────────────────────
    def is_on_cooldown(self, server_id: str, severity: str) -> bool:
        """
        Returns True if an alert for this server+severity was already sent
        within the cooldown window.

        Why DB and not in-memory?
          If the backend restarts during an active anomaly, in-memory cooldown
          is lost and alerts fire again immediately on the next ingestion.
          DB-backed cooldown survives restarts.

        Why cooldown per severity?
          A mild anomaly escalating to severe should always alert —
          even if the mild alert was sent 2 minutes ago.
        """
        since = datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)
        row   = self.db.execute(text("""
            SELECT id FROM alerts
            WHERE server_id    = :server_id
              AND severity     = :severity
              AND triggered_at >= :since
            LIMIT 1
        """), {"server_id": server_id, "severity": severity, "since": since}).fetchone()
        return row is not None

    def log_alert(self, server_id: str, severity: str, anomaly: dict, slack_ok: bool, email_ok: bool):
        """
        Persists alert to DB for two purposes:
          1. Cooldown checks on future ingestions
          2. Audit trail — when was each server alerted, was delivery successful
        """
        self.db.execute(text("""
            INSERT INTO alerts
              (server_id, severity, triggered_at, cpu_z_score, memory_z_score, slack_sent, email_sent)
            VALUES
              (:server_id, :severity, :triggered_at, :cpu_z, :memory_z, :slack_sent, :email_sent)
        """), {
            "server_id":    server_id,
            "severity":     severity,
            "triggered_at": datetime.now(timezone.utc),
            "cpu_z":        anomaly.get("metrics", {}).get("cpu", {}).get("z_score"),
            "memory_z":     anomaly.get("metrics", {}).get("memory", {}).get("z_score"),
            "slack_sent":   slack_ok,
            "email_sent":   email_ok,
        })
        self.db.commit()

    # ── Slack ─────────────────────────────────────────────────────────────────
    def send_slack(self, server_id: str, anomaly: dict) -> bool:
        """
        Sends a structured Slack message using Block Kit.
        Block Kit = Slack's JSON-based UI framework for rich messages.

        Security: the webhook URL is scoped to one channel.
          Even if the URL leaks, an attacker can only post to #pulseops-alerts.
          They cannot read messages, access other channels, or perform any
          other Slack action. Least-privilege by design.
        """
        if not self.slack_url:
            print("[Alert] SLACK_WEBHOOK_URL not set — skipping Slack.")
            return False

        severity = anomaly.get("overall", "unknown")
        emoji    = SEVERITY_EMOJI.get(severity, "❓")
        cpu      = anomaly.get("metrics", {}).get("cpu", {})
        memory   = anomaly.get("metrics", {}).get("memory", {})

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} PulseOps — {severity.upper()} on {server_id}"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Server:*\n`{server_id}`"},
                        {"type": "mrkdwn", "text": f"*Severity:*\n{severity.upper()}"},
                        {"type": "mrkdwn", "text": f"*CPU:*\n{cpu.get('current')}%  (z={cpu.get('z_score')}, {cpu.get('status')})"},
                        {"type": "mrkdwn", "text": f"*Memory:*\n{memory.get('current')}%  (z={memory.get('z_score')}, {memory.get('status')})"},
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Detected at:* {anomaly.get('timestamp', 'unknown')}\n"
                            f"*Baseline samples:* {anomaly.get('baseline_sample_count', 'N/A')}\n"
                            f"Run `POST /api/analyze/{server_id}` for AI root cause analysis."
                        )
                    }
                },
            ]
        }

        try:
            r = httpx.post(self.slack_url, json=payload, timeout=10.0)
            r.raise_for_status()
            print(f"[Alert] Slack sent — {server_id} ({severity})")
            return True
        except Exception as e:
            print(f"[Alert] Slack failed: {e}")
            return False

    # ── Email ─────────────────────────────────────────────────────────────────
    def send_email(self, server_id: str, anomaly: dict) -> bool:
        """
        Sends an HTML + plain-text email via Gmail SMTP over SSL (port 465).

        Security:
          - App Password used, not your real Gmail password.
            App Passwords are revocable per-app — if this one leaks,
            you revoke it without changing your Google password or
            affecting any other app.
          - SMTP_SSL (port 465) encrypts the connection end-to-end.
            Credentials and message content are never sent in plaintext.
          - We do NOT log the password anywhere — only success/failure.

        Why both HTML and plain text?
          Some email clients (corporate, CLI-based) do not render HTML.
          MIMEMultipart("alternative") sends both — the client picks the
          best version it can display.
        """
        if not all([self.email_from, self.email_pass, self.email_to]):
            print("[Alert] Email credentials not fully configured — skipping.")
            return False

        severity = anomaly.get("overall", "unknown")
        emoji    = SEVERITY_EMOJI.get(severity, "❓")
        cpu      = anomaly.get("metrics", {}).get("cpu", {})
        memory   = anomaly.get("metrics", {}).get("memory", {})
        color    = "#dc2626" if severity == "severe" else "#d97706"
        subject  = f"{emoji} PulseOps: {severity.upper()} anomaly on {server_id}"

        plain = (
            f"PulseOps Anomaly Alert\n"
            f"{'='*40}\n"
            f"Server   : {server_id}\n"
            f"Severity : {severity.upper()}\n"
            f"Time     : {anomaly.get('timestamp', 'unknown')}\n\n"
            f"CPU    : {cpu.get('current')}%  "
            f"(mean: {cpu.get('baseline_mean')}%, z={cpu.get('z_score')}, {cpu.get('status')})\n"
            f"Memory : {memory.get('current')}%  "
            f"(mean: {memory.get('baseline_mean')}%, z={memory.get('z_score')}, {memory.get('status')})\n\n"
            f"Run POST /api/analyze/{server_id} for AI root cause analysis."
        )

        html = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:{color};padding:20px;border-radius:8px 8px 0 0;">
    <h2 style="color:white;margin:0;">{emoji} PulseOps Alert — {severity.upper()}</h2>
  </div>
  <div style="background:#f9fafb;padding:20px;border:1px solid #e5e7eb;border-radius:0 0 8px 8px;">
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr><td style="padding:8px;font-weight:bold;width:120px;">Server</td><td style="padding:8px;font-family:monospace;">{server_id}</td></tr>
      <tr style="background:#fff"><td style="padding:8px;font-weight:bold;">Time</td><td style="padding:8px;">{anomaly.get('timestamp','')}</td></tr>
      <tr><td style="padding:8px;font-weight:bold;">CPU</td>
          <td style="padding:8px;">{cpu.get('current')}% &nbsp;<span style="color:#6b7280;">(mean {cpu.get('baseline_mean')}%, z={cpu.get('z_score')}, <b>{cpu.get('status')}</b>)</span></td></tr>
      <tr style="background:#fff"><td style="padding:8px;font-weight:bold;">Memory</td>
          <td style="padding:8px;">{memory.get('current')}% &nbsp;<span style="color:#6b7280;">(mean {memory.get('baseline_mean')}%, z={memory.get('z_score')}, <b>{memory.get('status')}</b>)</span></td></tr>
    </table>
    <p style="margin-top:16px;font-size:13px;color:#6b7280;">
      Run <code>POST /api/analyze/{server_id}</code> for AI root cause analysis.
    </p>
  </div>
</body></html>""".strip()

        try:
            msg              = MIMEMultipart("alternative")
            msg["Subject"]   = subject
            msg["From"]      = self.email_from
            msg["To"]        = self.email_to
            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(html,  "html"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.email_from, self.email_pass)
                server.sendmail(self.email_from, self.email_to, msg.as_string())

            print(f"[Alert] Email sent — {server_id} ({severity})")
            return True
        except Exception as e:
            print(f"[Alert] Email failed: {e}")
            return False

    # ── Main trigger ──────────────────────────────────────────────────────────
    def trigger(self, server_id: str, anomaly: dict):
        """
        Entry point called after every metric ingestion.
        Checks severity → checks cooldown → sends alerts → logs to DB.

        Order of operations matters:
          1. Check severity first — skip immediately if normal
          2. Check cooldown — skip if recently alerted for same severity
          3. Send Slack (attempt regardless of email config)
          4. Send email  (attempt regardless of Slack result)
          5. Log both results to DB
        """
        severity = anomaly.get("overall")
        if severity not in ("mild", "severe"):
            return  # normal reading — nothing to do

        if self.is_on_cooldown(server_id, severity):
            print(f"[Alert] {server_id} ({severity}) on cooldown — skipping.")
            return

        print(f"[Alert] Anomaly on {server_id} ({severity}) — sending alerts.")
        slack_ok = self.send_slack(server_id, anomaly)
        email_ok = self.send_email(server_id, anomaly)
        self.log_alert(server_id, severity, anomaly, slack_ok, email_ok)
