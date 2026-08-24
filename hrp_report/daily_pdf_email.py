"""
hrp_report.daily_pdf_email — build the PDF and email it at 08:00 IST.

Run standalone:      python -m hrp_report.daily_pdf_email
Or from app.py:      from hrp_report.daily_pdf_email import send_daily_pdf

Before building it refreshes the local snapshot with crm_sync so the numbers are
current. If the sync fails the report is still produced from the last good
snapshot, and the PDF says plainly how old that snapshot is.
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

import pytz

import settings as config
import settings
from mailer import MailError, send as send_mail
from . import metrics, pdf_report

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# On Railway these point at the mounted volume so data survives redeploys.
DB_PATH = settings.DB_PATH
OUT_DIR = settings.REPORTS_DIR


def refresh_snapshot() -> bool:
    """Pull the latest records into hrp_crm.db. Non-fatal on failure."""
    try:
        import crm_sync
        for fn in ("sync_all", "run_sync", "main", "sync"):
            if hasattr(crm_sync, fn):
                getattr(crm_sync, fn)()
                log.info("Snapshot refreshed via crm_sync.%s()", fn)
                return True
        log.warning("crm_sync has no recognised entry point; using existing snapshot.")
    except Exception as exc:
        log.error("Snapshot refresh failed (%s) — using existing snapshot.", exc)
    return False


def _body_html(rep: dict) -> str:
    d1, d2, d3 = rep["days"]
    p = rep["pipeline"]

    def row(label, a, b, c):
        return (
            f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;">{label}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;text-align:right;font-weight:700;">{a}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;text-align:right;color:#666;">{b}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;text-align:right;color:#666;">{c}</td></tr>'
        )

    def hdr(d):
        return f"{d['date'].strftime('%a %d %b')}"

    def f(v, suffix=""):
        return "n/a" if v is None else f"{v:,.1f}{suffix}" if isinstance(v, float) else f"{v:,}{suffix}"

    return f"""<!DOCTYPE html><html><body style="margin:0;padding:20px;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e2e5ea;">
  <div style="background:#16324f;padding:22px 26px;">
    <h1 style="color:#fff;margin:0;font-size:19px;">Daily Sales Numbers</h1>
    <p style="color:rgba(255,255,255,.75);margin:5px 0 0;font-size:13px;">
      {config.COMPANY_NAME} &middot; {d1['date'].strftime('%A %d %B %Y')} and the two days before
    </p>
  </div>
  <div style="padding:18px 22px;">
    <p style="font-size:13px;color:#444;margin:0 0 14px;">
      Full numbers, with change against each of the previous two days, are in the attached PDF.
      Headlines below.
    </p>
    <table style="width:100%;border-collapse:collapse;">
      <tr>
        <th style="text-align:left;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">Metric</th>
        <th style="text-align:right;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">{hdr(d1)}</th>
        <th style="text-align:right;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">{hdr(d2)}</th>
        <th style="text-align:right;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">{hdr(d3)}</th>
      </tr>
      {row('New leads', f(d1['new_leads']), f(d2['new_leads']), f(d3['new_leads']))}
      {row('Calls made', f(d1['calls']), f(d2['calls']), f(d3['calls']))}
      {row('Same-day contact rate', f(d1['same_day_contact_pct'],'%'), f(d2['same_day_contact_pct'],'%'), f(d3['same_day_contact_pct'],'%'))}
      {row('New deals', f(d1['new_deals']), f(d2['new_deals']), f(d3['new_deals']))}
      {row('Deals won', f(d1['deals_won']), f(d2['deals_won']), f(d3['deals_won']))}
    </table>
    <p style="font-size:12px;color:#666;margin:14px 0 0;">
      Open pipeline: <b>{p['open_deals']:,}</b> deals &middot;
      <b>{p['stalled_14d']:,}</b> with no activity for 14+ days &middot;
      <b>{p['open_without_amount_pct']}%</b> carrying no Amount.
    </p>
    <p style="font-size:11px;color:#999;margin:12px 0 0;">
      Weekday is shown against every column deliberately &mdash; lead and call volumes differ
      sharply by day of week, so a Monday figure next to a Sunday one will look like a jump
      that isn't one.
    </p>
  </div>
</div></body></html>"""


def _body_text(rep: dict) -> str:
    """Plain-text alternative. An HTML-only body is a common spam trigger."""
    d1, d2, d3 = rep["days"]
    p = rep["pipeline"]

    def col(v):
        return "n/a" if v is None else (f"{v:,.1f}" if isinstance(v, float) else f"{v:,}")

    lines = [
        f"{config.COMPANY_NAME} - Daily Sales Numbers",
        f"Reporting on {d1['date'].strftime('%A %d %B %Y')} and the two days before it.",
        "",
        f"{'Metric':32}{d1['date'].strftime('%a %d %b'):>12}"
        f"{d2['date'].strftime('%a %d %b'):>12}{d3['date'].strftime('%a %d %b'):>12}",
        "-" * 68,
    ]
    for label, key in [
        ("New leads", "new_leads"),
        ("Calls that happened", "calls"),
        ("Calls scheduled, not made", "calls_missed"),
        ("Same-day contact rate %", "same_day_contact_pct"),
        ("New deals", "new_deals"),
        ("Proposals sent", "proposals_sent"),
        ("Deals won", "deals_won"),
    ]:
        lines.append(f"{label:32}{col(d1.get(key)):>12}{col(d2.get(key)):>12}{col(d3.get(key)):>12}")

    lines += [
        "",
        f"Open pipeline: {p['open_deals']:,} deals, {p['stalled_14d']:,} with no activity "
        f"for 14+ days, {p['open_without_amount_pct']}% carrying no Amount.",
        "",
        "Full breakdowns are in the attached PDF.",
        "",
        "Weekday is shown against every column deliberately - lead and call volumes",
        "differ sharply by day of week.",
        "",
        f"{config.COMPANY_NAME}",
    ]
    return "\n".join(lines)


def send_daily_pdf(as_of=None) -> str | None:
    refresh_snapshot()

    if as_of is None:
        # MORNING report (08:00): yesterday plus the two days before it, all
        # complete. Do not change this — the 18:30 evening report is a separate
        # module (evening_report.py) and reports on today.
        as_of = datetime.now(IST).date() - timedelta(days=1)

    rep = metrics.build_report_data(DB_PATH, as_of=as_of)

    os.makedirs(OUT_DIR, exist_ok=True)
    pdf_path = os.path.join(OUT_DIR, f"HRP_Daily_Sales_Numbers_{as_of:%Y-%m-%d}.pdf")
    pdf_report.build_pdf(rep, pdf_path, company=config.COMPANY_NAME)

    try:
        send_mail(
            subject=f"Daily Sales Numbers - {as_of:%d %b %Y} | {config.COMPANY_NAME}",
            text_body=_body_text(rep),
            html_body=_body_html(rep),
            to=config.RECIPIENTS,
            attachment=pdf_path,
        )
        log.info("Daily PDF sent to %s", config.RECIPIENTS)
        return pdf_path
    except MailError as exc:
        log.error("Daily PDF email failed: %s", exc)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    send_daily_pdf()
