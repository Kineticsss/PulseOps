"""
PulseOps - backend/models.py
==============================
All database tables.

Phase 2: Metric
Phase 5: Alert
Phase 7: Server, AuthLog
"""

from sqlalchemy import Column, String, Float, DateTime, JSON, Integer, Boolean, Text
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass


class Metric(Base):
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
    __tablename__ = "alerts"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    server_id      = Column(String, nullable=False, index=True)
    severity       = Column(String, nullable=False)
    triggered_at   = Column(DateTime(timezone=True), nullable=False)
    cpu_z_score    = Column(Float, nullable=True)
    memory_z_score = Column(Float, nullable=True)
    slack_sent     = Column(Boolean, default=False)
    email_sent     = Column(Boolean, default=False)


class Server(Base):
    """
    Registered servers and their hashed API keys.

    Why store only the hash?
      The raw key is shown once at registration and never stored.
      If this table leaks, no raw keys are exposed.
    """
    __tablename__ = "servers"

    server_id       = Column(String, primary_key=True)
    api_key_hash    = Column(String, nullable=False, unique=True, index=True)
    is_active       = Column(Boolean, default=True, nullable=False)
    registered_at   = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    last_rotated_at = Column(DateTime(timezone=True), nullable=True)
    description     = Column(Text, nullable=True)


class AuthLog(Base):
    """
    Failed authentication attempts.
    Used to detect misconfigured agents or brute-force attempts.
    """
    __tablename__ = "auth_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    key_prefix   = Column(String, nullable=False)
    reason       = Column(String, nullable=False)
    attempted_at = Column(DateTime(timezone=True), nullable=False)