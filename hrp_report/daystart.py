"""
hrp_report.daystart — the 09:00 IST day-start report.

This is not a shift edition. A shift window ending at 09:00 would cover 20:00
yesterday to 09:00 today, thirteen hours in which nobody works, and would
report near-zero every morning. The 20:00 report already answers "what
happened today". This one answers "what must happen today": it is built from
follow-up dates, the meeting diary and untouched new leads, with one line
looking back at yesterday.

Sections, in order:

  yesterday          one line, from shift.window_metrics over 09:00-20:00
  commitments        open leads and deals whose follow-up falls today
  overdue this week  follow-ups missed by 1-7 days — the actionable list
  aged backlog       missed by more than a week, with the change since the
                     previous run. A standing queue, not window-scoped.
  coverage           how much of the open book carries a follow-up date at
                     all, and how much carries one that is still current
  diary              meetings starting today
  untouched leads    created since 20:00 yesterday and still Not Rated

FIELD NOTE. The follow-up field is spelled differently on the two modules:
Next_Follow_up on Leads, Next_Follow_Up on Deals. Each module is read by its
own name and never by the other. The wrong spelling returns nothing at all,
which reports a silent zero rather than an error.

Both carry a time of day, so "today" is a comparison of IST dates: a follow-up
at 08:30 that has not happened by 09:00 is still today's, not overdue.

Run standalone:   python -m hrp_report.daystart                  build only
                  python -m hrp_report.daystart --no-sync --at 2026-10-26T09:00
                  python -m hrp_report.daystart --send           email RECIPIENTS
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import KeepTogether, Paragraph, Spacer, TableStyle

import settings
from . import shift
from .metrics import (
    LOST_STAGES, WON_STAGES, SyncFailed, _dt, load_records, normalise_person, snapshot_time,
)
from .shift import (
    IST, INK, ROSTER, SNAPSHOT_GRACE, ZERO, _doc, _mail_report, _refresh_snapshot,
    _stale_banner, _styles, sync_failed_message,
    _table,
)

log = logging.getLogger(__name__)

DB_PATH = settings.DB_PATH
OUT_DIR = settings.REPORTS_DIR

# Spelled differently on purpose: that is how they are in Zoho. Never try both.
FOLLOW_UP_FIELD = {"Leads": "Next_Follow_up", "Deals": "Next_Follow_Up"}
LEAD_CLOSED = {"junk lead", "junk", "lost lead"}
DEAL_CLOSED = WON_STAGES | LOST_STAGES
UNTOUCHED = {"", "not rated"}

# A blank Done By. Staff share one Zoho login, so Done By is the only owner a
# record has: blank means nobody is working it. On these per-person tables it
# is usually the largest row, so it is labelled for what it is and shown on its
# own, never folded in with the named people.
UNASSIGNED = "Unassigned"

# Yesterday's look-back is the working day, the same window the 20:00 report
# covers. New leads are counted from the end of it.
DAY_FROM, DAY_TO = time(9, 0), time(20, 0)

# Below this share of open records with a current follow-up date, a short
# commitments list says more about missing dates than about a quiet day.
CURRENT_FLOOR = 50

KINDS = [("survey", "Survey"), ("client", "Client discussion"),
         ("unclassified", "Unclassified")]


# ── helpers ───────────────────────────────────────────────────────────

def _open(module, rec):
    if module == "Leads":
        return (rec.get("Lead_Status") or "").strip().lower() not in LEAD_CLOSED
    return (rec.get("Stage") or "").strip().lower() not in DEAL_CLOSED


def _who(rec):
    raw = (rec.get("Done_By") or "").strip()
    return UNASSIGNED if raw in ("", "-None-") else normalise_person(raw)


def _names(*tallies):
    """The roster in its fixed order, then anyone else, then Unassigned last.

    Unassigned is listed even at zero: an absent row says nothing, while a zero
    says every record in the table has someone on it.
    """
    seen = set().union(*tallies)
    extra = sorted(n for n in seen if n not in ROSTER and n != UNASSIGNED)
    return ROSTER + extra + [UNASSIGNED]


def _flag_unassigned(table, rows):
    """Bold the Unassigned row in red so the largest share cannot be skimmed past."""
    for r, row in enumerate(rows):
        if row and row[0] == UNASSIGNED:
            table.setStyle(TableStyle([("FONTNAME", (0, r), (-1, r), "Helvetica-Bold"),
                                       ("TEXTCOLOR", (0, r), (-1, r), ZERO)]))
    return table


def _pct(n, d):
    return round(100 * n / d) if d else 0


def _n(count, word):
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _signed(n):
    return f"+{n:,}" if n > 0 else (f"-{abs(n):,}" if n < 0 else "0")


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _owner(event):
    owner = event.get("Owner")
    name = owner.get("name") if isinstance(owner, dict) else owner
    return (name or "").strip() or "Not recorded"


# ── aged-backlog history ──────────────────────────────────────────────
#
# The change since yesterday cannot be recomputed from today's data: a record
# someone worked yesterday now carries a new date, so it is missing from both
# the "now" and the recomputed "then", and the burn-down is invisible. Each run
# therefore stores its own figure, in the same database on the volume.

_HISTORY = """
    CREATE TABLE IF NOT EXISTS daystart_backlog (
        day          TEXT PRIMARY KEY,
        total        INTEGER NOT NULL,
        by_person    TEXT NOT NULL,
        recorded_at  TEXT NOT NULL
    )
