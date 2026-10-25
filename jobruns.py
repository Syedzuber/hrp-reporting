"""
jobruns.py — a durable record of every scheduled job run.

scheduler.py keeps LAST_RUN in memory for its status page, but that dict
empties on every restart and is invisible to any other process. A watchdog
reading it sees "nothing recorded" after every redeploy, and one running in a
second process always sees an empty dict — and both of those look like health.

So every run is also written to a job_runs table in the same SQLite file as
the CRM snapshot, on the Railway volume, where it survives restarts and any
process can read it.

A row is written when a job starts, with status "running", and updated when
it ends to "ok" or "failed". A run killed part-way — a redeploy, an
out-of-memory kill — therefore stays at "running" instead of leaving no trace,
and the healthcheck reports it.

Nothing here raises: a broken audit trail must never stop a report going out.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, time

import settings

log = logging.getLogger(__name__)

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS job_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        job          TEXT NOT NULL,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        status       TEXT NOT NULL,
        error        TEXT
    );
    CREATE INDEX IF NOT EXISTS job_runs_by_job ON job_runs (job, id);
"""


def _connect():
    # The sync holds write locks while it stores each module, so wait for it
    # rather than failing straight away.
    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    conn.executescript(_SCHEMA)
    return conn


def start(job: str, started_at: datetime) -> int | None:
    """Record that `job` has begun. Returns the row id, or None if not recorded."""
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO job_runs (job, started_at, status) VALUES (?, ?, 'running')",
                (job, started_at.isoformat()))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception:
        log.exception("Could not record the start of %s in job_runs", job)
        return None


def finish(run_id: int | None, job: str, started_at: datetime,
           finished_at: datetime, status: str, error: str | None = None) -> None:
    """Record how `job` ended. If its start was never recorded, insert a full row."""
    try:
        conn = _connect()
        try:
            if run_id is not None:
                conn.execute(
                    "UPDATE job_runs SET finished_at=?, status=?, error=? WHERE id=?",
                    (finished_at.isoformat(), status, error, run_id))
            else:
                conn.execute(
                    "INSERT INTO job_runs (job, started_at, finished_at, status, error) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (job, started_at.isoformat(), finished_at.isoformat(), status, error))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("Could not record the outcome of %s in job_runs", job)


def _clock(value, fallback: str) -> time:
    try:
        h, m = str(value).split(":")[:2]
        return time(int(h), int(m))
    except Exception:
        h, m = fallback.split(":")
        return time(int(h), int(m))


def scheduled_jobs() -> list[tuple[str, time]]:
    """(job name, daily IST time) for every report job scheduler.py registers.

    This mirrors scheduler.main(), including its fallback times, and the names
    must match what _wrap records. A job added there must be added here, or the
    healthcheck will not watch it. The healthcheck itself is left out: it cannot
    usefully report on its own absence.
    """
    out = []
    if settings.MORNING_ENABLED:
        out.append(("morning", _clock(settings.MORNING_AT, "08:00")))
    if settings.DAYSTART_ENABLED:
        out.append(("daystart", _clock(settings.DAYSTART_AT, "09:00")))
    if settings.SHIFT_ENABLED:
        for raw in settings.SHIFT_TIMES:
            t = _clock(raw, "20:00")
            out.append((f"shift-{t.hour:02d}{t.minute:02d}", t))
    if settings.EVENING_ENABLED:
        out.append(("evening", _clock(settings.EVENING_AT, "18:30")))
    return out
