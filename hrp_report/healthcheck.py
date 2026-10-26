"""
hrp_report.healthcheck — watchdog for the CEO reports.

Runs as its own scheduled job inside scheduler.py. It used to read only
sync_log and the report PDFs, and stayed silent whenever it could not tell
what was going on — so for two months in which every report job failed, it
found nothing wrong and the status page showed it as "ok" every night. Every
check here now treats an absence of evidence as a failure, never as health.

It checks:

  1. the database — the records table exists and has rows, and the newest
     sync_log entry is recent. Row counts per module go into every alert.
  2. every scheduled report job, from the job_runs table that scheduler.py
     writes on every run (see jobruns.py). A job is a problem if its last run
     failed, if it started and never finished, or if it has not run since its
     most recent scheduled time. No runs recorded at all is a failure.
  3. the newest morning and evening report PDFs, as before.

Alerts go to ALERT_TO only, through mailer.py. There is deliberately no
fallback to RECIPIENTS, which is the CEO list: if ALERT_TO is empty, that is
itself reported as a problem and nothing is sent.

If everything is healthy it sends nothing at all, so it never becomes noise.

Run standalone:   python -m hrp_report.healthcheck          exits 1 on any problem
Force a test:     python -m hrp_report.healthcheck --test
"""

from __future__ import annotations

import glob
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from urllib.request import pathname2url

import pytz

import jobruns
import settings
from mailer import MailError, send as send_mail
from .metrics import GOOD_SYNC

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

DB_PATH = settings.DB_PATH
REPORTS = settings.REPORTS_DIR

# A daily job is late once it has missed a full cycle plus a couple of hours
# of slack for a machine that woke up slowly.
STALE_HOURS = 26

# A job is due once its clock time plus this has passed, so a check that
# happens to run at the same minute does not race the job it is checking.
START_GRACE = timedelta(minutes=15)

# A run still marked "running" after this long was killed or has hung. A full
# 300-day sync takes minutes, not hours.
MAX_RUNTIME = timedelta(hours=2)

# A WhatsApp recipient whose last this-many attempts all failed is alerted on.
# Two digests a day, so three failures is about a day and a half of silence.
WHATSAPP_FAILS_BEFORE_ALERT = 3

MORNING_GLOB = "HRP_Daily_Sales_Numbers_*.pdf"
EVENING_GLOB = "HRP_End_of_Day_*.pdf"


# ── helpers ───────────────────────────────────────────────────────────