"""


def previous_backlog(db_path: str, day: date) -> dict | None:
    """The most recent stored aged-backlog figure from before `day`, or None."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='daystart_backlog'").fetchone():
            return None
        row = conn.execute(
            "SELECT day, total, by_person FROM daystart_backlog WHERE day < ? "
            "ORDER BY day DESC LIMIT 1", (day.isoformat(),)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"day": date.fromisoformat(row[0]), "total": row[1],
            "by_person": json.loads(row[2])}


def record_backlog(db_path: str, rep: dict) -> None:
    """Store today's aged-backlog figure for tomorrow's comparison."""
    aged = rep["aged"]
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(_HISTORY)
        conn.execute(
            "INSERT OR REPLACE INTO daystart_backlog VALUES (?, ?, ?, ?)",
            (rep["today"].isoformat(), aged["total"], json.dumps(aged["by_person"]),
             rep["generated_at"].isoformat()))
        conn.commit()
    finally:
        conn.close()


# ── the data ──────────────────────────────────────────────────────────

def _stale(snap, now, sync_ok):
    """Banner markup, or None. The same two routes as shift.stale_message: the
    fact that this run's sync failed, and a time-based backstop for builds that
    did not sync. This report has no window, so the backstop measures from now."""
    missing = ("Leads that arrived since then and follow-up dates changed since then "
               "are missing: a short list below is out of date, not clear.")
    if not sync_ok:
        return sync_failed_message(snap, missing)
    if snap is None or snap < now - SNAPSHOT_GRACE:
        when = f"{snap:%d %b %H:%M IST}" if snap else "never"
        hours = SNAPSHOT_GRACE.total_seconds() / 3600
        return (f"<b>THIS IS NOT THIS MORNING'S DATA.</b> The CRM data was last refreshed "
                f"{when}, more than {hours:g} hour{'' if hours == 1 else 's'} before this "
                f"report was built. {missing}")
    return None


