"""
PulseOps - backend/models.py
==============================
Defines all database tables using SQLAlchemy ORM.

Phase 2 added: Metric table
Phase 5 added: Alert table — stores alert history for cooldown and audit
"""

from sqlalchemy import Column, String, Float, DateTime, JSON, Integer, Boolean
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass


class Metric(Base):
    """One row = one snapshot from one server at one point in time."""
    __tablename__ = "metrics"

    timestamp      = Column(DateTime(timezone=True), primary_key=True, default=lambda: datetime.now(timezone.utc))
    server_id      = Column(String, primary_key=True, index=True)
    cpu            = Column(JSON, nullable=False)
    memory         = Column(JSON, nullable=False)
    disk           = Column(JSON, nullable=False)
    network        = Column(JSON, nullable=False)
    processes      = Column(JSON, nullable=False)
    cpu_percent    = Column(Float, nullable=True)
    memory_percent = Column(Float, nullable=True)


class Alert(Base):
    """
    One row = one alert that was triggered.

    Serves two purposes:
      1. Cooldown: check if we alerted for this server+severity recently
      2. Audit trail: when was each server alerted, did delivery succeed

    Why not store the full anomaly payload?
      We only need the z-scores and severity for audit purposes.
      The full metric data is already in the Metric table — no duplication.
    """
    __tablename__ = "alerts"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    server_id      = Column(String, nullable=False, index=True)
    severity       = Column(String, nullable=False)  # mild | severe
    triggered_at   = Column(DateTime(timezone=True), nullable=False)
    cpu_z_score    = Column(Float, nullable=True)
    memory_z_score = Column(Float, nullable=True)
    slack_sent     = Column(Boolean, default=False)
    email_sent     = Column(Boolean, default=False)