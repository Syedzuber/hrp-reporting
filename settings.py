"""
settings.py — one source of configuration for both local and Railway.

Reads environment variables first, then falls back to the existing config.py
so nothing breaks while you are still running on Windows. Once the Railway
variables are set, config.py is no longer consulted and can stay off the repo.

NOTHING SECRET SHOULD EVER BE COMMITTED. config.py is in .gitignore.
"""

from __future__ import annotations

import os

try:                      # local Windows machine
    import config as _local
except Exception:         # Railway container — env vars only
    _local = None


def _get(name, default=None, cast=str):
    val = os.environ.get(name)
    if val in (None, ""):
        val = getattr(_local, name, None) if _local else None
    if val in (None, ""):
        return default
    if cast is bool:
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    if cast is int:
        return int(val)
    if cast is list:
        if isinstance(val, (list, tuple)):
            return list(val)
        return [p.strip() for p in str(val).replace(";", ",").split(",") if p.strip()]
    return cast(val)


# ── Zoho CRM ──────────────────────────────────────────────────────────
CLIENT_ID = _get("CLIENT_ID")
CLIENT_SECRET = _get("CLIENT_SECRET")
REFRESH_TOKEN = _get("REFRESH_TOKEN")
ZOHO_TOKEN_URL = _get("ZOHO_TOKEN_URL", "https://accounts.zoho.in/oauth/v2/token")
ZOHO_CRM_BASE = _get("ZOHO_CRM_BASE", "https://www.zohoapis.in/crm/v2")

# ── Company / delivery ────────────────────────────────────────────────
COMPANY_NAME = _get("COMPANY_NAME", "Horizon Renewable Power")
RECIPIENTS = _get("RECIPIENTS", [], list)
ALERT_TO = _get("ALERT_TO", [], list)

# ── Email transport ───────────────────────────────────────────────────
# Railway blocks outbound SMTP on Free and Hobby plans, so the HTTP API is the
# default. Set MAIL_TRANSPORT=smtp to force the old path when running locally.
MAIL_TRANSPORT = _get("MAIL_TRANSPORT", "api").lower()

SMTP_HOST = _get("SMTP_HOST", "smtp.zeptomail.in")
SMTP_PORT = _get("SMTP_PORT", 587, int)
SMTP_USER = _get("SMTP_USER", "emailapikey")
SMTP_PASSWORD = _get("SMTP_PASSWORD")
SMTP_FROM = _get("SMTP_FROM")

# The mail-agent send token. It is the same value ZeptoMail uses as the SMTP
# password, so it is reused unless a separate one is provided.
ZEPTO_TOKEN = _get("ZEPTO_TOKEN") or SMTP_PASSWORD
# India data centre accounts use api.zeptomail.in; the global endpoint is
# api.zeptomail.com. Your SMTP host is smtp.zeptomail.in, so .in is the default.
ZEPTO_API_URL = _get("ZEPTO_API_URL", "https://api.zeptomail.in/v1.1/email")

# ── Storage ───────────────────────────────────────────────────────────
# On Railway this points at the mounted volume so the database survives
# redeploys. Locally it stays beside the code.
DATA_DIR = _get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = _get("DB_PATH", os.path.join(DATA_DIR, "hrp_crm.db"))
REPORTS_DIR = _get("REPORTS_DIR", os.path.join(DATA_DIR, "reports"))

# ── Data window ───────────────────────────────────────────────────────
# How far back each sync pulls. The lead-status chart counts every lead since
# LADDER_SINCE, so SYNC_DAYS must reach at least that far or the early months
# are simply absent and the chart silently understates the backlog.
SYNC_DAYS = _get("SYNC_DAYS", 300, int)
LADDER_SINCE = _get("LADDER_SINCE", "2026-01-01")

# ── Schedules (IST). Change these to move a report's time. ────────────
MORNING_AT = _get("MORNING_AT", "08:00")
# The shift report runs once, at the end of the working day. It used to run at
# 11:00 too, but that edition covered 20:00 to 11:00 — mostly hours in which
# nobody works — so the morning slot is now the day-start report instead.
SHIFT_TIMES = _get("SHIFT_TIMES", ["20:00"], list)
# By default a shift window starts at the previous boundary, which with a single
# run time is 20:00 the day before: a 24-hour window. This overrides the start
# for a given run time, so "20:00=09:00" makes the day-end report cover the
# working day, 09:00 to 20:00.
SHIFT_WINDOW_STARTS = _get("SHIFT_WINDOW_STARTS", ["20:00=09:00"], list)

# The forward-looking report: what has to happen today. See hrp_report/daystart.py.
DAYSTART_AT = _get("DAYSTART_AT", "09:00")

EVENING_AT = _get("EVENING_AT", "18:30")   # legacy single-run report
HEALTHCHECK_AT = _get("HEALTHCHECK_AT", "21:00")
TIMEZONE = _get("TIMEZONE", "Asia/Kolkata")

# Off by default: superseded by the day-start report, and it has no stale-data
# banner (see hrp_report/daily_pdf_email.py). Off in code so that a fresh
# deploy without the variable cannot bring it back.
MORNING_ENABLED = _get("MORNING_ENABLED", False, bool)
SHIFT_ENABLED = _get("SHIFT_ENABLED", True, bool)
DAYSTART_ENABLED = _get("DAYSTART_ENABLED", True, bool)
EVENING_ENABLED = _get("EVENING_ENABLED", False, bool)   # superseded by SHIFT
HEALTHCHECK_ENABLED = _get("HEALTHCHECK_ENABLED", True, bool)


def summary() -> str:
    """Safe to log — never prints a secret."""
    def mask(v):
        return "set" if v else "MISSING"
    return (
        f"CRM base {ZOHO_CRM_BASE} | token {mask(REFRESH_TOKEN)} | "
        f"transport {MAIL_TRANSPORT} | from {SMTP_FROM or 'MISSING'} | "
        f"{len(RECIPIENTS)} recipients | alerts to {len(ALERT_TO) or 'derived'} | "
        f"db {DB_PATH} | sync {SYNC_DAYS}d | ladder since {LADDER_SINCE} | "
        f"morning {MORNING_AT} | day start {DAYSTART_AT} | shifts {','.join(SHIFT_TIMES)} | "
        f"window starts {','.join(SHIFT_WINDOW_STARTS) or 'default'} | "
        f"health {HEALTHCHECK_AT} | {TIMEZONE}"
    )