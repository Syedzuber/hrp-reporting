"""
hrp_report.evening — the 18:30 IST end-of-day report.

Separate from the 08:00 morning report in every respect: its own metrics, its
own PDF, its own email, its own scheduled task. Nothing here is imported by
daily_pdf_email.py, so changes on this side cannot affect the morning edition.

Column 1 is TODAY up to the moment it runs. Columns 2 and 3 are the two days
before it, complete — anything logged after 18:30 yesterday was picked up by
today's sync, so no activity is lost, it simply lands a day later.

Run standalone:   python -m hrp_report.evening
"""

from __future__ import annotations

import logging
import os
import smtplib
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

import pytz
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

import settings as config
import settings
from mailer import MailError, send as send_mail
from .metrics import (
    CALL_COMPLETED, _day, _dt, _link_id, load_records, normalise_person,
    snapshot_time,
)

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# On Railway these point at the mounted volume so data survives redeploys.
DB_PATH = settings.DB_PATH
OUT_DIR = settings.REPORTS_DIR

# Everyone who should always appear, even on a zero day — a missing name reads
# as "no data", a zero reads as "no work". The second is the useful signal.
ROSTER = [
    "Deepti",
    "Neelam Kumari",
    "Chandan Kumar Singh",
    "Shivam Tiwari",
    "Ashish Tripathi",
]

# Events (Meetings) are classified by title keyword, because the module has no
# type field. Tell the team to name them accordingly and these start counting.
SURVEY_WORDS = ("survey", "site visit", "site-visit", "inspection")

INK = colors.HexColor("#16191d")
MUTE = colors.HexColor("#6b7280")
LINE = colors.HexColor("#d9dde3")
BAND = colors.HexColor("#f4f6f8")
ZERO = colors.HexColor("#9b2226")


# ── metric computation ────────────────────────────────────────────────

def _completed(call):
    status = (call.get("Call_Status") or "").strip().lower()
    return status in CALL_COMPLETED or (call.get("Call_Duration_in_seconds") or 0) > 0


def _index(data):
    calls = data.get("Calls", [])
    leads = data.get("Leads", [])
    deals = data.get("Deals", [])
    proposals = data.get("Proposals", [])
    events = data.get("Events", [])

    # who worked each record, and when it was last touched
    actor = {}
    for rec in list(leads) + list(deals):
        who = normalise_person(rec.get("Done_By"))
        if who != "Not recorded":
            actor[rec.get("id")] = (who, _day(rec.get("Modified_Time")))

    # completed calls in time order, so "has been called before" is answerable
    timed = []
    for c in calls:
        if not _completed(c):
            continue
        when = _dt(c.get("Call_Start_Time")) or _dt(c.get("Created_Time"))
        if when:
            timed.append((when, c))
    timed.sort(key=lambda x: x[0])

    seen = set()
    calls_by_day = defaultdict(list)
    followups_by_day = defaultdict(list)
    for when, c in timed:
        target = _link_id(c)
        calls_by_day[when.date()].append(c)
        if target:
            if target in seen:
                followups_by_day[when.date()].append(c)
            seen.add(target)

    leads_by_day = defaultdict(list)
    for l in leads:
        d = _day(l.get("Created_Time"))
        if d:
            leads_by_day[d].append(l)

    proposals_by_day = defaultdict(list)
    for p in proposals:
        d = _day(p.get("Date_1")) or _day(p.get("Created_Time"))
        if d:
            proposals_by_day[d].append(p)

    events_by_day = defaultdict(list)
    for e in events:
        d = _day(e.get("Start_DateTime")) or _day(e.get("Created_Time"))
        if d:
            events_by_day[d].append(e)

    return {
        "actor": actor,
        "calls_by_day": calls_by_day,
        "followups_by_day": followups_by_day,
        "leads_by_day": leads_by_day,
        "proposals_by_day": proposals_by_day,
        "events_by_day": events_by_day,
    }