def _open_readonly(path):
    """Open an existing database without ever creating one.

    sqlite3.connect() on a missing path quietly creates an empty file, which
    is how an empty database used to pass as present. Read-only mode refuses.
    """
    if not os.path.exists(path):
        return None
    uri = "file:" + pathname2url(os.path.abspath(path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _ist(value):
    if not value:
        return None
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        when = IST.localize(when)
    return when.astimezone(IST)


def database_state(db_path: str | None = None) -> dict:
    """What is actually in the snapshot. scheduler.py's status page uses this too."""
    path = db_path or DB_PATH
    state = {"path": path, "exists": False, "tables": [], "records": {},
             "total": 0, "has_records": False, "last_sync": None,
             "last_attempt": None, "error": None}
    try:
        conn = _open_readonly(path)
    except Exception as exc:
        state["error"] = str(exc)
        return state
    if conn is None:
        return state
    state["exists"] = True
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        state["tables"] = sorted(tables)
        if "records" in tables:
            state["records"] = dict(conn.execute(
                "SELECT module, COUNT(*) FROM records GROUP BY module ORDER BY module"))
            state["total"] = sum(state["records"].values())
            state["has_records"] = state["total"] > 0
        if "sync_log" in tables:
            # Freshness counts only syncs that completed and stored rows, the
            # same rule as the reports' stale banners (metrics.GOOD_SYNC).
            row = conn.execute(
                f"SELECT MAX(synced_at) FROM sync_log WHERE {GOOD_SYNC}").fetchone()
            state["last_sync"] = _ist(row[0]) if row else None
            row = conn.execute("SELECT MAX(synced_at) FROM sync_log").fetchone()
            state["last_attempt"] = _ist(row[0]) if row else None
    except Exception as exc:
        state["error"] = str(exc)
    finally:
        conn.close()
    return state


def latest_runs(db_path: str | None = None) -> dict:
    """{job: its most recent job_runs row}. Empty if nothing has been recorded."""
    conn = _open_readonly(db_path or DB_PATH)
    if conn is None:
        return {}
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='job_runs'").fetchone():
            return {}
        rows = conn.execute(
            "SELECT job, started_at, finished_at, status, error FROM job_runs "
            "WHERE id IN (SELECT MAX(id) FROM job_runs GROUP BY job)").fetchall()
    finally:
        conn.close()
    return {job: {"started_at": _ist(started), "finished_at": _ist(finished),
                  "status": status, "error": error}
            for job, started, finished, status, error in rows}


def _last_due(now, at):
    """The most recent time a daily job at clock time `at` should have started."""
    due = IST.localize(datetime.combine(now.date(), at))
    if due > now - START_GRACE:
        due -= timedelta(days=1)
    return due


def _newest(pattern):
    """(path, datetime, hours_old) of the newest matching report, or (None,)*3."""
    files = glob.glob(os.path.join(REPORTS, pattern))
    if not files:
        return None, None, None
    newest = max(files, key=os.path.getmtime)
    when = datetime.fromtimestamp(os.path.getmtime(newest), IST)
    return newest, when, (datetime.now(IST) - when).total_seconds() / 3600.0


def _when(d):
    return d.strftime("%a %d %b %Y %H:%M IST") if d else "never"


# ── the checks ────────────────────────────────────────────────────────

def _database_problems(db, now):
    problems = []
    if db["error"]:
        problems.append(f"Database: {db['path']} could not be read ({db['error']}).")
        return problems
    if not db["exists"]:
        problems.append(
            f"Database: {db['path']} does not exist. No sync has ever written to it, "
            f"so every report is built from nothing.")
        return problems
    if "records" not in db["tables"]:
        problems.append(
            f"Database: {db['path']} exists but has no records table. No sync has "
            f"ever completed against this file, so every report query fails.")
    elif not db["has_records"]:
        problems.append("Database: the records table exists but is empty. The sync "
                        "ran without storing anything.")

    if db["last_sync"] is None:
        problems.append("CRM sync: sync_log has no successful entry — none that "
                        "completed and stored rows — so there is no evidence a sync "
                        "has ever worked.")
    else:
        age = (now - db["last_sync"]).total_seconds() / 3600.0
        if age > STALE_HOURS:
            problems.append(
                f"CRM data is {age:.0f} hours old — last synced "
                f"{db['last_sync'].strftime('%a %d %b %Y at %H:%M IST')}. Every figure in "
                f"the reports is stale, and zeros will read as inactivity rather than as "
                f"missing data.")
    if db["last_attempt"] and (db["last_sync"] is None or db["last_attempt"] > db["last_sync"]):
        problems.append(
            f"CRM sync: the most recent attempt, {_when(db['last_attempt'])}, stored "
            f"nothing usable — no module completed with rows. The data is from "
            f"{_when(db['last_sync'])}.")
    return problems


def _whatsapp_problems(db_path=None):
    """The digest is secondary and never fails a job, which is exactly why it
    needs watching: nothing else would notice it had stopped arriving."""
    if not settings.WHATSAPP_ENABLED:
        return []
    if not settings.INTERAKT_API_KEY:
        return ["WhatsApp: WHATSAPP_ENABLED is on but INTERAKT_API_KEY is not set, so "
                "no digest is being sent. Copy the key into this service's variables."]
    if not settings.WHATSAPP_TO:
        return ["WhatsApp: WHATSAPP_ENABLED is on but WHATSAPP_TO is empty, so no "
                "digest is being sent."]
    conn = _open_readonly(db_path or DB_PATH)
    if conn is None:
        return []
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='whatsapp_log'").fetchone():
            return []
        rows = conn.execute(
            "SELECT recipient, status, sent_at, report, error FROM whatsapp_log "
            "WHERE status IN ('ok', 'failed') ORDER BY id DESC").fetchall()
    finally:
        conn.close()

    # Per recipient, so one wrong number cannot hide behind the others.
    recent = {}
    for recipient, status, sent_at, report, error in rows:
        recent.setdefault(recipient, [])
        if len(recent[recipient]) < WHATSAPP_FAILS_BEFORE_ALERT:
            recent[recipient].append((status, sent_at, report, error))
    problems = []
    for recipient, attempts in recent.items():
        if (len(attempts) == WHATSAPP_FAILS_BEFORE_ALERT
                and all(a[0] == "failed" for a in attempts)):
            _, sent_at, report, error = attempts[0]
            problems.append(
                f"WhatsApp: the last {WHATSAPP_FAILS_BEFORE_ALERT} digests to {recipient} "
                f"all failed, most recently the {report} digest at "
                f"{_when(_ist(sent_at))}: {error}. The emails are unaffected.")
    return problems


def _job_problems(scheduled, runs, now):
    problems = []
    for name, at in scheduled:
        label = f"Job {name} (daily {at:%H:%M})"
        run = runs.get(name)
        if run is None:
            # The bug this replaces: an empty record was read as "nothing wrong".
            problems.append(
                f"{label}: no runs recorded at all. There is no evidence it has "
                f"ever run, so it is treated as failed.")
            continue

        due = _last_due(now, at)
        started, status = run["started_at"], run["status"]
        error = f" Error: {run['error']}" if run["error"] else ""
        if started is None or started < due:
            problems.append(
                f"{label}: did not run at {_when(due)}. Its last recorded run started "
                f"{_when(started)} with status {status}.{error}")
        elif status == "failed":
            problems.append(f"{label}: last run FAILED, started {_when(started)}.{error}")
        elif status == "running":
            if now - started > MAX_RUNTIME:
                problems.append(
                    f"{label}: started {_when(started)} and never finished. The "
                    f"process was killed or the job hung part-way.")
        elif status != "ok":
            problems.append(f"{label}: last run has unrecognised status {status!r}.")
    return problems


def run_check(force: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST)
    db = database_state()
    runs = latest_runs()
    scheduled = jobruns.scheduled_jobs()
    m_path, m_at, m_age = _newest(MORNING_GLOB)
    e_path, e_at, e_age = _newest(EVENING_GLOB)
    recipients = list(settings.ALERT_TO or [])

    problems = []
    if not recipients:
        log.error("ALERT_TO is empty — the healthcheck has nowhere to send alerts.")
        problems.append(
            "ALERT_TO is not set, so this watchdog has nowhere to send alerts. It "
            "does not fall back to RECIPIENTS, the CEO list. Set ALERT_TO in Railway.")

    problems += _database_problems(db, now)
    problems += _job_problems(scheduled, runs, now)
    problems += _whatsapp_problems()

    if settings.MORNING_ENABLED:
        if m_at is None:
            problems.append(
                "Morning report: no PDF has ever been produced in the reports folder.")
        elif m_age > STALE_HOURS:
            problems.append(
                f"Morning report last produced {m_at.strftime('%a %d %b at %H:%M IST')} "
                f"({m_age:.0f} hours ago) — {os.path.basename(m_path)}.")

    # Only complain about the evening report once it has run at least once,
    # so this does not fire before that job is enabled.
    if e_at is not None and e_age > STALE_HOURS:
        problems.append(
            f"Evening report last produced {e_at.strftime('%a %d %b at %H:%M IST')} "
            f"({e_age:.0f} hours ago) — {os.path.basename(e_path)}.")

    state = {
        "problems": problems,
        "database": db,
        "runs": runs,
        "scheduled": scheduled,
        "snapshot_at": db["last_sync"],
        "morning_at": m_at,
        "evening_at": e_at,
        "checked_at": now,
        "healthy": not problems,
        # None: no alert was needed. True/False: one was attempted.
        "alert_sent": None,
    }

    if problems or force:
        state["alert_sent"] = _send_alert(state, recipients, force=force and not problems)
    else:
        log.info("Healthy — %d records, last sync %s, %d jobs on schedule. No alert sent.",
                 db["total"], _when(db["last_sync"]), len(scheduled))
    return state


def exit_code(state) -> int:
    """Non-zero when anything is wrong, including an alert that did not go out."""
    return 0 if state["healthy"] and state["alert_sent"] is not False else 1


def _send_alert(state, recipients, force=False) -> bool:
    problems = state["problems"]
    if not recipients:
        log.error("Healthcheck alert NOT sent: ALERT_TO is empty (%d problem(s)).",
                  len(problems))
        return False

    db = state["database"]
    subject = ("[TEST] CEO reports healthcheck" if force else
               f"ACTION NEEDED - CEO reports have stopped ({len(problems)} issue"
               f"{'s' if len(problems) != 1 else ''})")

    if db["records"]:
        counts = ", ".join(f"{m} {n:,}" for m, n in db["records"].items())
        records = f"{db['total']:,} ({counts})"
    else:
        records = "none"

    lines = [
        "CEO REPORT HEALTHCHECK",
        "",
        ("This is a test run — the checks below all passed." if force else
         "One or more of the CEO reports is not running correctly."),
        "",
        "STATUS",
        f"  Database               : {db['path']}",
        f"  Records stored         : {records}",
        f"  CRM data last synced   : {_when(state['snapshot_at'])}",
        f"  Morning report PDF     : {_when(state['morning_at'])}",
        f"  Evening report PDF     : {_when(state['evening_at'])}",
        "",
        "JOBS (most recent recorded run)",
    ]
    for name, at in state["scheduled"]:
        run = state["runs"].get(name)
        if run is None:
            lines.append(f"  {name:<12} {at:%H:%M}  no runs recorded")
        else:
            lines.append(f"  {name:<12} {at:%H:%M}  {run['status']:<8} "
                         f"{_when(run['started_at'])}")
    lines.append("")

    if problems:
        lines += ["WHAT IS WRONG"]
        lines += [f"  {i}. {p}" for i, p in enumerate(problems, 1)]
        lines += [
            "",
            "FIRST THINGS TO CHECK",
            "  Railway > this service > Deployments > View logs, around the times above.",
            "  The service URL shows last_runs, db_has_records and last_sync.",
            "",
        ]
    lines += [
        f"Checked at {_when(state['checked_at'])}.",
        "Sent only to ALERT_TO, never to the CEO distribution list.",
    ]
    body = "\n".join(lines)

    html = ("<html><body style=\"font-family:Consolas,Menlo,monospace;font-size:13px;"
            "white-space:pre-wrap;color:#16191d;\">" +
            body.replace("&", "&amp;").replace("<", "&lt;") +
            "</body></html>")
    try:
        send_mail(subject=subject, text_body=body, html_body=html, to=recipients)
        log.warning("Healthcheck alert sent to %s (%d problem(s))", recipients, len(problems))
        return True
    except MailError as exc:
        log.error("Healthcheck alert FAILED to send: %s", exc)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_check(force="--test" in sys.argv)
    for p in result["problems"]:
        print("PROBLEM:", p)
    if result["healthy"]:
        print("Healthy — nothing sent.")
    if result["alert_sent"] is False:
        print("ALERT NOT SENT.")
    sys.exit(exit_code(result))