def build_daystart_data(db_path: str, now: datetime | None = None,
                        sync_ok: bool = True) -> dict:
    """`sync_ok` is False when this run's own sync failed — see build_shift_data."""
    data = load_records(db_path)
    idx = shift._index(data)
    now = now or datetime.now(IST)
    today = now.date()
    yday = today - timedelta(days=1)
    y_from = IST.localize(datetime.combine(yday, DAY_FROM))
    y_to = IST.localize(datetime.combine(yday, DAY_TO))
    since = shift.ladder_since()

    commitments = defaultdict(lambda: {"Leads": 0, "Deals": 0})
    this_week = defaultdict(lambda: {"1-3": 0, "4-7": 0})
    aged = defaultdict(lambda: {"Leads": 0, "Deals": 0})
    coverage = {}
    oldest = 0
    for module, field in FOLLOW_UP_FIELD.items():
        cov = {"open": 0, "dated": 0, "current": 0}
        for rec in data.get(module, []):
            if not _open(module, rec):
                continue
            cov["open"] += 1
            due = _dt(rec.get(field))
            if not due:
                continue
            cov["dated"] += 1
            late = (today - due.date()).days
            who = _who(rec)
            if late <= 0:
                cov["current"] += 1
                if late == 0:
                    commitments[who][module] += 1
            elif late <= 7:
                this_week[who]["1-3" if late <= 3 else "4-7"] += 1
            else:
                aged[who][module] += 1
                oldest = max(oldest, late)
        coverage[module] = cov

    aged_by_person = {who: v["Leads"] + v["Deals"] for who, v in aged.items()}

    events = data.get("Events", [])
    diary = []
    for e in events:
        start = _dt(e.get("Start_DateTime"))
        if start and start.date() == today:
            diary.append({"at": start, "kind": shift._meeting_type(e),
                          "title": (e.get("Event_Title") or "").strip() or "(untitled)",
                          "owner": _owner(e)})
    diary.sort(key=lambda d: d["at"])
    starts = [d for d in (_dt(e.get("Start_DateTime")) for e in events) if d]

    untouched = sorted(
        ({"at": when, "name": (l.get("Full_Name") or l.get("Company") or "").strip()
          or "(no name)",
          "source": (l.get("Lead_Source") or "").strip() or "—",
          "city": (l.get("City") or "").strip() or "—",
          "who": _who(l)}
         for when, l in idx["leads"]
         if y_to <= when < now
         and (l.get("Lead_Status") or "").strip().lower() in UNTOUCHED),
        key=lambda r: r["at"])

    snap = snapshot_time(db_path)
    return {
        "today": today,
        "generated_at": now,
        "snapshot_at": snap,
        "sync_ok": sync_ok,
        # Banner markup when the figures cannot be trusted as current, else None.
        "stale": _stale(snap, now, sync_ok),
        "yesterday": shift.window_metrics(idx, y_from, y_to),
        "waiting": shift.ladder_backlog(idx, since)["stuck"],
        "ladder_since": since,
        "commitments": dict(commitments),
        "this_week": dict(this_week),
        "aged": {"by_module": dict(aged), "by_person": aged_by_person,
                 "total": sum(aged_by_person.values()), "oldest_days": oldest},
        "aged_previous": previous_backlog(db_path, today),
        "coverage": coverage,
        "diary": diary,
        "events_total": len(events),
        "events_last": max(starts) if starts else None,
        "meeting_type_synced": any("Meeting_Type" in e for e in events),
        "untouched": untouched,
        "untouched_since": y_to,
    }


# ── the PDF ───────────────────────────────────────────────────────────

def _coverage_findings(rep):
    """(all-open current %, list of findings as markup)."""
    cov = rep["coverage"]
    lead, deal = cov["Leads"], cov["Deals"]
    open_all = lead["open"] + deal["open"]
    cur_all = _pct(lead["current"] + deal["current"], open_all)
    lead_cur, deal_cur = _pct(lead["current"], lead["open"]), _pct(deal["current"], deal["open"])

    out = []
    if cur_all < CURRENT_FLOOR:
        out.append(
            "<b>A short commitments list above means missing dates, not little to do.</b> "
            f"Only {cur_all}% of open records carry a follow-up date that is today or "
            "later; the rest have none, or one that has already passed.")
    # The process finding, worth more than the percentage: dates are set on
    # leads but not moved on, while deals are kept up to date.
    if lead_cur < CURRENT_FLOOR and deal_cur >= 2 * lead_cur:
        aged_leads = sum(v["Leads"] for v in rep["aged"]["by_module"].values())
        out.append(
            "<b>Follow-up dates are kept current on Deals but not on Leads.</b> "
            f"{deal_cur}% of open deals carry a current date, against {lead_cur}% of "
            f"open leads. Leads do get a date — {_pct(lead['dated'], lead['open'])}% carry "
            f"one — but {aged_leads:,} of them passed more than a week ago: the date is "
            "set once and not moved on when the lead is worked.")
    return cur_all, out