def _attribute(idx, calls, day):
    """Per-person call counts via the Done By on the linked record.

    A call carries only the shared Zoho login, so the person comes from the
    lead or deal it is attached to — trusted only when that record was touched
    the same day, because Done By holds the most recent actor and drifts.
    """
    out = defaultdict(int)
    for c in calls:
        rec = idx["actor"].get(_link_id(c))
        if rec and rec[1] == day:
            out[rec[0]] += 1
        else:
            out["Not attributable"] += 1
    return dict(out)


def _is_survey(event):
    title = (event.get("Event_Title") or "").lower()
    return any(w in title for w in SURVEY_WORDS)


def day_metrics(idx, day: date, now: datetime) -> dict:
    calls = idx["calls_by_day"].get(day, [])
    followups = idx["followups_by_day"].get(day, [])
    proposals = idx["proposals_by_day"].get(day, [])
    leads = idx["leads_by_day"].get(day, [])
    events = idx["events_by_day"].get(day, [])
    tomorrow = idx["events_by_day"].get(day + timedelta(days=1), [])

    def tally(items, keyfn):
        c = defaultdict(int)
        for it in items:
            c[keyfn(it)] += 1
        return dict(c)

    meetings = [e for e in events if not _is_survey(e)]
    meetings_done = [
        e for e in meetings
        if (end := _dt(e.get("End_DateTime"))) and end <= now
    ] if day == now.date() else meetings

    return {
        "date": day,
        "calls": len(calls),
        "calls_by_person": _attribute(idx, calls, day),
        "followups": len(followups),
        "followups_by_person": _attribute(idx, followups, day),
        "proposals": len(proposals),
        "proposals_by_person": tally(
            proposals, lambda p: normalise_person(p.get("Proposal_Sender_Name"))),
        "surveys_scheduled": sum(1 for e in events if _is_survey(e)),
        "meetings_done": len(meetings_done),
        "meetings_tomorrow": len([e for e in tomorrow if not _is_survey(e)]),
        "leads": len(leads),
        "leads_by_status": tally(
            leads, lambda l: (l.get("Lead_Status") or "Not set").strip() or "Not set"),
    }


def build_evening_data(db_path: str, as_of: date | None = None) -> dict:
    data = load_records(db_path)
    idx = _index(data)
    now = datetime.now(IST)
    if as_of is None:
        as_of = now.date()
    days = [as_of, as_of - timedelta(days=1), as_of - timedelta(days=2)]
    return {
        "days": [day_metrics(idx, d, now) for d in days],
        "events_synced": len(data.get("Events", [])),
        "snapshot_at": snapshot_time(db_path),
        "generated_at": now,
    }


# ── PDF ───────────────────────────────────────────────────────────────

def _people(days, key):
    """Roster first, then anyone else with activity, then Not attributable."""
    extra = []
    for d in days:
        for name in (d.get(key) or {}):
            if name not in ROSTER and name != "Not attributable" and name not in extra:
                extra.append(name)
    return ROSTER + sorted(extra) + ["Not attributable"]


def _person_table(days, key, include_unattributed=True):
    names = _people(days, key)
    if not include_unattributed:
        names = [n for n in names if n != "Not attributable"]
    rows = [[""] + [d["date"].strftime("%a %d %b") for d in days]]
    for n in names:
        vals = [(d.get(key) or {}).get(n, 0) for d in days]
        if n == "Not attributable" and not any(vals):
            continue
        rows.append([n] + [f"{v:,}" for v in vals])
    rows.append(["Total"] + [f"{sum((d.get(key) or {}).values()):,}" for d in days])
    return rows


def _style_table(rows, widths, zero_flag_col=1):
    t = Table(rows, colWidths=widths)
    last = len(rows) - 1
    cmds = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.6),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("FONTSIZE", (0, 1), (-1, -1), 8.6),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 1), (-1, last - 1), 0.4, LINE),
        ("LINEABOVE", (0, last), (-1, last), 0.9, INK),
        ("FONTNAME", (0, last), (-1, last), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]
    # a zero in today's column is the point of this report — make it visible
    for r in range(1, last):
        if rows[r][zero_flag_col] == "0":
            cmds.append(("TEXTCOLOR", (zero_flag_col, r), (zero_flag_col, r), ZERO))
    return t, cmds


