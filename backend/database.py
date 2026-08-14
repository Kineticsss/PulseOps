"""
PulseOps - backend/database.py
================================
Handles the database connection and table creation.

CS 413 (Adv. Software Eng.) - Separation of Concerns:
  Database logic lives here, not in main.py.
  main.py should not care HOW data is stored, only THAT it is stored.
  This makes it easy to swap databases later without touching the API.

CS 415 (OS Principles) - Why TimescaleDB?
  Metrics are time-series data - every row has a timestamp and that
  timestamp is almost always part of every query ("give me the last
  5 minutes of CPU data"). TimescaleDB partitions data automatically
  by time (called hypertables), making time-range queries very fast.
"""

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# ── Connection ────────────────────────────────────────────────────────────────
# We read credentials from environment variables, not hardcoded strings.
# Hardcoding passwords in source code is a security risk - they end up in Git.
# For now we provide defaults that match our Docker container setup.

DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://pulseops:pulseops@127.0.0.1:5432/pulseops"
)

# create_engine() sets up the connection pool - a set of reusable connections.
# pool_pre_ping=True tests each connection before using it, reconnecting
# automatically if the database restarted.
engine = create_engine(DB_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


# ── Base class for all models ─────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Table creation ────────────────────────────────────────────────────────────
def init_db():
    """
    Creates all tables and converts the metrics table into a TimescaleDB
    hypertable - a table that is automatically partitioned by time.

    CS 412 (Data Science) - Why hypertables?
      A regular PostgreSQL table with millions of metric rows would need
      a full table scan for time-range queries. TimescaleDB splits the
      table into chunks by time interval (default: 7 days per chunk).
      Queries like "last 1 hour of data" only touch one or two chunks
      instead of scanning the entire table.
    """
    from backend.models import Base as ModelBase
    ModelBase.metadata.create_all(engine)

    # Convert to hypertable - this is the TimescaleDB-specific step.
    # IF NOT EXISTS means it is safe to call init_db() multiple times.
    with engine.connect() as conn:
        conn.execute(text("""
            SELECT create_hypertable(
                'metrics',
                by_range('timestamp'),
                if_not_exists => TRUE
            );
        """))
        conn.commit()
    print("[DB] Tables ready.")


def get_db():
    """
    Dependency function for FastAPI.
    Yields a database session and closes it when the request is done.
    This is the standard pattern for managing DB sessions in FastAPI.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
