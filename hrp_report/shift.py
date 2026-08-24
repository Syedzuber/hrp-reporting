"""
hrp_report.shift — the twice-daily interval report (11:00 and 20:00 IST).

This supersedes the single evening edition. Each run covers the interval since
the previous run, so the two together tile the whole day with no gap and no
overlap:

    11:00 run   covers 20:00 yesterday  ->  11:00 today   (15 hours, overnight)
    20:00 run   covers 11:00 today      ->  20:00 today   ( 9 hours, the shift)

Because the two windows are different lengths, the comparison columns are the
SAME window on the previous two days — never the other shift. Comparing a
nine-hour working shift against a fifteen-hour overnight window would be
meaningless.

Run standalone:   python -m hrp_report.shift
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, time, timedelta

import pytz
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

import settings as config
import settings
from mailer import MailError, send as send_mail
from .metrics import (
    CALL_COMPLETED, _dt, _link_id, load_records, normalise_person, snapshot_time,
)

log = logging.getLogger(__name__)
IST = pytz.timezone(settings.TIMEZONE)

DB_PATH = settings.DB_PATH
OUT_DIR = settings.REPORTS_DIR

ROSTER = ["Deepti", "Neelam Kumari", "Chandan Kumar Singh",
          "Shivam Tiwari", "Ashish Tripathi"]

SURVEY_WORDS = ("survey", "site visit", "site-visit", "inspection")

# The four statuses the chart tracks, in escalation order. The order carries
# meaning — it is a progression from untouched to three failed attempts — so it
# is never re-sorted by value.
LADDER = ["Not Rated", "Missed 1", "Missed 2", "Missed 3"]

# A sequential single-hue ramp, light to dark, because these are ordered stages
# of the same failure rather than four unrelated categories. Lightness descends
# monotonically, so the chart survives a monochrome printer.
LADDER_FILL = [colors.HexColor(h) for h in
               ("#f0cdb0", "#dda071", "#c2703f", "#8f3d1c")]

INK = colors.HexColor("#16191d")
MUTE = colors.HexColor("#6b7280")
LINE = colors.HexColor("#d9dde3")
ZERO = colors.HexColor("#9c2b28")
GRID = colors.HexColor("#e8ebef")


# ── window arithmetic ─────────────────────────────────────────────────

def shift_boundaries():
    """The clock times that divide the day, sorted."""
    out = []
    for raw in settings.SHIFT_TIMES:
        try:
            h, m = str(raw).split(":")[:2]
            out.append(time(int(h), int(m)))
        except Exception:
            log.error("Bad shift time %r — ignored.", raw)
    return sorted(out) or [time(11, 0), time(20, 0)]


def window_for(now: datetime):
    """(start, end) of the interval ending at `now`.

    Start is the most recent shift boundary strictly before now, looking back
    into yesterday when the run is the first of the day.
    """
    bounds = shift_boundaries()
    candidates = []
    for offset in (1, 0):
        day = (now - timedelta(days=offset)).date()
        for t in bounds:
            candidates.append(IST.localize(datetime.combine(day, t)))
    earlier = [c for c in candidates if c < now]
    start = max(earlier) if earlier else now - timedelta(hours=12)
    return start, now


# ── metrics over an interval ──────────────────────────────────────────

def _completed(call):
    status = (call.get("Call_Status") or "").strip().lower()
    return status in CALL_COMPLETED or (call.get("Call_Duration_in_seconds") or 0) > 0


def _index(data):
    calls, leads = data.get("Calls", []), data.get("Leads", [])
    deals, proposals = data.get("Deals", []), data.get("Proposals", [])
    events = data.get("Events", [])

    actor = {}
    for rec in list(leads) + list(deals):
        who = normalise_person(rec.get("Done_By"))
        if who != "Not recorded":
            actor[rec.get("id")] = (who, _dt(rec.get("Modified_Time")))

    timed, seen, followup_ids = [], set(), set()
    for c in calls:
        if not _completed(c):
            continue
        when = _dt(c.get("Call_Start_Time")) or _dt(c.get("Created_Time"))
        if when:
            timed.append((when, c))
    timed.sort(key=lambda x: x[0])
    for when, c in timed:
        target = _link_id(c)
        if target:
            if target in seen:
                followup_ids.add(id(c))
            seen.add(target)

    return {
        "actor": actor,
        "calls": timed,
        "followup_ids": followup_ids,
        "leads": [(_dt(l.get("Created_Time")), l) for l in leads if _dt(l.get("Created_Time"))],
        # Proposal Date carries no time of day, so the interval report uses the
        # record's creation timestamp instead. Daily totals still match.
        "proposals": [(_dt(p.get("Created_Time")), p) for p in proposals if _dt(p.get("Created_Time"))],
        "events": [(_dt(e.get("Start_DateTime")) or _dt(e.get("Created_Time")), e)
                   for e in events],
    }


def _in(items, start, end):
    return [obj for when, obj in items if when and start <= when < end]


def _attribute(idx, calls, start, end):
    """Credit a call to the Done By on the record it is attached to.

    Only trusted when that record was touched inside the same window, because
    Done By holds the most recent actor and drifts over time.
    """
    out = defaultdict(int)
    for c in calls:
        rec = idx["actor"].get(_link_id(c))
        if rec and rec[1] and start <= rec[1] < end:
            out[rec[0]] += 1
        else:
            out["Not attributable"] += 1
    return dict(out)


def _is_survey(event):
    return any(w in (event.get("Event_Title") or "").lower() for w in SURVEY_WORDS)


def window_metrics(idx, start, end):
    calls = [c for when, c in idx["calls"] if start <= when < end]
    followups = [c for c in calls if id(c) in idx["followup_ids"]]
    leads = _in(idx["leads"], start, end)
    proposals = _in(idx["proposals"], start, end)
    events = _in(idx["events"], start, end)
    tomorrow = _in(idx["events"], end, end + timedelta(days=1))

    def tally(items, keyfn):
        c = defaultdict(int)
        for it in items:
            c[keyfn(it)] += 1
        return dict(c)

    status = tally(leads, lambda l: (l.get("Lead_Status") or "Not set").strip() or "Not set")

    return {
        "start": start, "end": end,
        "hours": round((end - start).total_seconds() / 3600.0, 1),
        "calls": len(calls),
        "calls_by_person": _attribute(idx, calls, start, end),
        "followups": len(followups),
        "followups_by_person": _attribute(idx, followups, start, end),
        "proposals": len(proposals),
        "proposals_by_person": tally(
            proposals, lambda p: normalise_person(p.get("Proposal_Sender_Name"))),
        "surveys_scheduled": sum(1 for e in events if _is_survey(e)),
        "meetings_done": sum(1 for e in events if not _is_survey(e)),
        "meetings_tomorrow": sum(1 for e in tomorrow if not _is_survey(e)),
        "leads": len(leads),
        "leads_by_status": status,
        "ladder": {k: status.get(k, 0) for k in LADDER},
    }


def build_shift_data(db_path: str, now: datetime | None = None) -> dict:
    data = load_records(db_path)
    idx = _index(data)
    now = now or datetime.now(IST)
    start, end = window_for(now)

    windows = [(start, end)]
    for back in (1, 2):
        windows.append((start - timedelta(days=back), end - timedelta(days=back)))

    return {
        "windows": [window_metrics(idx, s, e) for s, e in windows],
        "events_synced": len(data.get("Events", [])),
        "snapshot_at": snapshot_time(db_path),
        "generated_at": now,
    }


# ── the chart ─────────────────────────────────────────────────────────

class LadderChart(Flowable):
    """Horizontal bars for the four escalation statuses.

    Bars stay in escalation order rather than sorted by size, because the order
    is the information. Every bar is labelled, so identity never rests on colour
    alone and the chart still reads if printed in black and white.
    """

    def __init__(self, current: dict, previous: dict, width=170 * mm, row=13):
        super().__init__()
        self.cur, self.prev = current, previous
        self.width = width
        self.row = row
        self.height = row * len(LADDER) + 20

    def draw(self):
        c = self.canv
        label_w, right_w = 34 * mm, 30 * mm
        plot_w = self.width - label_w - right_w
        top = self.height - 14
        peak = max([self.cur.get(k, 0) for k in LADDER] +
                   [self.prev.get(k, 0) for k in LADDER] + [1])

        c.setFont("Helvetica-Bold", 7.2)
        c.setFillColor(MUTE)
        c.drawString(0, self.height - 8, "LEADS IN THIS WINDOW BY STATUS")
        c.setFont("Helvetica", 7.2)
        c.drawRightString(self.width, self.height - 8, "faint bar = same window yesterday")

        # recessive gridlines, so magnitude is readable without a heavy axis
        c.setStrokeColor(GRID)
        c.setLineWidth(0.4)
        for frac in (0.25, 0.5, 0.75, 1.0):
            x = label_w + plot_w * frac
            c.line(x, top - self.row * len(LADDER) + 3, x, top + 3)

        for i, key in enumerate(LADDER):
            y = top - (i + 1) * self.row + 3
            now_v, was_v = self.cur.get(key, 0), self.prev.get(key, 0)

            c.setFont("Helvetica", 8.2)
            c.setFillColor(INK)
            c.drawString(0, y + 1.5, key)

            # yesterday, as a thin ghost behind today
            if was_v:
                c.setFillColor(colors.HexColor("#e9edf1"))
                c.roundRect(label_w, y - 1, max(1.2, plot_w * was_v / peak), 3, 1.5,
                            stroke=0, fill=1)

            if now_v:
                c.setFillColor(LADDER_FILL[i])
                c.roundRect(label_w, y + 2.5, max(1.6, plot_w * now_v / peak), 6.5, 2,
                            stroke=0, fill=1)

            c.setFont("Helvetica-Bold", 8.6)
            c.setFillColor(ZERO if now_v == 0 else INK)
            c.drawString(label_w + plot_w + 4, y + 1.5, f"{now_v:,}")

            if was_v or now_v:
                delta = now_v - was_v
                c.setFont("Helvetica", 7.4)
                c.setFillColor(MUTE)
                c.drawString(label_w + plot_w + 16,
                             y + 1.5, f"({delta:+d} vs yest.)" if delta else "(no change)")

        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.line(label_w, top - self.row * len(LADDER) + 3, label_w, top + 3)


# ── PDF ───────────────────────────────────────────────────────────────

def _people(wins, key):
    extra = []
    for w in wins:
        for n in (w.get(key) or {}):
            if n not in ROSTER and n != "Not attributable" and n not in extra:
                extra.append(n)
    return ROSTER + sorted(extra) + ["Not attributable"]


def _hdr(wins):
    out = [""]
    for i, w in enumerate(wins):
        d = w["end"]
        out.append(f"{d:%a %d %b}\n{w['start']:%H:%M}-{w['end']:%H:%M}" if i == 0
                   else f"{d:%a %d %b}\n{w['start']:%H:%M}-{w['end']:%H:%M}")
    return out


def _table(rows, widths, flag_zero=True):
    t = Table(rows, colWidths=widths)
    last = len(rows) - 1
    cmds = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.2),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("FONTSIZE", (0, 1), (-1, -1), 8.6),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
        ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 1), (-1, last - 1), 0.4, LINE),
        ("LINEABOVE", (0, last), (-1, last), 0.9, INK),
        ("FONTNAME", (0, last), (-1, last), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]
    if flag_zero:
        for r in range(1, last):
            if rows[r][1] == "0":
                cmds.append(("TEXTCOLOR", (1, r), (1, r), ZERO))
    t.setStyle(TableStyle(cmds))
    return t


def build_pdf(rep: dict, out_path: str, company: str = "Horizon Renewable Power") -> str:
    wins = rep["windows"]
    w1 = wins[0]
    gen = rep["generated_at"]

    st = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=st["Title"], fontName="Helvetica-Bold",
                        fontSize=17, leading=20, textColor=INK, alignment=TA_LEFT,
                        spaceAfter=2)
    sub = ParagraphStyle("sub", parent=st["Normal"], fontSize=9.5, leading=13, textColor=MUTE)
    sec = ParagraphStyle("sec", parent=st["Normal"], fontName="Helvetica-Bold",
                         fontSize=10.5, leading=14, textColor=INK, spaceBefore=12, spaceAfter=4)
    note = ParagraphStyle("note", parent=st["Normal"], fontSize=7.8, leading=10.5, textColor=MUTE)

    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=13 * mm, bottomMargin=12 * mm,
                            title=f"Shift Report — {company}", author=company)

    label = "Overnight" if w1["hours"] > 12 else "Working shift"
    W = [66 * mm, 26 * mm, 26 * mm, 26 * mm]
    story = [
        Paragraph(f"Shift Report — {label}", h1),
        Paragraph(
            f"{company} &nbsp;·&nbsp; <b>{w1['start']:%a %d %b %H:%M}</b> to "
            f"<b>{w1['end']:%H:%M}</b> ({w1['hours']:g} hours). The two columns beside it "
            f"are the same window on the previous two days &mdash; not the other shift, "
            f"which is a different length.", sub),
        Spacer(1, 8),
    ]

    # staleness guard, same as before
    snap = rep.get("snapshot_at")
    if snap and snap < w1["start"]:
        warn = ParagraphStyle("warn", parent=st["Normal"], fontSize=9, leading=12.5,
                              textColor=colors.white, backColor=ZERO,
                              borderPadding=7, spaceAfter=8)
        story.append(Paragraph(
            f"<b>THESE NUMBERS ARE NOT REAL.</b> The CRM data was last refreshed "
            f"{snap:%d %b %H:%M IST}, before this window even began. Every zero below "
            f"means no data was downloaded, not that no work was done.", warn))

    # headline
    rows = [_hdr(wins)]
    for lbl, key in [("Calls made", "calls"), ("Follow-up calls", "followups"),
                     ("Proposals sent", "proposals"), ("New leads", "leads"),
                     ("Surveys scheduled", "surveys_scheduled"),
                     ("Client meetings done", "meetings_done"),
                     ("Meetings scheduled tomorrow", "meetings_tomorrow")]:
        rows.append([lbl] + [f"{w.get(key, 0):,}" for w in wins])
    rows.append(["", "", "", ""])
    story.append(_table(rows, W))

    # the chart
    story.append(Paragraph("Lead status in this window", sec))
    story.append(LadderChart(w1["ladder"], wins[1]["ladder"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Shaded darker as the escalation deepens. These are the statuses of leads that "
        "arrived <i>in this window</i>, as they stand now — a lead created this morning "
        "and missed three times by evening appears under Missed 3, not Not Rated.", note))

    # per-person blocks
    for title, key, unattr in [
        ("Calls made — by person", "calls_by_person", True),
        ("Follow-up calls — by person", "followups_by_person", True),
        ("Proposals sent — by person", "proposals_by_person", False),
    ]:
        names = _people(wins, key)
        if not unattr:
            names = [n for n in names if n != "Not attributable"]
        rws = [_hdr(wins)]
        for n in names:
            vals = [(w.get(key) or {}).get(n, 0) for w in wins]
            if n == "Not attributable" and not any(vals):
                continue
            rws.append([n] + [f"{v:,}" for v in vals])
        rws.append(["Total"] + [f"{sum((w.get(key) or {}).values()):,}" for w in wins])
        story.append(KeepTogether([Paragraph(title, sec), _table(rws, W)]))

    if not rep.get("events_synced"):
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "<b>Surveys and meetings read zero because nothing is logged in the Zoho "
            "Meetings module</b> — not because none happened. They begin counting the day "
            "the team starts logging. Put <i>Survey</i> or <i>Site Visit</i> in the title "
            "so surveys separate from ordinary meetings.", note))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "<b>How to read this.</b> The two daily editions tile the day without gap or "
        "overlap: the 11:00 report covers the overnight window since 20:00, and the 20:00 "
        "report covers the working shift since 11:00. Because those windows differ in "
        "length, they are never compared against each other — only against the same window "
        "on earlier days. "
        "<b>Calls by person</b> is attributed through Done By on the linked lead or deal "
        "and is approximate; what cannot be traced is listed as Not attributable rather "
        "than shared out. <b>Proposals by person</b> is exact. Proposals are placed in a "
        "window by record creation time, since Proposal Date carries no time of day.", note))
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        f"Data snapshot: {snap:%d %b %Y %H:%M IST} &nbsp;·&nbsp; generated "
        f"{gen:%d %b %Y %H:%M IST}" if snap else f"generated {gen:%d %b %Y %H:%M IST}", note))

    doc.build(story)
    return out_path


# ── email ─────────────────────────────────────────────────────────────

def _body_text(rep):
    wins = rep["windows"]
    w1 = wins[0]
    hdr = "".join(f"{w['end']:%a %d %b}".rjust(14) for w in wins)
    out = [
        f"{config.COMPANY_NAME} - Shift Report",
        f"{w1['start']:%a %d %b %H:%M} to {w1['end']:%H:%M} ({w1['hours']:g} hours)",
        "Compared with the same window on the previous two days.",
        "", f"{'Metric':28}{hdr}", "-" * 70,
    ]
    for lbl, key in [("Calls made", "calls"), ("Follow-up calls", "followups"),
                     ("Proposals sent", "proposals"), ("New leads", "leads"),
                     ("Surveys scheduled", "surveys_scheduled"),
                     ("Client meetings done", "meetings_done")]:
        out.append(f"{lbl:28}" + "".join(f"{w.get(key,0):>14,}" for w in wins))
    out += ["", "Lead status in this window", "-" * 70]
    for k in LADDER:
        out.append(f"{k:28}" + "".join(f"{(w.get('ladder') or {}).get(k,0):>14,}" for w in wins))
    out += ["", "Full per-person breakdowns are in the attached PDF.", "", config.COMPANY_NAME]
    return "\n".join(out)


def _body_html(rep):
    wins = rep["windows"]
    w1 = wins[0]
    hdr = "".join(
        f'<th style="text-align:right;font-size:11px;color:#888;padding:0 10px 6px;'
        f'border-bottom:2px solid #16324f;">{w["end"]:%a %d %b}</th>' for w in wins)

    def row(label, vals, strong=False):
        cells = "".join(
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;'
            f'text-align:right;font-weight:{"700" if i == 0 else "400"};'
            f'color:{"#9c2b28" if (i == 0 and v == 0) else ("#1a3c5e" if i == 0 else "#666")};">'
            f'{v:,}</td>' for i, v in enumerate(vals))
        return (f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;'
                f'font-weight:{"600" if strong else "400"};">{label}</td>{cells}</tr>')

    ladder_rows = "".join(
        row(k, [(w.get("ladder") or {}).get(k, 0) for w in wins]) for k in LADDER)

    return f"""<!DOCTYPE html><html><body style="margin:0;padding:20px;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