def build_pdf(rep: dict, out_path: str, company: str = "Horizon Renewable Power") -> str:
    days = rep["days"]
    d1 = days[0]
    gen = rep["generated_at"]
    cut = gen.strftime("%H:%M")

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontName="Helvetica-Bold",
                        fontSize=17, leading=20, textColor=INK, alignment=TA_LEFT,
                        spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9.5, leading=13,
                         textColor=MUTE)
    sec = ParagraphStyle("sec", parent=styles["Normal"], fontName="Helvetica-Bold",
                         fontSize=10.5, leading=14, textColor=INK,
                         spaceBefore=12, spaceAfter=4)
    note = ParagraphStyle("note", parent=styles["Normal"], fontSize=7.8, leading=10.5,
                          textColor=MUTE)

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=13 * mm, bottomMargin=12 * mm,
        title=f"End of Day Report — {company}", author=company,
    )

    W = [70 * mm, 24 * mm, 24 * mm, 24 * mm]
    story = [
        Paragraph("End of Day Report", h1),
        Paragraph(
            f"{company} &nbsp;·&nbsp; <b>{d1['date'].strftime('%A %d %B %Y')}</b> up to "
            f"<b>{cut} IST</b>, with the two full days before it for comparison. "
            f"Anything logged later this evening appears in tomorrow's edition.", sub),
        Spacer(1, 6),
    ]

    # If the local snapshot predates the days being reported, every figure below
    # is meaningless — say so loudly rather than letting zeros read as inactivity.
    snap = rep.get("snapshot_at")
    stale_by = (d1["date"] - snap.date()).days if snap else None
    if stale_by and stale_by > 0:
        warn = ParagraphStyle("warn", parent=styles["Normal"], fontSize=9,
                              leading=12.5, textColor=colors.white,
                              backColor=ZERO, borderPadding=7, spaceAfter=8)
        story.append(Paragraph(
            f"<b>THESE NUMBERS ARE NOT REAL.</b> The CRM data on this machine was last "
            f"refreshed on {snap.strftime('%d %b %Y at %H:%M IST')} &mdash; "
            f"{stale_by} day{'s' if stale_by > 1 else ''} before the period reported here. "
            f"Every zero below means <i>no data was downloaded</i>, not that no work was "
            f"done. Run the sync and rebuild before reading or circulating this report.",
            warn))

    def headline(label, key, direction_note=None):
        rows = [[""] + [d["date"].strftime("%a %d %b") for d in days],
                [label] + [f"{d.get(key, 0):,}" for d in days]]
        t, cmds = _style_table(rows + [["", "", "", ""]], W)
        t = Table(rows, colWidths=W)
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 7.6),
            ("TEXTCOLOR", (0, 0), (-1, 0), MUTE),
            ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
            ("FONTSIZE", (0, 1), (-1, 1), 10),
            ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("TEXTCOLOR", (1, 1), (1, 1), ZERO if rows[1][1] == "0" else INK),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    def block(title, rows):
        t, cmds = _style_table(rows, W)
        t.setStyle(TableStyle(cmds))
        return KeepTogether([Paragraph(title, sec), t])

    # 1-2 calls
    story.append(block("Calls made — by person", _person_table(days, "calls_by_person")))
    # 8-9 follow-ups
    story.append(block("Follow-up calls — by person",
                       _person_table(days, "followups_by_person")))
    # 3-4 proposals
    story.append(block("Proposals sent — by person",
                       _person_table(days, "proposals_by_person",
                                     include_unattributed=False)))

    # 5-7 meetings and surveys
    story.append(Paragraph("Surveys and client meetings", sec))
    mrows = [[""] + [d["date"].strftime("%a %d %b") for d in days]]
    for label, key in [("Surveys scheduled", "surveys_scheduled"),
                       ("Client meetings done", "meetings_done"),
                       ("Client meetings scheduled for tomorrow", "meetings_tomorrow")]:
        mrows.append([label] + [f"{d.get(key, 0):,}" for d in days])
    t, cmds = _style_table(mrows + [["", "", "", ""]], W)
    t = Table(mrows, colWidths=W)
    mc = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.6),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("FONTSIZE", (0, 1), (-1, -1), 8.6),
        ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]
    for r in range(1, len(mrows)):
        if mrows[r][1] == "0":
            mc.append(("TEXTCOLOR", (1, r), (1, r), ZERO))
    t.setStyle(TableStyle(mc))
    story.append(t)

    if not rep.get("events_synced"):
        story.append(Spacer(1, 3))
        story.append(Paragraph(
            "<b>These read zero because meetings are not being logged in Zoho at all</b> "
            "&mdash; not because none happened. The Meetings module currently holds no "
            "records. Every meeting or survey booked in Zoho will appear here from the "
            "next run onwards. Name surveys with the word <i>Survey</i> or <i>Site Visit</i> "
            "in the title so they separate from ordinary client meetings.", note))

    # 10 today's leads by status
    story.append(Paragraph("Leads created today — current status", sec))
    wanted = ["Missed 1", "Missed 2", "Missed 3", "Not Rated"]
    others = []
    for d in days:
        for k in (d.get("leads_by_status") or {}):
            if k not in wanted and k not in others:
                others.append(k)
    srows = [[""] + [d["date"].strftime("%a %d %b") for d in days]]
    for k in wanted + sorted(others):
        srows.append([k] + [f"{(d.get('leads_by_status') or {}).get(k, 0):,}" for d in days])
    srows.append(["Total leads created"] + [f"{d.get('leads', 0):,}" for d in days])
    t, cmds = _style_table(srows, W)
    t.setStyle(TableStyle(cmds))
    story.append(t)

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "<b>How to read this.</b> The first column stops at the time in its header, so it "
        "will read lower than the two full days beside it — compare it against the same "
        "weekday, not against yesterday. "
        "<b>Calls by person:</b> callers share one Zoho login, so a call is credited to the "
        "Done By on the lead or deal it is attached to, counted only where that record was "
        "worked the same day. What cannot be traced is shown as <i>Not attributable</i> "
        "rather than spread across people. A Done By field on the Calls module would make "
        "this exact. "
        "<b>Proposals by person</b> uses Proposal Sender Name and is exact, because every "
        "proposal is its own record. "
        "<b>A follow-up</b> is any completed call to a lead or deal that had been called "
        "before — the CRM has no follow-up flag, so this is derived. "
        "<b>Status</b> reflects where each lead stands now, not the moment it was set.", note))
    story.append(Spacer(1, 5))
    snap = rep.get("snapshot_at")
    story.append(Paragraph(
        f"Data snapshot: {snap.strftime('%d %b %Y %H:%M IST') if snap else 'unknown'} "
        f"&nbsp;·&nbsp; generated {gen.strftime('%d %b %Y %H:%M IST')}", note))

    doc.build(story)
    return out_path


