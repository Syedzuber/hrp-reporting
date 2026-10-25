"""
hrp_report.pdf_report — numbers-only A4 PDF, three complete days side by side.

No charts. Every metric shows the last three complete days plus the change
against the previous day and against two days ago.
"""

from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

INK = colors.HexColor("#16191d")
MUTE = colors.HexColor("#6b7280")
LINE = colors.HexColor("#d9dde3")
BAND = colors.HexColor("#f4f6f8")
GOOD = colors.HexColor("#0b6e4f")
BAD = colors.HexColor("#9b2226")

# metric key, label, formatter, direction (+1 = higher is better, -1 = lower, 0 = neutral)
SECTIONS = [
    ("Lead flow", [
        ("new_leads", "New leads created", "int", 1),
        ("junk_of_days_leads", "…of those, now marked Junk", "int", -1),
        ("junk_pct_of_days_leads", "…junk rate", "pct", -1),
    ]),
    ("Contact discipline", [
        ("leads_worked", "Leads worked (last activity)", "int", 1),
        ("calls_logged", "Call records logged", "int", 0),
        ("calls", "…calls that actually happened", "int", 1),
        ("calls_missed", "…scheduled but not made", "int", -1),
        ("call_completion_pct", "…completion rate", "pct", 1),
        ("calls_per_lead", "Calls per new lead", "num", 1),
        ("leads_contacted_same_day", "New leads contacted same day", "int", 1),
        ("same_day_contact_pct", "…same-day contact rate", "pct", 1),
        ("median_first_response_h", "Median first response (hours)", "num", -1),
        ("calls_linked_pct", "Calls linked to a record", "pct", 1),
    ]),
    ("Work after conversion", [
        ("lead_calls", "Calls on leads", "int", 0),
        ("deal_calls", "Calls on deals (post-conversion)", "int", 1),
        ("deals_actioned", "Deals worked", "int", 1),
    ]),
    ("Pipeline movement", [
        ("new_deals", "New deals created", "int", 1),
        ("proposals_sent", "Proposals sent", "int", 1),
        ("proposal_value", "…value proposed (EPC cost with taxes)", "inr", 1),
        ("proposal_kw", "…capacity proposed (kW)", "num", 1),
        ("deals_won", "Deals won", "int", 1),
        ("won_value", "Won value", "inr", 1),
        ("deals_lost", "Deals lost", "int", -1),
    ]),
]


def _fmt(value, kind):
    if value is None:
        return "n/a"
    if kind == "int":
        return f"{int(value):,}"
    if kind == "num":
        return f"{value:,.1f}"
    if kind == "pct":
        return f"{value:,.1f}%"
    if kind == "inr":
        if value >= 100000:
            return f"Rs {value/100000:,.2f}L"
        return f"Rs {value:,.0f}" if value else "Rs 0"
    return str(value)


def _delta(cur, prev, kind, direction):
    """Return (text, colour). Percentages are suppressed on small counts."""
    if cur is None or prev is None:
        return "n/a", MUTE
    diff = cur - prev
    if abs(diff) < 1e-9:
        return "no change", MUTE
    sign = "+" if diff > 0 else ""
    if kind in ("pct",):
        text = f"{sign}{diff:,.1f} pts"
    elif kind == "inr":
        text = f"{sign}{_fmt(abs(diff), 'inr')}" if diff > 0 else f"-{_fmt(abs(diff), 'inr')}"
    elif kind == "num":
        text = f"{sign}{diff:,.1f}"
    else:
        # counts: show absolute, add % only when the base is big enough to mean anything
        if prev >= 10:
            text = f"{sign}{int(diff):,}  ({sign}{diff/prev*100:,.0f}%)"
        else:
            text = f"{sign}{int(diff):,}"
    if direction == 0:
        return text, MUTE
    good = (diff > 0) if direction > 0 else (diff < 0)
    return text, (GOOD if good else BAD)


BREAKDOWNS = [
    ("proposals_by_user", "Proposals sent by user"),
    ("calls_by_person", "Calls made by person"),
    ("leads_actioned_by_person", "Leads actioned by person (Done By)"),
    ("latest_call_action", "Latest call action on those leads"),
    ("deals_actioned_by_person", "Deals worked by person (Done By)"),
    ("deal_latest_action", "Latest call action on those deals"),
    ("leads_by_status", "Leads by status"),
    ("leads_by_source", "Leads by source"),
    ("leads_by_state", "Leads by state"),
    ("deals_by_capacity", "Deals by installation capacity"),
    ("proposals_by_capacity", "Proposals by plant capacity"),
]