<div style="max-width:660px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e2e5ea;">
  <div style="background:#16324f;padding:22px 26px;">
    <h1 style="color:#fff;margin:0;font-size:19px;">Shift Report</h1>
    <p style="color:rgba(255,255,255,.75);margin:5px 0 0;font-size:13px;">
      {config.COMPANY_NAME} &middot; {w1['start']:%a %d %b %H:%M} to {w1['end']:%H:%M} &middot; {w1['hours']:g} hours
    </p>
  </div>
  <div style="padding:18px 22px;">
    <table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">This window</th>{hdr}</tr>
      {row('Calls made', [w['calls'] for w in wins], True)}
      {row('Follow-up calls', [w['followups'] for w in wins], True)}
      {row('Proposals sent', [w['proposals'] for w in wins], True)}
      {row('New leads', [w['leads'] for w in wins], True)}
      {row('Surveys scheduled', [w['surveys_scheduled'] for w in wins], True)}
      {row('Client meetings done', [w['meetings_done'] for w in wins], True)}
    </table>
    <h3 style="color:#16324f;font-size:13px;margin:18px 0 8px;">Lead status in this window</h3>
    <table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">Status</th>{hdr}</tr>
      {ladder_rows}
    </table>
    <p style="font-size:12px;color:#666;margin:16px 0 0;">
      The chart and the per-person breakdowns are in the attached PDF.
    </p>
    <p style="font-size:11px;color:#999;margin:10px 0 0;">
      The 11:00 edition covers the overnight window since 20:00; the 20:00 edition covers
      the working shift since 11:00. The two are never compared against each other, since
      they are different lengths.
    </p>
  </div>
</div></body></html>"""


def send_shift_report(now=None) -> str | None:
    try:
        import crm_sync
        crm_sync.sync()
    except Exception as exc:
        log.error("Snapshot refresh failed (%s) — using existing snapshot.", exc)

    rep = build_shift_data(DB_PATH, now=now)
    w1 = rep["windows"][0]
    tag = "overnight" if w1["hours"] > 12 else "shift"

    os.makedirs(OUT_DIR, exist_ok=True)
    pdf_path = os.path.join(
        OUT_DIR, f"HRP_Shift_{w1['end']:%Y-%m-%d_%H%M}_{tag}.pdf")
    build_pdf(rep, pdf_path, company=config.COMPANY_NAME)

    try:
        send_mail(
            subject=(f"Shift Report {w1['start']:%H:%M}-{w1['end']:%H:%M} "
                     f"- {w1['end']:%d %b %Y} | {config.COMPANY_NAME}"),
            text_body=_body_text(rep), html_body=_body_html(rep),
            to=config.RECIPIENTS, attachment=pdf_path)
        log.info("Shift report sent to %s", config.RECIPIENTS)
        return pdf_path
    except MailError as exc:
        log.error("Shift report email failed: %s", exc)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    send_shift_report()