# ── email ─────────────────────────────────────────────────────────────

def _body_html(rep: dict) -> str:
    days = rep["days"]
    d1 = days[0]
    cut = rep["generated_at"].strftime("%H:%M")

    def row(label, vals, bold=False):
        w = "700" if bold else "400"
        cells = "".join(
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;'
            f'text-align:right;font-weight:{"700" if i == 0 else "400"};'
            f'color:{"#9b2226" if (i == 0 and v == 0) else ("#1a3c5e" if i == 0 else "#666")};">'
            f'{v:,}</td>' for i, v in enumerate(vals))
        return (f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;'
                f'font-size:13px;font-weight:{w};">{label}</td>{cells}</tr>')

    hdr = "".join(
        f'<th style="text-align:right;font-size:11px;color:#888;padding:0 10px 6px;'
        f'border-bottom:2px solid #16324f;">{d["date"].strftime("%a %d %b")}</th>'
        for d in days)

    people = _people(days, "calls_by_person")
    prows = "".join(
        row(n, [(d.get("calls_by_person") or {}).get(n, 0) for d in days])
        for n in people
        if any((d.get("calls_by_person") or {}).get(n, 0) for d in days) or n in ROSTER)

    return f"""<!DOCTYPE html><html><body style="margin:0;padding:20px;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
<div style="max-width:660px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e2e5ea;">
  <div style="background:#16324f;padding:22px 26px;">
    <h1 style="color:#fff;margin:0;font-size:19px;">End of Day Report</h1>
    <p style="color:rgba(255,255,255,.75);margin:5px 0 0;font-size:13px;">
      {config.COMPANY_NAME} &middot; {d1['date'].strftime('%A %d %B %Y')} up to {cut} IST
    </p>
  </div>
  <div style="padding:18px 22px;">
    <table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">Today</th>{hdr}</tr>
      {row('Calls made', [d['calls'] for d in days], bold=True)}
      {row('Follow-up calls', [d['followups'] for d in days], bold=True)}
      {row('Proposals sent', [d['proposals'] for d in days], bold=True)}
      {row('Surveys scheduled', [d['surveys_scheduled'] for d in days], bold=True)}
      {row('Client meetings done', [d['meetings_done'] for d in days], bold=True)}
      {row('Meetings tomorrow', [d['meetings_tomorrow'] for d in days], bold=True)}
    </table>
    <h3 style="color:#16324f;font-size:13px;margin:18px 0 8px;">Calls by person</h3>
    <table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">Person</th>{hdr}</tr>
      {prows}
    </table>
    <p style="font-size:12px;color:#666;margin:16px 0 0;">
      Full breakdowns &mdash; proposals by person, follow-ups by person, and today's leads
      by status &mdash; are in the attached PDF.
    </p>
    <p style="font-size:11px;color:#999;margin:10px 0 0;">
      Today's column stops at {cut}; anything logged later appears in tomorrow's report.
      Callers share one Zoho login, so per-person call counts are attributed via Done By
      and what cannot be traced is listed separately.
    </p>
  </div>
</div></body></html>"""


