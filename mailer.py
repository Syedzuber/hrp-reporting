"""
mailer.py — one send path for every report.

Railway disables outbound SMTP on the Free and Hobby plans, so the default
transport is ZeptoMail's HTTPS API, which works on any plan. SMTP is kept as a
fallback so nothing changes while you are still running on Windows.

Usage:
    from mailer import send
    send(subject="...", text_body="...", html_body="...",
         to=["a@b.com"], attachment="/path/to/report.pdf")
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

import requests

import settings

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    pass


def send(subject: str, text_body: str, html_body: str,
         to: list, attachment: str | None = None,
         transport: str | None = None) -> bool:
    """Send one message. Returns True on success, raises MailError on failure."""
    to = [t for t in (to or []) if t]
    if not to:
        raise MailError("No recipients given.")
    if not settings.SMTP_FROM:
        raise MailError("SMTP_FROM is not configured.")

    mode = (transport or settings.MAIL_TRANSPORT or "api").lower()
    if mode == "api":
        return _send_api(subject, text_body, html_body, to, attachment)
    return _send_smtp(subject, text_body, html_body, to, attachment)


# ── ZeptoMail HTTPS API ───────────────────────────────────────────────

def _send_api(subject, text_body, html_body, to, attachment):
    if not settings.ZEPTO_TOKEN:
        raise MailError("ZEPTO_TOKEN (or SMTP_PASSWORD) is not set.")

    payload = {
        "from": {"address": settings.SMTP_FROM,
                 "name": f"{settings.COMPANY_NAME} Reports"},
        "to": [{"email_address": {"address": addr}} for addr in to],
        "subject": subject,
        "textbody": text_body,
        "htmlbody": html_body,
        "track_opens": False,
        "track_clicks": False,
    }

    if attachment and os.path.exists(attachment):
        mime, _ = mimetypes.guess_type(attachment)
        with open(attachment, "rb") as fh:
            payload["attachments"] = [{
                "name": os.path.basename(attachment),
                "mime_type": mime or "application/octet-stream",
                "content": base64.b64encode(fh.read()).decode("ascii"),
            }]

    token = settings.ZEPTO_TOKEN.strip()
    # ZeptoMail expects the scheme prefix; tolerate a token pasted with or
    # without it so a copy-paste slip is not a silent failure.
    if not token.lower().startswith("zoho-enczapikey"):
        token = f"Zoho-enczapikey {token}"

    try:
        r = requests.post(
            settings.ZEPTO_API_URL,
            headers={"Authorization": token,
                     "Accept": "application/json",
                     "Content-Type": "application/json"},
            json=payload, timeout=45)
    except Exception as exc:
        raise MailError(f"ZeptoMail API unreachable: {exc}") from exc

    if r.status_code in (200, 201, 202):
        log.info("Sent via ZeptoMail API to %s", to)
        return True

    raise MailError(
        f"ZeptoMail API returned HTTP {r.status_code}: {r.text[:400]}")


# ── SMTP fallback ─────────────────────────────────────────────────────

def _send_smtp(subject, text_body, html_body, to, attachment):
    domain = settings.SMTP_FROM.split("@")[-1].strip(">")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = formataddr((f"{settings.COMPANY_NAME} Reports", settings.SMTP_FROM))
    msg["To"] = ", ".join(to)
    msg["Reply-To"] = settings.SMTP_FROM
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["MIME-Version"] = "1.0"
    msg["Auto-Submitted"] = "auto-generated"

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    if attachment and os.path.exists(attachment):
        with open(attachment, "rb") as fh:
            part = MIMEApplication(fh.read(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment",
                        filename=os.path.basename(attachment))
        msg.attach(part)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=45) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            srv.sendmail(settings.SMTP_FROM, to, msg.as_string())
    except Exception as exc:
        raise MailError(f"SMTP send failed: {exc}") from exc

    log.info("Sent via SMTP to %s", to)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    print(settings.summary())
    target = settings.ALERT_TO or settings.RECIPIENTS[:1]
    send(subject=f"Test from {settings.COMPANY_NAME} reporting",
         text_body="Transport test. If you can read this, mail is working.",
         html_body="<p>Transport test. If you can read this, mail is working.</p>",
         to=target)
    print("Sent to", target)
