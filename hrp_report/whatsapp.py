"""
hrp_report.whatsapp — a WhatsApp digest of a report, via Interakt.

Email is the system of record. This is a secondary digest that follows it:
it is sent only after the email went out, and a failure here never fails the
job. Because a digest that quietly stops arriving is exactly how the original
outage went unnoticed, every attempt is written to a whatsapp_log table and
the healthcheck alerts when a recipient's recent attempts have all failed.

The Interakt account is shared with another system (the PMS, in a separate
Railway project). Two consequences:

  * Template codenames are global to the WhatsApp number, so ours are
    prefixed (crm_) and versioned (_v1).
  * INTERAKT_API_KEY is copied into this service's variables by hand; Railway
    references do not work across projects.

TEMPLATES. An approved WhatsApp template cannot be edited. Changing the
wording means submitting a new template under a new codename and pointing the
WHATSAPP_TEMPLATE_* variable at it — a variable change, not a deploy. The text
below is what each template must say, word for word, to be submitted for
approval. It is kept here so the number of {{n}} variables can be checked
against the values before anything is sent: Meta rejects a mismatch.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime

import pytz
import requests

import settings

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

API_URL = "https://api.interakt.ai/v1/public/message/"

# Submit these in Interakt as UTILITY templates, language English (en).
TEMPLATES = {
    "daystart": (
        "HRP Day Start, {{1}}\n"
        "Follow-ups due today: {{2}}\n"
        "Overdue this week: {{3}}\n"
        "Aged backlog (over a week): {{4}}, of which {{5}} have no owner\n"
        "Follow-up dates current: {{6}}\n"
        "The full report is in your email."
    ),
    "shift": (
        "HRP Shift Report, {{1}}\n"
        "Calls made: {{2}}\n"
        "Proposals sent: {{3}}\n"
        "Deals converted: {{4}}\n"
        "New leads: {{5}}\n"
        "Leads still waiting: {{6}}\n"
        "The full report is in your email."
    ),
}


class WhatsAppError(RuntimeError):
    pass


def template_name(kind: str) -> str:
    return {"daystart": settings.WHATSAPP_TEMPLATE_DAYSTART,
            "shift": settings.WHATSAPP_TEMPLATE_SHIFT}[kind]


def variable_count(kind: str) -> int:
    return len(set(re.findall(r"\{\{(\d+)\}\}", TEMPLATES[kind])))


def check_values(kind: str, values: list) -> None:
    """Raise unless `values` fills the template exactly. Called before every send,
    and by the tests, so a mismatch fails in testing rather than at 09:00."""
    want = variable_count(kind)
    if len(values) != want:
        raise WhatsAppError(f"{kind} template has {want} variables but {len(values)} "
                            f"values were given — Meta would reject this message.")
    blank = [i for i, v in enumerate(values, 1) if not str(v).strip()]
    if blank:
        raise WhatsAppError(f"{kind} template variable(s) {blank} are blank — Meta "
                            f"rejects empty variables.")


def normalise_number(raw: str) -> str:
    """10-digit Indian mobile, no country code, no leading 0. Raises if not one."""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise WhatsAppError(f"{mask(raw)} is not a 10-digit Indian mobile number")
    return digits


def mask(number) -> str:
    """Phone numbers are logged by their last four digits only."""
    digits = re.sub(r"\D", "", str(number))
    return f"******{digits[-4:]}" if len(digits) >= 4 else "****"


def _auth_header() -> str:
    key = (settings.INTERAKT_API_KEY or "").strip()
    # The key Interakt shows is already base64. Tolerate it pasted with the scheme.
    if key.lower().startswith("basic "):
        key = key[6:].strip()
    return f"Basic {key}"


def build_payload(kind: str, number: str, values: list) -> dict:
    check_values(kind, values)
    return {
        "countryCode": "+91",
        "phoneNumber": normalise_number(number),
        "type": "Template",
        "template": {
            "name": template_name(kind),
            "languageCode": "en",
            "bodyValues": [str(v) for v in values],
        },
    }


def _is_false(value) -> bool:
    return value is False or str(value).strip().lower() == "false"


def _failure_marker(body) -> str | None:
    """The explicit failure marker in a response body, or None if there is none."""
    if not isinstance(body, dict):
        return None
    for key in ("result", "success"):
        if key in body and _is_false(body[key]):
            return f'"{key}" is false'
    for key in ("error", "errors"):
        if body.get(key):
            return f'"{key}" is set'
    return None


def _message_id(body) -> str | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for value in (body.get("id"), body.get("messageId"), body.get("message_id"),
                  data.get("id"), data.get("messageId")):
        if value:
            return str(value)
    return None


def post(payload: dict) -> str:
    """Send one message. Returns Interakt's message id; raises WhatsAppError.

    Interakt's documentation says only that a successful call returns a message
    id; the full response shape is unconfirmed. So the rule is deliberately
    lenient:

      * FAILURE if the status is not 2xx;
      * FAILURE if the body carries an explicit failure marker — "result" or
        "success" false, or a non-empty "error" or "errors" (Interakt can
        answer 200 with an error);
      * SUCCESS otherwise, with the message id logged when one can be found.

    An unrecognised 2xx therefore counts as sent. A false failure raises a
    false alarm; a missed one is noticed by the recipient getting nothing.
    Tighten this once WHATSAPP_LOG_RESPONSES has shown the real shape.
    """
    try:
        r = requests.post(API_URL, json=payload, timeout=20,
                          headers={"Authorization": _auth_header(),
                                   "Content-Type": "application/json"})
    except Exception as exc:
        raise WhatsAppError(f"Interakt unreachable: {exc}") from exc

    if settings.WHATSAPP_LOG_RESPONSES:
        # The full raw body, to learn the real format. It may contain the
        # recipient's number, so turn this off once the format is confirmed.
        log.info("Interakt raw response (HTTP %s): %s", r.status_code, r.text)

    try:
        body = r.json()
    except ValueError:
        body = None
    if not (200 <= r.status_code < 300):
        raise WhatsAppError(f"Interakt HTTP {r.status_code}: {r.text[:400]}")
    marker = _failure_marker(body)
    if marker:
        raise WhatsAppError(f"Interakt HTTP {r.status_code} but {marker}: {r.text[:400]}")
    return _message_id(body) or "(no id returned)"


# ── the log the healthcheck reads ─────────────────────────────────────

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS whatsapp_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        sent_at    TEXT NOT NULL,
        report     TEXT NOT NULL,
        template   TEXT NOT NULL,
        recipient  TEXT NOT NULL,      -- masked: last four digits only
        status     TEXT NOT NULL,      -- ok | failed | skipped_stale
        message_id TEXT,
        error      TEXT
    )
"""