def build_pdf(rep: dict, out_path: str, company: str = "Horizon Renewable Power") -> str:
    st, h1, sub, sec, note = _styles()
    lead = ParagraphStyle("lead", parent=st["Normal"], fontSize=10.5, leading=14.5,
                          textColor=INK, spaceAfter=3)
    cell = ParagraphStyle("cell", parent=st["Normal"], fontSize=8.6, leading=10.5,
                          textColor=INK)
    # A data finding: set in body type on a tint so it reads as part of the
    # argument, not as a footnote to it.
    finding = ParagraphStyle("finding", parent=st["Normal"], fontSize=9, leading=12.5,
                             textColor=INK, backColor=colors.HexColor("#f6e7e6"),
                             borderPadding=6, spaceBefore=10, spaceAfter=4)
    doc = _doc(out_path, "Day Start", company)
    today, gen, snap = rep["today"], rep["generated_at"], rep["snapshot_at"]

    story = [
        Paragraph(f"Day Start — {today:%A %d %B %Y}", h1),
        Paragraph(
            f"{company} &nbsp;·&nbsp; what has to happen today. Built from follow-up "
            f"dates, the meeting diary and new leads &mdash; not from a window of "
            f"activity, which at this hour would be the empty overnight.", sub),
        Spacer(1, 8),
    ]

    if rep["stale"]:
        story.append(_stale_banner(st, rep["stale"]))

    # 1 — yesterday, one line
    y = rep["yesterday"]
    story.append(Paragraph(
        f"<b>Yesterday:</b> {_n(y['calls'], 'call')}, {_n(y['proposals'], 'proposal')}, "
        f"{_n(y['deals_converted'], 'deal')} converted, "
        f"{_n(rep['waiting'], 'lead')} still waiting.", lead))
    story.append(Paragraph(
        f"{y['start']:%a %d %b}, 09:00&ndash;20:00, the same window as the 20:00 report. "
        f"<i>Still waiting</i> is the standing lead backlog: every lead since "
        f"{rep['ladder_since']:%d %b %Y} still at Not Rated or Missed 1&ndash;3.", note))

    W4 = [72 * mm, 30 * mm, 30 * mm, 30 * mm]

    # 2 — today's commitments
    c = rep["commitments"]
    rows = [["", "Leads", "Deals", "Total"]]
    for name in _names(c):
        v = c.get(name, {"Leads": 0, "Deals": 0})
        rows.append([name, f"{v['Leads']:,}", f"{v['Deals']:,}", f"{v['Leads'] + v['Deals']:,}"])
    tl = sum(v["Leads"] for v in c.values())
    td = sum(v["Deals"] for v in c.values())
    rows.append(["Total", f"{tl:,}", f"{td:,}", f"{tl + td:,}"])
    story.append(KeepTogether([
        Paragraph("Today's commitments", sec),
        Paragraph(
            f"Open leads and deals whose next follow-up falls on {today:%a %d %b}. "
            "Follow-up values carry a time of day, so <i>today</i> means any time on "
            "today's IST date &mdash; including times before this report ran. Junk, Lost "
            "and Won records are left out.", note),
        Spacer(1, 4),
        _flag_unassigned(_table(rows, W4, flag_zero=False, bold_first_col=False), rows),
    ]))

    # 3a — overdue this week
    w = rep["this_week"]
    rows = [["", "1–3 days late", "4–7 days late", "Total"]]
    for name in _names(w):
        v = w.get(name, {"1-3": 0, "4-7": 0})
        rows.append([name, f"{v['1-3']:,}", f"{v['4-7']:,}", f"{v['1-3'] + v['4-7']:,}"])
    a = sum(v["1-3"] for v in w.values())
    b = sum(v["4-7"] for v in w.values())
    rows.append(["Total", f"{a:,}", f"{b:,}", f"{a + b:,}"])
    story.append(KeepTogether([
        Paragraph("Overdue this week", sec),
        Paragraph(
            "Open leads and deals whose follow-up date passed in the last seven days. "
            "This is the actionable list: recent misses, still open, worth calling today.",
            note),
        Spacer(1, 4),
        _flag_unassigned(_table(rows, W4, flag_zero=False, bold_first_col=False), rows),
    ]))

    # 3b — aged backlog, a queue to burn down
    ag, prev = rep["aged"], rep["aged_previous"]
    by_mod, by_person = ag["by_module"], ag["by_person"]
    prev_people = prev["by_person"] if prev else {}
    head = f"Change since\n{prev['day']:%a %d %b}" if prev else "Change"
    rows = [["", "Leads", "Deals", "Total", head]]
    for name in _names(by_person, prev_people):
        v = by_mod.get(name, {"Leads": 0, "Deals": 0})
        now_n = by_person.get(name, 0)
        change = _signed(now_n - prev_people.get(name, 0)) if prev else "—"
        rows.append([name, f"{v['Leads']:,}", f"{v['Deals']:,}", f"{now_n:,}", change])
    rows.append(["Total",
                 f"{sum(v['Leads'] for v in by_mod.values()):,}",
                 f"{sum(v['Deals'] for v in by_mod.values()):,}",
                 f"{ag['total']:,}",
                 _signed(ag["total"] - prev["total"]) if prev else "—"])
    since_note = ("No earlier figure is stored yet, so the change column fills in from "
                  "the next run. " if not prev else "")
    unowned = by_mod.get(UNASSIGNED, {"Leads": 0, "Deals": 0})
    aged_leads = sum(v["Leads"] for v in by_mod.values())
    aged_deals = sum(v["Deals"] for v in by_mod.values())
    owner_finding = []
    if unowned["Leads"] or unowned["Deals"]:
        parts = []
        if unowned["Leads"]:
            parts.append(f"<b>{unowned['Leads']:,} of the {aged_leads:,} aged leads "
                         f"({_pct(unowned['Leads'], aged_leads)}%) have no owner set</b>")
        if unowned["Deals"]:
            parts.append(f"<b>{unowned['Deals']:,} of the {aged_deals:,} aged deals</b>"
                         if parts else
                         f"<b>{unowned['Deals']:,} of the {aged_deals:,} aged deals "
                         f"({_pct(unowned['Deals'], aged_deals)}%) have no owner set</b>")
        owner_finding = [Paragraph(
            " and ".join(parts) + " &mdash; Done By is blank. These cannot be chased by "
            "anyone; they need assigning before they can be worked. The per-person rows "
            "above them are only the part of the queue someone owns.", finding)]
    story.append(KeepTogether([
        Paragraph("Aged backlog — more than a week overdue", sec),
        Paragraph(
            "Open records whose follow-up date passed more than seven days ago. This is "
            "<b>not window-scoped</b>: like the lead backlog on the 20:00 report, it is the "
            "standing queue, counted fresh from every open record each morning. Read it "
            "for direction, not level. It falls only when someone works an old record "
            "and sets a new date, so the column to watch is the change. "
            + since_note
            + (f"The oldest is {ag['oldest_days']:,} days overdue." if ag["total"] else ""),
            note),
        Spacer(1, 4),
        _flag_unassigned(_table(rows, [62 * mm, 26 * mm, 26 * mm, 26 * mm, 30 * mm],
                                flag_zero=False, bold_first_col=False), rows),
    ] + owner_finding))

    # 4 — follow-up coverage
    cov = rep["coverage"]
    rows = [["Open records", "Open", "Carry a date", "Current\n(today or later)"]]
    for module in ("Leads", "Deals"):
        m = cov[module]
        rows.append([module, f"{m['open']:,}",
                     f"{m['dated']:,} ({_pct(m['dated'], m['open'])}%)",
                     f"{m['current']:,} ({_pct(m['current'], m['open'])}%)"])
    o = sum(m["open"] for m in cov.values())
    d = sum(m["dated"] for m in cov.values())
    cu = sum(m["current"] for m in cov.values())
    rows.append(["All open", f"{o:,}", f"{d:,} ({_pct(d, o)}%)", f"{cu:,} ({_pct(cu, o)}%)"])
    _, findings = _coverage_findings(rep)
    block = [
        Paragraph("Follow-up coverage", sec),
        Paragraph(
            "How much of the open book the lists above can see. <b>Carry a date</b> is "
            "any follow-up date at all; <b>current</b> is one that is today or later. "
            "A record with no date, or a stale one, never appears under today's "
            "commitments however much it needs a call.", note),
        Spacer(1, 4),
        _table(rows, W4, flag_zero=False, bold_first_col=False),
    ]
    for f in findings:
        block += [Spacer(1, 4), Paragraph(f, note)]
    story.append(KeepTogether(block))

    # 5 — today's diary
    story.append(Paragraph("Today's diary", sec))
    diary = rep["diary"]
    if diary:
        rows = [["Time", "Type", "Meeting", "Owner"]]
        for kind, label in KINDS:
            for m in (m for m in diary if m["kind"] == kind):
                rows.append([Paragraph(_esc(v), cell) for v in
                             (f"{m['at']:%H:%M}", label, m["title"], m["owner"])])
        story.append(_table(rows, [18 * mm, 32 * mm, 85 * mm, 45 * mm], flag_zero=False,
                            bold_first_col=False, total=False, numeric=False))
        notes = ["Owner is shown because meetings carry no Done By. A meeting created "
                 "under the shared HRP Admin login shows that instead of a person."]
        unc = sum(1 for m in diary if m["kind"] == "unclassified")
        if unc:
            notes.append(f"{_n(unc, 'meeting')} had no Meeting Type and nothing "
                         f"survey-like in the title, so {'it is' if unc == 1 else 'they are'} "
                         "listed as unclassified.")
        if not rep["meeting_type_synced"]:
            notes.append("Meeting Type is not in the synced data yet; until a sync brings "
                         "it in, the split is read from meeting titles.")
        story.append(Spacer(1, 4))
        story.append(Paragraph(" ".join(notes), note))
    else:
        last = rep["events_last"]
        story.append(Paragraph(
            f"<b>No meetings are logged for {today:%a %d %b}.</b> "
            f"{_n(rep['events_total'], 'meeting')} have been logged in Zoho in total"
            + (f", the most recent starting {last:%d %b %Y}" if last else "")
            + " &mdash; read a zero here against that.", note))

    # 6 — untouched new leads
    story.append(Paragraph("Untouched new leads", sec))
    new = rep["untouched"]
    if new:
        # The owner column stays even when every row is Unassigned: an empty
        # column is the finding.
        rows = [["Created", "Lead", "Source", "City", "Owner (Done By)"]]
        nobody = ParagraphStyle("nobody", parent=cell, fontName="Helvetica-Bold",
                                textColor=ZERO)
        for r in new:
            rows.append([Paragraph(_esc(v), cell) for v in
                         (f"{r['at']:%a %H:%M}", r["name"], r["source"], r["city"])]
                        + [Paragraph(_esc(r["who"]),
                                     nobody if r["who"] == UNASSIGNED else cell)])
        story.append(Paragraph(
            f"{_n(len(new), 'lead')} created since {rep['untouched_since']:%a %d %b %H:%M} "
            "and still at Not Rated or blank. Call these first. Phone numbers are left off "
            "this report; they are in Zoho.", note))
        story.append(Spacer(1, 4))
        story.append(_table(rows, [22 * mm, 58 * mm, 36 * mm, 30 * mm, 34 * mm],
                            flag_zero=False, bold_first_col=False, total=False,
                            numeric=False))
        unowned = [r for r in new if r["who"] == UNASSIGNED]
        if unowned:
            sources = defaultdict(int)
            for r in unowned:
                sources[r["source"]] += 1
            via = (f"All {len(unowned)} came in through {next(iter(sources))}."
                   if len(sources) == 1 and len(unowned) > 1 else
                   "By source: " + ", ".join(f"{s} {n}" for s, n in
                                             sorted(sources.items(), key=lambda kv: -kv[1]))
                   + ".")
            story.append(Paragraph(
                f"<b>{len(unowned):,} of the {_n(len(new), 'new lead')} arrived with no "
                f"owner.</b> {via} Until someone is set in Done By, no one is responsible "
                "for the first call.", finding))
    else:
        story.append(Paragraph(
            f"No lead created since {rep['untouched_since']:%a %d %b %H:%M} is still at "
            "Not Rated or blank.", note))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "<b>By person</b> uses Done By on the lead or deal, which holds only the most "
        "recent person to touch it &mdash; not necessarily whoever set the follow-up date. "
        "A record whose Done By is blank is listed as <b>Unassigned</b>: staff share one "
        "Zoho login, so Done By is the only owner a record has, and blank means no one.",
        note))
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        f"Data snapshot: {snap:%d %b %Y %H:%M IST} &nbsp;·&nbsp; generated "
        f"{gen:%d %b %Y %H:%M IST}" if snap else f"generated {gen:%d %b %Y %H:%M IST}", note))

    doc.build(story)
    return out_path