def _body_text(rep: dict) -> str:
    days = rep["days"]
    d1 = days[0]
    cut = rep["generated_at"].strftime("%H:%M")
    hdr = "".join(f"{d['date'].strftime('%a %d %b'):>12}" for d in days)
    lines = [
        f"{config.COMPANY_NAME} - End of Day Report",
        f"{d1['date'].strftime('%A %d %B %Y')} up to {cut} IST,"
        f" with the two full days before it.",
        "",
        f"{'Metric':30}{hdr}",
        "-" * 66,
    ]
    for label, key in [("Calls made", "calls"), ("Follow-up calls", "followups"),
                       ("Proposals sent", "proposals"),
                       ("Surveys scheduled", "surveys_scheduled"),
                       ("Client meetings done", "meetings_done"),
                       ("Meetings tomorrow", "meetings_tomorrow")]:
        lines.append(f"{label:30}" + "".join(f"{d.get(key, 0):>12,}" for d in days))
    lines += ["", "Calls by person", "-" * 66]
    for n in _people(days, "calls_by_person"):
        vals = [(d.get("calls_by_person") or {}).get(n, 0) for d in days]
        if any(vals) or n in ROSTER:
            lines.append(f"{n:30}" + "".join(f"{v:>12,}" for v in vals))
    lines += ["", "Full breakdowns are in the attached PDF.", "",
              f"Today's column stops at {cut}; later activity appears tomorrow.",
              "", config.COMPANY_NAME]
    return "\n".join(lines)


def send_evening_report(as_of=None) -> str | None:
    try:
        import crm_sync
        crm_sync.sync()
        log.info("Snapshot refreshed.")
    except Exception as exc:
        log.error("Snapshot refresh failed (%s) — using existing snapshot.", exc)

    rep = build_evening_data(DB_PATH, as_of=as_of)
    day = rep["days"][0]["date"]

    os.makedirs(OUT_DIR, exist_ok=True)
    pdf_path = os.path.join(OUT_DIR, f"HRP_End_of_Day_{day:%Y-%m-%d}.pdf")
    build_pdf(rep, pdf_path, company=config.COMPANY_NAME)

    try:
        send_mail(
            subject=f"End of Day Report - {day:%d %b %Y} | {config.COMPANY_NAME}",
            text_body=_body_text(rep),
            html_body=_body_html(rep),
            to=config.RECIPIENTS,
            attachment=pdf_path,
        )
        log.info("End of day report sent to %s", config.RECIPIENTS)
        return pdf_path
    except MailError as exc:
        log.error("End of day email failed: %s", exc)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    send_evening_report()