def _record(report, template, recipient, status, message_id=None, error=None):
    try:
        conn = sqlite3.connect(settings.DB_PATH, timeout=30)
        try:
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT INTO whatsapp_log (sent_at, report, template, recipient, status, "
                "message_id, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(IST).isoformat(), report, template, mask(recipient), status,
                 message_id, (error or "")[:400] or None))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("Could not record the WhatsApp attempt in whatsapp_log")


# ── the one entry point the reports call ──────────────────────────────

def send_digest(kind: str, values: list, stale: bool = False) -> None:
    """Send the digest to every WHATSAPP_TO number. Never raises.

    Call it only after the email has gone out. A failure is logged at ERROR and
    recorded for the healthcheck, but does not fail the job: email is the
    record, this is a convenience.
    """
    if not settings.WHATSAPP_ENABLED or not settings.INTERAKT_API_KEY:
        log.info("WhatsApp %s digest not sent: %s.", kind,
                 "WHATSAPP_ENABLED is off" if not settings.WHATSAPP_ENABLED
                 else "INTERAKT_API_KEY is not set")
        return

    template = template_name(kind)
    recipients = list(settings.WHATSAPP_TO or [])
    if stale:
        # A template cannot carry the stale-data banner, so the digest would show
        # old figures as current. The email carries the warning; this stays quiet.
        log.warning("WhatsApp %s digest not sent: the report's data is stale, and the "
                    "message could not say so. The email carries the warning.", kind)
        for number in recipients:
            _record(kind, template, number, "skipped_stale")
        return
    if not recipients:
        log.error("WhatsApp %s digest not sent: WHATSAPP_TO is empty.", kind)
        return

    for number in recipients:
        try:
            message_id = post(build_payload(kind, number, values))
            log.info("WhatsApp %s digest sent to %s via %s, message id %s",
                     kind, mask(number), template, message_id)
            _record(kind, template, number, "ok", message_id=message_id)
        except Exception as exc:
            log.error("WhatsApp %s digest to %s FAILED: %s", kind, mask(number), exc)
            _record(kind, template, number, "failed", error=str(exc))
