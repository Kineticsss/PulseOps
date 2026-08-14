"""
PulseOps - backend/models.py
==============================
Defines the database table structure using SQLAlchemy ORM.

CS 413 (Adv. Software Eng.) - ORM (Object Relational Mapper):
  Instead of writing raw SQL to define tables, we define Python classes.
  SQLAlchemy translates them into the correct SQL for our database.
  Each class = one table. Each class attribute = one column.

CS 412 (Data Science) - Schema Design for Time-Series:
  The primary key is (server_id, timestamp) - not just an integer ID.
  This reflects how we query the data: always by server and time range.
  JSON columns store nested metric data (cpu, memory, disk, network)
  so we do not need separate tables for each metric type in Phase 2.
  We can normalize further in Phase 3 when we know our query patterns.
"""

from sqlalchemy import Column, String, Float, DateTime, JSON
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass


class Metric(Base):
    """
    One row = one snapshot from one server at one point in time.

    Column choices explained:
      timestamp  - when the snapshot was taken (UTC always)
      server_id  - which server sent this snapshot
      cpu        - JSON blob: percent_total, per_core, frequency, etc.
      memory     - JSON blob: ram and swap breakdowns
      disk       - JSON blob: partitions and io_counters
      network    - JSON blob: bytes sent/recv, rates, errors
      processes  - JSON blob: top 10 processes by CPU at this moment

    Why JSON columns for metrics?
      Metrics evolve - we might add new fields to cpu or memory later.
      With JSON columns, adding a field to the agent does not require
      a database migration. The tradeoff: we cannot query inside JSON
      as efficiently as dedicated columns. For Phase 2 this is fine.
      Phase 3 will move high-frequency query fields (cpu percent, memory
      percent) into dedicated float columns for anomaly detection queries.
    """
    __tablename__ = "metrics"

    timestamp  = Column(DateTime(timezone=True), primary_key=True, default=lambda: datetime.now(timezone.utc))
    server_id  = Column(String, primary_key=True, index=True)
    cpu        = Column(JSON, nullable=False)
    memory     = Column(JSON, nullable=False)
    disk       = Column(JSON, nullable=False)
    network    = Column(JSON, nullable=False)
    processes  = Column(JSON, nullable=False)

    # Convenience columns - extracted from JSON for fast queries.
    # These are the fields anomaly detection will query most often.
    cpu_percent    = Column(Float, nullable=True)
    memory_percent = Column(Float, nullable=True)