# ── email ─────────────────────────────────────────────────────────────

def _summary_lines(rep):
    y, ag, prev = rep["yesterday"], rep["aged"], rep["aged_previous"]
    c = rep["commitments"]
    due = sum(v["Leads"] + v["Deals"] for v in c.values())
    week = sum(v["1-3"] + v["4-7"] for v in rep["this_week"].values())
    cur_all, findings = _coverage_findings(rep)
    cov = rep["coverage"]
    o = sum(m["open"] for m in cov.values())
    unowned_new = sum(1 for r in rep["untouched"] if r["who"] == UNASSIGNED)
    lines = [
        ("Yesterday", f"{_n(y['calls'], 'call')}, {_n(y['proposals'], 'proposal')}, "
                      f"{_n(y['deals_converted'], 'deal')} converted, "
                      f"{_n(rep['waiting'], 'lead')} still waiting"),
        ("Due today", _n(due, "follow-up")),
        ("Overdue this week", _n(week, "follow-up")),
        ("Aged backlog (8+ days)", f"{ag['total']:,}"
            + (f", {ag['by_person'].get(UNASSIGNED, 0):,} with no owner"
               if ag["by_person"].get(UNASSIGNED) else "")
            + (f"  ({_signed(ag['total'] - prev['total'])} since {prev['day']:%a %d %b})"
               if prev else "")),
        ("Follow-up coverage", f"{_pct(sum(m['dated'] for m in cov.values()), o)}% carry a "
                               f"date, {cur_all}% a current one"),
        ("Meetings today", f"{len(rep['diary']):,}"),
        ("Untouched new leads", f"{len(rep['untouched']):,}"
            + (f", {unowned_new:,} with no owner" if unowned_new else "")),
    ]
    return lines, findings


