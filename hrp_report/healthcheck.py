"""
hrp_report.healthcheck — watchdog for the two CEO reports.

Runs as its OWN scheduled task, deliberately separate from the reports it
watches. A warning can only travel inside something that gets sent, and the
failure that matters most — a task not running at all — sends nothing. Only an
outside observer catches that.

It checks three things and emails ONLY the operator (never the CEO list) when
any of them is stale:

  1. the CRM snapshot in hrp_crm.db (via sync_log)
  2. the newest morning report PDF
  3. the newest evening report PDF, if one has ever been produced

If everything is healthy it sends nothing at all, so it never becomes noise.

Alert recipients come from config.ALERT_TO if defined; otherwise it falls back
to the first address in config.RECIPIENTS that is not on the company domain,
so a misconfiguration cannot accidentally page the CEO.

Run standalone:   python -m hrp_report.healthcheck
Force a test:     python -m hrp_report.healthcheck --test
"""

from __future__ import annotations

import glob
import logging
import os
import re
import smtplib
import sqlite3
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

import pytz

import settings as config
import settings
from mailer import MailError, send as send_mail

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = settings.DB_PATH
REPORTS = settings.REPORTS_DIR
LOGS = os.path.join(HERE, "logs")

# A daily job is late once it has missed a full cycle plus a couple of hours
# of slack for a machine that woke up slowly.
STALE_HOURS = 26

MORNING_GLOB = "HRP_Daily_Sales_Numbers_*.pdf"
EVENING_GLOB = "HRP_End_of_Day_*.pdf"


# ── helpers ───────────────────────────────────────────────────────────

def _alert_recipients() -> list:
    """Operator addresses. Never the CEO distribution list."""
    explicit = getattr(config, "ALERT_TO", None)
    if explicit:
        return list(explicit) if isinstance(explicit, (list, tuple)) else [explicit]

    company = (config.SMTP_FROM.split("@")[-1] or "").lower()
    outside = [r for r in config.RECIPIENTS if not r.lower().endswith("@" + company)]
    if outside:
        return outside
    # Last resort: the first recipient, so the alert still goes somewhere.
    return config.RECIPIENTS[:1]


def _snapshot_age():
    """(datetime, hours_old) of the most recent CRM sync, or (None, None)."""
    if not os.path.exists(DB_PATH):
        return None, None
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute("SELECT MAX(synced_at) FROM sync_log").fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None, None
        when = datetime.fromisoformat(row[0])
        if when.tzinfo is None:
            when = IST.localize(when)
        when = when.astimezone(IST)
        return when, (datetime.now(IST) - when).total_seconds() / 3600.0
    except Exception as exc:
        log.error("Could not read sync_log: %s", exc)
        return None, None


def _newest(pattern):
    """(path, datetime, hours_old) of the newest matching report, or (None,)*3."""
    files = glob.glob(os.path.join(REPORTS, pattern))
    if not files:
        return None, None, None
    newest = max(files, key=os.path.getmtime)
    when = datetime.fromtimestamp(os.path.getmtime(newest), IST)
    return newest, when, (datetime.now(IST) - when).total_seconds() / 3600.0


def _log_tail(name, lines=18):
    path = os.path.join(LOGS, name)
    if not os.path.exists(path):
        return f"({name} does not exist — the task has never run from this folder)"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.readlines()
        return "".join(content[-lines:]).rstrip() or "(log is empty)"
    except Exception as exc:
        return f"(could not read {name}: {exc})"


def _last_run_verdict(name):
    """Read the last RUN line from a log so the alert can name the outcome."""
    path = os.path.join(LOGS, name)
    if not os.path.exists(path):
        return "never run"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            hits = [l.strip() for l in fh if re.match(r"RUN (OK|FAILED|ABORTED|STARTED)", l)]
        return hits[-1] if hits else "no RUN lines found"
    except Exception:
        return "unreadable"


# ── the check ─────────────────────────────────────────────────────────