def _breakdown_table(days, key, sec_style, note_style):
    """Three-day table for one categorical breakdown. Numbers only."""
    d1, d2, d3 = days
    keys = []
    for d in days:
        for k in (d.get(key) or {}):
            if k not in keys:
                keys.append(k)
    # order by most recent day's count, then by name
    keys.sort(key=lambda k: (-(d1.get(key) or {}).get(k, 0), str(k)))

    rows = [["", d1["date"].strftime("%a %d %b"),
             d2["date"].strftime("%a %d %b"),
             d3["date"].strftime("%a %d %b")]]
    for k in keys:
        rows.append([
            str(k),
            f"{(d1.get(key) or {}).get(k, 0):,}",
            f"{(d2.get(key) or {}).get(k, 0):,}",
            f"{(d3.get(key) or {}).get(k, 0):,}",
        ])
    if len(rows) == 1:
        rows.append(["No data recorded on these days", "0", "0", "0"])
    rows.append([
        "Total",
        f"{sum((d1.get(key) or {}).values()):,}",
        f"{sum((d2.get(key) or {}).values()):,}",
        f"{sum((d3.get(key) or {}).values()):,}",
    ])

    t = Table(rows, colWidths=[74 * mm, 22 * mm, 22 * mm, 22 * mm])
    last = len(rows) - 1
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.6),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("FONTSIZE", (0, 1), (-1, -1), 8.4),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 1), (-1, last - 1), 0.4, LINE),
        ("LINEABOVE", (0, last), (-1, last), 0.9, INK),
        ("FONTNAME", (0, last), (-1, last), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return KeepTogether([Paragraph(dict(BREAKDOWNS)[key], sec_style), t, Spacer(1, 4)])


def build_pdf(report: dict, out_path: str, company: str = "Horizon Renewable Power") -> str:
    days = report["days"]
    pipe = report["pipeline"]
    d1, d2, d3 = days[0], days[1], days[2]

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontName="Helvetica-Bold",
                        fontSize=17, leading=20, textColor=INK, alignment=TA_LEFT,
                        spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9.5, leading=13,
                         textColor=MUTE)
    sec = ParagraphStyle("sec", parent=styles["Normal"], fontName="Helvetica-Bold",
                         fontSize=10, leading=13, textColor=INK,
                         spaceBefore=10, spaceAfter=4)
    note = ParagraphStyle("note", parent=styles["Normal"], fontSize=7.8, leading=10.5,
                          textColor=MUTE)

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=13 * mm, bottomMargin=12 * mm,
        title=f"Daily Sales Numbers — {company}",
        author=company,
    )

    # Column 1 is today up to the moment this ran; the other two are complete.
    gen = report.get("generated_at")
    cut = gen.strftime("%H:%M") if gen else None
    partial_day = gen.date() if gen else None

    story = []
    story.append(Paragraph("Daily Sales Numbers", h1))
    if partial_day and d1["date"] == partial_day:
        story.append(Paragraph(
            f"{company} &nbsp;·&nbsp; <b>{d1['date'].strftime('%A %d %B %Y')}</b> up to "
            f"<b>{cut} IST</b>, with the two full days before it. Today's column is still "
            f"running &mdash; anything logged later this evening appears in tomorrow's "
            f"edition, so nothing is lost.", sub))
    else:
        story.append(Paragraph(
            f"{company} &nbsp;·&nbsp; reporting on <b>{d1['date'].strftime('%A %d %B %Y')}</b> "
            f"and the two days before it. Complete days only.", sub))
    story.append(Spacer(1, 8))

    def dcol(d):
        base = f"{d['date'].strftime('%a')}\n{d['date'].strftime('%d %b')}"
        if partial_day and d["date"] == partial_day:
            base += f"\nto {cut}"
        return base

    header = ["", dcol(d1), dcol(d2), dcol(d3), "vs prev day", "vs 2 days ago"]

    rows = [header]
    styles_cmd = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTE),
        ("LINEBELOW", (0, 0), (-1, 0), 1.1, INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 1), (-1, -1), 8.6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]

    r = 1
    for title, metrics in SECTIONS:
        rows.append([title, "", "", "", "", ""])
        styles_cmd += [
            ("SPAN", (0, r), (-1, r)),
            ("BACKGROUND", (0, r), (-1, r), BAND),
            ("FONTNAME", (0, r), (0, r), "Helvetica-Bold"),
            ("FONTSIZE", (0, r), (0, r), 8),
            ("TEXTCOLOR", (0, r), (0, r), INK),
        ]
        r += 1
        for key, label, kind, direction in metrics:
            v1, v2, v3 = d1.get(key), d2.get(key), d3.get(key)
            t_prev, c_prev = _delta(v1, v2, kind, direction)
            t_2ago, c_2ago = _delta(v1, v3, kind, direction)
            rows.append([label, _fmt(v1, kind), _fmt(v2, kind), _fmt(v3, kind), t_prev, t_2ago])
            styles_cmd += [
                ("FONTNAME", (1, r), (1, r), "Helvetica-Bold"),
                ("TEXTCOLOR", (4, r), (4, r), c_prev),
                ("TEXTCOLOR", (5, r), (5, r), c_2ago),
                ("FONTSIZE", (4, r), (5, r), 7.8),
                ("LINEBELOW", (0, r), (-1, r), 0.4, LINE),
            ]
            if label.startswith("…"):
                styles_cmd.append(("TEXTCOLOR", (0, r), (0, r), MUTE))
            r += 1

    table = Table(rows, colWidths=[62 * mm, 20 * mm, 20 * mm, 20 * mm, 29 * mm, 29 * mm],
                  repeatRows=1)
    table.setStyle(TableStyle(styles_cmd))
    story.append(table)

    # ── pipeline snapshot ────────────────────────────────────
    story.append(Paragraph("Pipeline right now (snapshot, not a daily figure)", sec))
    snap = [
        ["Open deals", _fmt(pipe["open_deals"], "int"),
         f"Open value (from {pipe.get('open_value_from', 0)} priced deals)",
         _fmt(pipe["open_value"], "inr")],
        ["Sitting in Proposal", _fmt(pipe["in_proposal"], "int"),
         "No activity 14+ days", _fmt(pipe["stalled_14d"], "int")],
        ["Open deals with no Amount", _fmt(pipe["open_without_amount"], "int"),
         "…so open value is understated by", _fmt(pipe["open_without_amount_pct"], "pct")],
        ["Follow-up date set", _fmt(pipe.get("followup_set"), "int"),
         "…of those, already overdue", _fmt(pipe.get("followup_overdue"), "int")],
        ["Open deals with no follow-up date", _fmt(pipe.get("followup_missing"), "int"), "", ""],
    ]
    st = Table(snap, colWidths=[52 * mm, 26 * mm, 52 * mm, 30 * mm])
    st.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8.6),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("TEXTCOLOR", (0, 0), (0, -1), INK),
        ("TEXTCOLOR", (2, 0), (2, -1), INK),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(st)

    # ── categorical breakdowns ───────────────────────────────
    from reportlab.platypus import PageBreak
    story.append(PageBreak())
    story.append(Paragraph("Breakdowns", h1))
    story.append(Paragraph(
        "Same three days, side by side. <b>Leads by status, source, state</b> cover the "
        "leads <i>created</i> on each day, so those three total the day's new leads. "
        "<b>Leads actioned by person</b> and <b>latest call action</b> cover every lead "
        "<i>worked</i> that day regardless of when it arrived, so their totals are larger "
        "and are not a subset of new leads.", sub))
    story.append(Spacer(1, 6))
    for key, _label in BREAKDOWNS:
        story.append(_breakdown_table(days, key, sec, note))

    # ── caveats ──────────────────────────────────────────────
    story.append(Spacer(1, 10))
    snap_at = report.get("snapshot_at")
    gen_at = report.get("generated_at")
    caveats = (
        "<b>How to read this.</b> "
        + ("The first column is today and is still incomplete &mdash; it stops at the time "
           "printed in its header, so it will read lower than the two full days beside it. "
           "Nothing is lost: whatever is logged later appears in tomorrow's edition. "
           if (partial_day and d1["date"] == partial_day) else "") +
        "Weekday is shown against every column on purpose: "
        "lead volume and call volume differ sharply by day of week, so a Monday figure "
        "compared against a Sunday will look like a jump that isn't one. "
        "Percentage change is shown only where the previous day's figure was 10 or more; "
        "below that the absolute change is the honest number.<br/><br/>"
        "<b>Known limits.</b> &nbsp;"
        "'Deals won' and 'Deals lost' are dated by last modification, which is a proxy for "
        "the day the stage actually changed &mdash; the CRM does not store stage history. "
        "'Proposals sent' counts records in the Proposals module by their Proposal Date, "
        "and 'by user' uses the Proposal Sender Name on that record rather than the record "
        "owner. "
        "<b>Calls:</b> a call record is only counted as having happened when its status says "
        "so &mdash; roughly 43% of call records in this CRM are scheduled calls that were "
        "never made, and those are shown separately rather than folded into the total. "
        "Call duration is not usable: the median duration on completed calls is one second, "
        "so no quality judgement should be drawn from it. "
        "<b>People vs accounts:</b> most callers are off-roll and share a single Zoho login, "
        "so the Call record's owner is a licence, not a person. Calls are instead attributed "
        "to the Done By on the lead or deal they are attached to, counted only where that "
        "record was touched the same day &mdash; anything else is shown as 'Not attributable' "
        "rather than guessed. A Done By field on the Calls module would make this exact. 'Leads actioned by person' is a "
        "proxy, not a call count &mdash; Done By keeps only the most recent actor, so repeat "
        "touches collapse to one and a lead worked by two people shows only the second. "
        "Proposals by user is exact, because each proposal is its own record. "
        "Installation capacity is read from a free-text field, so values that cannot be "
        "parsed as a number appear under 'Not recorded'. "
        "<b>Open value</b> sums the Amount field on open deals, but well under half of them "
        "carry one &mdash; the count of priced deals is stated beside it, and the true "
        "pipeline is materially larger than the figure shown. "
        "'Junk' reflects each lead's status at the time of the snapshot, not the day it was "
        "marked. Median first response covers only leads that were eventually called at all, "
        "so it should always be read next to the same-day contact rate and the never-called "
        "backlog rather than on its own."
    )
    story.append(Paragraph(caveats, note))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"Data snapshot: {snap_at.strftime('%d %b %Y %H:%M IST') if snap_at else 'unknown'} "
        f"&nbsp;·&nbsp; generated {gen_at.strftime('%d %b %Y %H:%M IST') if gen_at else ''}",
        note))

    doc.build(story)
    return out_path