def _plain(markup):
    for a, b in (("<b>", ""), ("</b>", ""), ("<i>", ""), ("</i>", ""), ("&mdash;", "-"),
                 ("&amp;", "&")):
        markup = markup.replace(a, b)
    return markup


def _body_text(rep):
    lines, findings = _summary_lines(rep)
    out = [f"{settings.COMPANY_NAME} - Day Start, {rep['today']:%a %d %b %Y}", ""]
    if rep["stale"]:
        out += ["WARNING: " + _plain(rep["stale"]), ""]
    out += [f"{label:26}{value}" for label, value in lines]
    out += [""] + [_plain(f) for f in findings]
    out += ["", "The per-person tables, the diary and the lead list are in the attached PDF.",
            "", settings.COMPANY_NAME]
    return "\n".join(out)


def _body_html(rep):
    lines, findings = _summary_lines(rep)
    rows = "".join(
        f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;'
        f'color:#666;">{label}</td><td style="padding:6px 10px;border-bottom:1px solid #eee;'
        f'font-size:13px;color:#1a3c5e;font-weight:600;">{_esc(value)}</td></tr>'
        for label, value in lines)
    stale = ""
    if rep["stale"]:
        stale = (f'<p style="background:#9c2b28;color:#fff;padding:10px 12px;font-size:13px;'
                 f'margin:0 0 14px;">{rep["stale"]}</p>')
    notes = "".join(f'<p style="font-size:12px;color:#444;margin:12px 0 0;">{f}</p>'
                    for f in findings)
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:20px;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
<div style="max-width:660px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e2e5ea;">
  <div style="background:#16324f;padding:22px 26px;">
    <h1 style="color:#fff;margin:0;font-size:19px;">Day Start</h1>
    <p style="color:rgba(255,255,255,.75);margin:5px 0 0;font-size:13px;">
      {settings.COMPANY_NAME} &middot; {rep['today']:%A %d %B %Y} &middot; what has to happen today
    </p>
  </div>
  <div style="padding:18px 22px;">
    {stale}
    <table style="width:100%;border-collapse:collapse;">{rows}</table>
    {notes}
    <p style="font-size:12px;color:#666;margin:16px 0 0;">
      The per-person tables, the diary and the lead list are in the attached PDF.
    </p>
  </div>
