"""
scheduler.py — the Railway entrypoint.

One always-on process holding all three jobs, replacing Windows Task Scheduler.
Times come from settings (environment variables on Railway), so changing a
report's time is a variable change and a restart, not a code edit.

    Morning CEO Report    MORNING_AT      default 08:00 IST
    End of Day Report     EVENING_AT      default 18:30 IST
    Report Healthcheck    HEALTHCHECK_AT  default 21:00 IST

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
    """Run a job, record the outcome, never let an exception kill the scheduler."""
    def runner():
        started = datetime.now(TZ)
        log.info("=== %s starting ===", name)
        try:
            fn()
            LAST_RUN[name] = {"at": started.isoformat(), "status": "ok"}
            log.info("=== %s ok ===", name)
        except Exception as exc:
            LAST_RUN[name] = {"at": started.isoformat(), "status": "failed",
                              "error": str(exc)[:400]}
            log.exception("=== %s FAILED: %s ===", name, exc)
    return runner


# ── jobs ──────────────────────────────────────────────────────────────

def job_morning():
    import crm_sync
    from hrp_report.daily_pdf_email import send_daily_pdf
    crm_sync.sync()
    send_daily_pdf()


def job_evening():
    from hrp_report.evening import send_evening_report
    send_evening_report()          # syncs internally


def job_shift():
    from hrp_report.shift import send_shift_report
    send_shift_report()            # syncs internally


def job_healthcheck():
    from hrp_report.healthcheck import run_check
    run_check()


# ── status endpoint ───────────────────────────────────────────────────

class Status(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "service": "hrp-reporting",
            "now": datetime.now(TZ).isoformat(),
            "timezone": settings.TIMEZONE,
            "schedule": {
                "morning": settings.MORNING_AT if settings.MORNING_ENABLED else "disabled",
                "shifts": settings.SHIFT_TIMES if settings.SHIFT_ENABLED else "disabled",
                "evening_legacy": settings.EVENING_AT if settings.EVENING_ENABLED else "disabled",
                "healthcheck": settings.HEALTHCHECK_AT if settings.HEALTHCHECK_ENABLED else "disabled",
            },
            "last_runs": LAST_RUN,
            "db_path": settings.DB_PATH,
            "db_exists": os.path.exists(settings.DB_PATH),
        }, indent=1).encode()
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

    # Run any job immediately on boot by setting RUN_ON_BOOT=morning|evening|healthcheck
    boot = (os.environ.get("RUN_ON_BOOT") or "").strip().lower()
    if boot in ("morning", "evening", "healthcheck", "shift"):
        log.info("RUN_ON_BOOT=%s — running once now.", boot)
        threading.Thread(
            target=_wrap(boot, {"morning": job_morning,
                                "evening": job_evening,
                                "shift": job_shift,
                                "healthcheck": job_healthcheck}[boot]),
            daemon=True).start()

    serve_status()      # blocks, keeping the container alive


if __name__ == "__main__":
    main()