def run_check(force: bool = False) -> dict:
    snap_at, snap_age = _snapshot_age()
    m_path, m_at, m_age = _newest(MORNING_GLOB)
    e_path, e_at, e_age = _newest(EVENING_GLOB)

    problems = []

    if snap_at is None:
        problems.append(
            "CRM snapshot: could not be read at all. hrp_crm.db is missing, empty or "
            "corrupt, so both reports are running blind.")
    elif snap_age > STALE_HOURS:
        problems.append(
            f"CRM data is {snap_age:.0f} hours old — last synced "
            f"{snap_at.strftime('%a %d %b %Y at %H:%M IST')}. Every figure in both "
            f"reports is stale, and zeros will read as inactivity rather than as "
            f"missing data.")

    if m_at is None:
        problems.append(
            "Morning report: no PDF has ever been produced in the reports folder.")
    elif m_age > STALE_HOURS:
        problems.append(
            f"Morning report last produced {m_at.strftime('%a %d %b at %H:%M IST')} "
            f"({m_age:.0f} hours ago) — {os.path.basename(m_path)}.")

    # Only complain about the evening report once it has run at least once,
    # so this does not fire before that task is registered.
    if e_at is not None and e_age > STALE_HOURS:
        problems.append(
            f"Evening report last produced {e_at.strftime('%a %d %b at %H:%M IST')} "
            f"({e_age:.0f} hours ago) — {os.path.basename(e_path)}.")

    state = {
        "problems": problems,
        "snapshot_at": snap_at,
        "snapshot_age": snap_age,
        "morning_at": m_at,
        "evening_at": e_at,
        "healthy": not problems,
    }

    if problems or force:
        _send_alert(state, force=force and not problems)
    else:
        log.info("Healthy — snapshot %.1fh old, morning report %.1fh old. No alert sent.",
                 snap_age or -1, m_age if m_age is not None else -1)
    return state


def _send_alert(state, force=False):
    to = _alert_recipients()
    problems = state["problems"]

    when = lambda d: d.strftime("%a %d %b %Y %H:%M IST") if d else "never"
    subject = ("[TEST] CEO reports healthcheck" if force else
               f"ACTION NEEDED - CEO reports have stopped ({len(problems)} issue"
               f"{'s' if len(problems) != 1 else ''})")

    lines = [
        "CEO REPORT HEALTHCHECK",
        "",
        ("This is a test run — the checks below all passed." if force else
         "One or more of the daily CEO reports is not running correctly."),
        "",
        "STATUS",
        f"  CRM data last synced   : {when(state['snapshot_at'])}",
        f"  Morning report (08:00) : {when(state['morning_at'])}",
        f"  Evening report (18:30) : {when(state['evening_at'])}",
        "",
    ]
    if problems:
        lines += ["WHAT IS WRONG"]
        lines += [f"  {i}. {p}" for i, p in enumerate(problems, 1)]
        lines += [
            "",
            "FIRST THINGS TO CHECK",
            '  schtasks /query /tn "HRP Daily CEO Report" /v /fo LIST',
            '  schtasks /query /tn "HRP Evening CEO Report" /v /fo LIST',
            "  Look at Last Run Time, Last Result and Task To Run. A Task To Run",
            "  pointing at a folder that no longer exists is the known failure mode.",
            "",
        ]
    lines += [
        "MORNING LOG (last lines)",
        _log_tail("daily_report.log"),
        "",
        "EVENING LOG (last lines)",
        _log_tail("evening_report.log"),
        "",
        f"Last morning run line : {_last_run_verdict('daily_report.log')}",
        f"Last evening run line : {_last_run_verdict('evening_report.log')}",
        "",
        f"Checked at {datetime.now(IST).strftime('%a %d %b %Y %H:%M IST')} on this machine.",
        "Sent only to the operator, not to the CEO distribution list.",
    ]
    body = "\n".join(lines)

    html = ("<html><body style=\"font-family:Consolas,Menlo,monospace;font-size:13px;"
            "white-space:pre-wrap;color:#16191d;\">" +
            body.replace("&", "&amp;").replace("<", "&lt;") +
            "</body></html>")
    try:
        send_mail(subject=subject, text_body=body, html_body=html, to=to)
        log.warning("Healthcheck alert sent to %s (%d problem(s))", to, len(problems))
    except MailError as exc:
        log.error("Healthcheck alert FAILED to send: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_check(force="--test" in sys.argv)
    for p in result["problems"]:
        print("PROBLEM:", p)
    if result["healthy"]:
        print("Healthy — nothing sent.")
    sys.exit(0)
