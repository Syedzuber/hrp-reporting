"""
scheduler.py — the Railway entrypoint.

One always-on process holding every job, replacing Windows Task Scheduler.
Times come from settings (environment variables on Railway), so changing a
report's time is a variable change and a restart, not a code edit.

    Morning CEO Report    MORNING_AT      default 08:00 IST  (superseded, off by default)
    Day Start Report      DAYSTART_AT     default 09:00 IST
    Shift Report          SHIFT_TIMES     default 20:00 IST
    End of Day Report     EVENING_AT      default 18:30 IST  (legacy, off by default)
    Report Healthcheck    HEALTHCHECK_AT  default 21:00 IST

A job added here must also be added to jobruns.scheduled_jobs(), or the
healthcheck will not watch it.

Also serves a tiny HTTP endpoint on $PORT so Railway sees a live service and
you can check status from a browser.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

import jobruns
import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("scheduler")
TZ = pytz.timezone(settings.TIMEZONE)

LAST_RUN = {}


def _hhmm(value, fallback="08:00"):
    try:
        h, m = str(value).split(":")[:2]
        return int(h), int(m)
    except Exception:
        log.error("Bad time %r — using %s", value, fallback)
        h, m = fallback.split(":")
        return int(h), int(m)


def _wrap(name, fn):
    """Run a job, record the outcome, never let an exception kill the scheduler.

    LAST_RUN feeds the status page only. It is lost on every restart, so the
    outcome is also written to the job_runs table, which is what the
    healthcheck reads.
    """
    def runner():
        started = datetime.now(TZ)
        run_id = jobruns.start(name, started)
        log.info("=== %s starting ===", name)
        try:
            fn()
            LAST_RUN[name] = {"at": started.isoformat(), "status": "ok"}
            jobruns.finish(run_id, name, started, datetime.now(TZ), "ok")
            log.info("=== %s ok ===", name)
        except Exception as exc:
            LAST_RUN[name] = {"at": started.isoformat(), "status": "failed",
                              "error": str(exc)[:400]}
            jobruns.finish(run_id, name, started, datetime.now(TZ), "failed",
                           str(exc)[:400])
            log.exception("=== %s FAILED: %s ===", name, exc)
    return runner


# ── jobs ──────────────────────────────────────────────────────────────

# The report functions catch a mail failure, log it and return None rather
# than raising. Without the checks below, a report that never reached anyone
# would be recorded as "ok". A failed sync is different: the report still goes
# out from the previous snapshot, and the function raises SyncFailed after
# sending, which _wrap records as a failed run.

def job_morning():
    from hrp_report.daily_pdf_email import send_daily_pdf
    # send_daily_pdf syncs first. This job used to sync as well, so every
    # morning pulled the data twice — and an auth failure here would now stop
    # the report before it was sent.
    if not send_daily_pdf():
        raise RuntimeError("Morning report was not emailed — see the mail error above.")


def job_evening():
    from hrp_report.evening import send_evening_report
    if not send_evening_report():  # syncs internally
        raise RuntimeError("Evening report was not emailed — see the mail error above.")


def job_shift():
    from hrp_report.shift import send_shift_report
    if not send_shift_report():    # syncs internally
        raise RuntimeError("Shift report was not emailed — see the mail error above.")


def job_daystart():
    from hrp_report.daystart import send_daystart_report
    if not send_daystart_report():  # syncs internally
        raise RuntimeError("Day-start report was not emailed — see the mail error above.")


def job_healthcheck():
    from hrp_report.healthcheck import run_check
    state = run_check()
    # A check that found problems is recorded as failed, so the status page
    # cannot say "ok" while the reports are down.
    if not state["healthy"]:
        sent = "sent" if state["alert_sent"] else "NOT sent"
        raise RuntimeError(f"{len(state['problems'])} problem(s) found, alert {sent}. "
                           f"First: {state['problems'][0]}")


# ── status endpoint ───────────────────────────────────────────────────

def status() -> dict:
    from hrp_report.healthcheck import database_state
    # This used to report db_exists, which only checked that the file was
    # there. SQLite creates an empty file on connect, so it was true for a
    # database with no tables. These say what is actually in it.
    db = database_state()
    return {
        "service": "hrp-reporting",
        "now": datetime.now(TZ).isoformat(),
        "timezone": settings.TIMEZONE,
        "schedule": {
            "morning": settings.MORNING_AT if settings.MORNING_ENABLED else "disabled",
            "daystart": settings.DAYSTART_AT if settings.DAYSTART_ENABLED else "disabled",
            "shifts": settings.SHIFT_TIMES if settings.SHIFT_ENABLED else "disabled",
            "evening_legacy": settings.EVENING_AT if settings.EVENING_ENABLED else "disabled",
            "healthcheck": settings.HEALTHCHECK_AT if settings.HEALTHCHECK_ENABLED else "disabled",
        },
        "last_runs": LAST_RUN,
        "db_path": settings.DB_PATH,
        "db_has_records": db["has_records"],
        "records_by_module": db["records"],
        "last_sync": db["last_sync"].isoformat() if db["last_sync"] else None,
        "db_error": db["error"],
    }


class Status(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(status(), indent=1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):      # keep Railway logs readable
        pass


def serve_status():
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), Status).serve_forever()


# ── main ──────────────────────────────────────────────────────────────

def main():
    os.makedirs(settings.REPORTS_DIR, exist_ok=True)
    log.info("Config: %s", settings.summary())

    sched = BackgroundScheduler(timezone=TZ)

    if settings.MORNING_ENABLED:
        h, m = _hhmm(settings.MORNING_AT, "08:00")
        sched.add_job(_wrap("morning", job_morning), "cron", hour=h, minute=m,
                      id="morning", misfire_grace_time=3600, coalesce=True)
        log.info("Morning report scheduled %02d:%02d %s", h, m, settings.TIMEZONE)

    if settings.DAYSTART_ENABLED:
        h, m = _hhmm(settings.DAYSTART_AT, "09:00")
        sched.add_job(_wrap("daystart", job_daystart), "cron", hour=h, minute=m,
                      id="daystart", misfire_grace_time=3600, coalesce=True)
        log.info("Day-start report scheduled %02d:%02d %s", h, m, settings.TIMEZONE)

    if settings.SHIFT_ENABLED:
        for raw in settings.SHIFT_TIMES:
            h, m = _hhmm(raw, "20:00")
            sched.add_job(_wrap(f"shift-{h:02d}{m:02d}", job_shift), "cron",
                          hour=h, minute=m, id=f"shift-{h:02d}{m:02d}",
                          misfire_grace_time=3600, coalesce=True)
            log.info("Shift report scheduled %02d:%02d %s", h, m, settings.TIMEZONE)

    if settings.EVENING_ENABLED:
        h, m = _hhmm(settings.EVENING_AT, "18:30")
        sched.add_job(_wrap("evening", job_evening), "cron", hour=h, minute=m,
                      id="evening", misfire_grace_time=3600, coalesce=True)
        log.info("Legacy evening report scheduled %02d:%02d %s", h, m, settings.TIMEZONE)

    if settings.HEALTHCHECK_ENABLED:
        h, m = _hhmm(settings.HEALTHCHECK_AT, "21:00")
        sched.add_job(_wrap("healthcheck", job_healthcheck), "cron", hour=h, minute=m,
                      id="healthcheck", misfire_grace_time=3600, coalesce=True)
        log.info("Healthcheck scheduled %02d:%02d %s", h, m, settings.TIMEZONE)

    sched.start()

    # Run any job immediately on boot by setting
    # RUN_ON_BOOT=morning|daystart|shift|evening|healthcheck
    boot = (os.environ.get("RUN_ON_BOOT") or "").strip().lower()
    if boot in ("morning", "daystart", "evening", "healthcheck", "shift"):
        log.info("RUN_ON_BOOT=%s — running once now.", boot)
        threading.Thread(
            target=_wrap(boot, {"morning": job_morning,
                                "daystart": job_daystart,
                                "evening": job_evening,
                                "shift": job_shift,
                                "healthcheck": job_healthcheck}[boot]),
            daemon=True).start()

    serve_status()      # blocks, keeping the container alive


if __name__ == "__main__":
    main()