</div></body></html>"""


# ── send ──────────────────────────────────────────────────────────────

def write_pdf(rep: dict) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"HRP_DayStart_{rep['today']:%Y-%m-%d}.pdf")
    return build_pdf(rep, path, company=settings.COMPANY_NAME)


def send_daystart_report(now=None, sync: bool = True) -> str | None:
    """Sync, build and send, as send_shift_report: a failed sync still sends the
    report, with its banner, and raises SyncFailed afterwards."""
    sync_error = _refresh_snapshot("day-start report") if sync else None
    rep = build_daystart_data(DB_PATH, now=now, sync_ok=sync_error is None)
    pdf_path = write_pdf(rep)

    # Stored for tomorrow's change column. Not from a stale snapshot: that would
    # record yesterday's queue under today's date and show a false zero change.
    if not rep["stale"]:
        try:
            record_backlog(DB_PATH, rep)
        except Exception:
            log.exception("Could not store today's aged-backlog figure")

    sent = _mail_report(
        "Day-start report",
        subject=f"Day Start - {rep['today']:%a %d %b %Y} | {settings.COMPANY_NAME}",
        text_body=_body_text(rep), html_body=_body_html(rep), pdf_path=pdf_path)

    if sync_error is not None:
        raise SyncFailed("day-start report", sync_error, bool(sent)) from sync_error
    return sent


def _parse_at(raw):
    naive = datetime.fromisoformat(raw.replace(" ", "T"))
    return IST.localize(naive) if naive.tzinfo is None else naive.astimezone(IST)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Build the day-start report. Sends nothing "
                                             "unless --send is given.")
    ap.add_argument("--send", action="store_true", help="email it to RECIPIENTS")
    ap.add_argument("--no-sync", action="store_true", help="use the snapshot as it stands")
    ap.add_argument("--at", help="pretend it is this moment, e.g. 2026-10-26T09:00")
    args = ap.parse_args()
    when = _parse_at(args.at) if args.at else None

    if args.send:
        print(send_daystart_report(now=when, sync=not args.no_sync) or "Email failed.")
    else:
        sync_error = None if args.no_sync else _refresh_snapshot("day-start report")
        rep = build_daystart_data(DB_PATH, now=when, sync_ok=sync_error is None)
        print(_body_text(rep))
        print("\nPDF written:", write_pdf(rep))
        print("Nothing was emailed and nothing was recorded. Add --send for the real run.")
