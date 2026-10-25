"""
hrp_report.shift — the end-of-day interval report (20:00 IST).

This supersedes the single evening edition. It runs once, at 20:00, and covers
the working day, 09:00 to 20:00 (see SHIFT_WINDOW_STARTS). The comparison
columns are the same window on the previous two days.

It used to run at 11:00 as well, covering 20:00 to 11:00. That edition was
mostly hours in which nobody works and reported near-zero, so the morning slot
is now hrp_report.daystart, which looks forward rather than back.

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
    CALL_COMPLETED, LOST_STAGES, WON_STAGES, SyncFailed, _dt, _link_id, load_records,
    normalise_person, snapshot_time,
)

log = logging.getLogger(__name__)
IST = pytz.timezone(settings.TIMEZONE)

DB_PATH = settings.DB_PATH
OUT_DIR = settings.REPORTS_DIR

ROSTER = ["Deepti", "Neelam Kumari", "Chandan Kumar Singh",
          "Shivam Tiwari", "Ashish Tripathi"]

# Meeting Type is a picklist on the Meetings module: Survey / Client Discussion.
# It was added in August 2026 and is blank on everything logged before then, so
# the old title-keyword rule stays as a fallback rather than being replaced.
MEETING_SURVEY = {"survey", "site survey", "site visit"}
MEETING_CLIENT = {"client discussion", "client meeting", "meeting"}
SURVEY_WORDS = ("survey", "site visit", "site-visit", "inspection")

# The four statuses the chart tracks, in escalation order. The order carries
# meaning — it is a progression from untouched to three failed attempts — so it
# is never re-sorted by value.
#
# Unlike everything else on this report, the chart is NOT window-scoped. It is
# the standing backlog: every lead created since LADDER_SINCE that still sits in
# one of these four states. A nine-hour window shows almost nothing; the book as
# a whole is the number worth acting on.
LADDER = ["Not Rated", "Missed 1", "Missed 2", "Missed 3"]

# Open deal stages in pipeline order, for the aging table. A stage not listed
# here (a new picklist value) is appended after these rather than dropped.
DEAL_STAGES = ["Qualification", "Needs Analysis", "Site Visit", "Costing",
               "Proposal/Price Quote", "Negotiation/Review"]
# (label, lowest age in days, highest age in days) — inclusive.
DEAL_AGE_BUCKETS = [("0-7", 0, 7), ("8-15", 8, 15), ("16-30", 16, 30),
                    ("31-60", 31, 60), ("61-90", 61, 90), ("91-180", 91, 180),
                    ("180+", 181, None)]
DEAL_IDLE_DAYS = 14

# A sync stamps sync_log with the moment it STARTED, so even freshly synced data
# is a few minutes older than the end of the window it is reported against —
# longer for a full SYNC_DAYS pull. Within this grace the snapshot counts as
# current; beyond it the time-based stale banner fires. Shared with the
# day-start report.
SNAPSHOT_GRACE = timedelta(hours=1)

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
    return sorted(out) or [time(20, 0)]


def _window_start_overrides():
    """{run time -> start time} from SHIFT_WINDOW_STARTS, e.g. 20:00 -> 09:00."""
    out = {}
    for raw in getattr(settings, "SHIFT_WINDOW_STARTS", []) or []:
        try:
            run, begin = str(raw).split("=")
            rh, rm = (int(x) for x in run.strip().split(":")[:2])
            bh, bm = (int(x) for x in begin.strip().split(":")[:2])
            out[(rh, rm)] = time(bh, bm)
        except Exception:
            log.error("Bad SHIFT_WINDOW_STARTS entry %r — ignored.", raw)
    return out


def window_for(now: datetime):
    """(start, end) of the interval ending at `now`.

    Normally the start is the most recent shift boundary strictly before now,
    looking back into yesterday when the run is the first of the day.

    SHIFT_WINDOW_STARTS overrides that for a given run time. The day-end report
    is configured as 20:00=09:00 so it covers the working day rather than the
    full 24 hours since the previous 20:00 run.
    """
    overrides = _window_start_overrides()
    for (rh, rm), begin in overrides.items():
        # Match a run at, or shortly after, the configured time. A manual run
        # at 20:07 is still the 20:00 edition.
        run_at = IST.localize(datetime.combine(now.date(), time(rh, rm)))
        if run_at <= now < run_at + timedelta(hours=1):
            start = IST.localize(datetime.combine(now.date(), begin))
            if start < now:
                return start, now

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


def _activity_at(rec):
    """When this record was last actually worked.

    Zoho's own Leads charts filter on Last Activity Time, which is why a chart
    built in the CRM shows far more work than a count of Call records does: a
    lead is touched whenever someone calls it, emails it, or moves its status,
    and only some of those create a Call record.

    Modified_Time is the fallback for records synced before Last_Activity_Time
    was added to the field list. It is close but slightly looser, because a
    workflow rule or a bulk edit moves it without anyone having worked the lead.
    """
    return _dt(rec.get("Last_Activity_Time")) or _dt(rec.get("Modified_Time"))


def _index(data):
    calls, leads = data.get("Calls", []), data.get("Leads", [])
    deals, proposals = data.get("Deals", []), data.get("Proposals", [])
    events = data.get("Events", [])

    actor = {}
    for rec in list(leads) + list(deals):
        who = normalise_person(rec.get("Done_By"))
        if who != "Not recorded":
            actor[rec.get("id")] = (who, _activity_at(rec))

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
        # Leads worked — the same thing Zoho's own Leads chart counts when you
        # filter on Last Activity Time and group by Done By. See _activity_at.
        "worked": [(_activity_at(l), l) for l in leads if _activity_at(l)],
        # Proposal Date carries no time of day, so the interval report uses the
        # record's creation timestamp instead. Daily totals still match.
        "proposals": [(_dt(p.get("Created_Time")), p) for p in proposals if _dt(p.get("Created_Time"))],
        # Deals are how a lead is converted. There is no conversion timestamp
        # in this CRM — Zoho_Lead_ID is empty on every deal and
        # Lead_Conversion_Time holds a number of days, not a date — so the
        # deal's own creation time is what places a conversion in a window.
        "deals": [(_dt(d.get("Created_Time")), d) for d in deals if _dt(d.get("Created_Time"))],
        # Deals worked — same Last Activity Time rule as leads. Once a lead
        # converts it leaves the Leads module, so this is the only place the
        # team's post-conversion calling shows up.
        "deals_worked": [(_activity_at(d), d) for d in deals if _activity_at(d)],
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


def _meeting_type(event):
    """'survey', 'client' or 'unclassified' for one meeting.

    Meeting Type is authoritative when it is set. When it is blank — every
    meeting logged before the picklist existed, and any the team leaves at
    -None- — the title is searched for survey wording instead. Anything that
    matches neither is counted as a client meeting, since that is the larger
    category, but it is also totalled separately so the guessing is visible.
    """
    mt = (event.get("Meeting_Type") or "").strip().lower()
    if mt in MEETING_SURVEY:
        return "survey"
    if mt in MEETING_CLIENT:
        return "client"
    if mt:                      # a picklist value we have not seen before
        return "survey" if "survey" in mt or "visit" in mt else "client"
    title = (event.get("Event_Title") or "").lower()
    if any(w in title for w in SURVEY_WORDS):
        return "survey"
    return "unclassified"


def _is_survey(event):
    return _meeting_type(event) == "survey"


def window_metrics(idx, start, end):
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

    # ── calls, counted from the Leads module rather than the Calls module ──
    #
    # The Calls module is not usable as a call count: on a typical day two
    # thirds of its records sit at Overdue, meaning a call was scheduled and
    # the record never closed, even though the call was made. The team's real
    # record of a call is on the lead itself — they set Latest Call Action
    # (Missed, Interested, Call Back, Not Interested, …) every time they ring
    # someone. Between 92% and 99% of leads touched on any given day carry one.
    #
    # So a call here is: a lead whose last activity falls in this window and
    # which carries a call outcome. The known limit, printed on the report, is
    # that a lead rung twice in one window counts once.
    deals_new = _in(idx["deals"], start, end)

    worked = _in(idx["worked"], start, end)
    called = [l for l in worked if (l.get("Latest_Call_Action") or "").strip()]

    # A call to a lead that already existed before this window is a follow-up;
    # a call to a lead created inside it is a first contact.
    new_ids = {l.get("id") for l in leads}
    followups = [l for l in called if l.get("id") not in new_ids]
    first_contact = [l for l in called if l.get("id") in new_ids]

    # ── deals worked ──
    #
    # Every open deal whose last activity falls in this window — the same
    # figure Zoho's Deals chart shows when filtered on Last Activity Time and
    # grouped by Done By. It is split two ways so neither inflates the other:
    #   * converted this window — the deal was created inside it. A converted
    #     lead leaves the Leads module, so the call made while converting is
    #     not counted as a lead call; it is counted here instead.
    #   * follow-ups — the deal existed before this window.
    # Deals now at Closed Won / Lost are left out: there is no stage-change
    # timestamp, so a closed deal's last activity is taken to be the closing.
    # No Latest Call Action is required, unlike leads — Zoho's chart does not
    # require one either.
    new_deal_ids = {d.get("id") for d in deals_new}
    closed = WON_STAGES | LOST_STAGES
    deals_worked = [
        d for d in _in(idx["deals_worked"], start, end)
        if (d.get("Stage") or "").strip().lower() not in closed
    ]
    deal_followups = [d for d in deals_worked if d.get("id") not in new_deal_ids]
    deal_new_worked = [d for d in deals_worked if d.get("id") in new_deal_ids]

    def by_person(items):
        return tally(items, lambda l: normalise_person(l.get("Done_By")))

    return {
        "start": start, "end": end,
        "hours": round((end - start).total_seconds() / 3600.0, 1),
        "calls": len(called),
        "calls_by_person": by_person(called),
        "followups": len(followups),
        "followups_by_person": by_person(followups),
        "first_contact": len(first_contact),
        # Every lead touched, whether or not a call outcome was recorded. Kept
        # so the difference from `calls` shows how much activity is untracked.
        "leads_worked": len(worked),
        "leads_worked_by_person": by_person(worked),
        # Kept apart from `calls` so lead figures stay comparable with earlier
        # reports; see the deals-worked block above.
        "deals_worked": len(deals_worked),
        "deals_worked_by_person": by_person(deals_worked),
        "deal_new_worked": len(deal_new_worked),
        "deal_followups": len(deal_followups),
        "deals_converted": len(deals_new),
        "deals_by_person": tally(
            deals_new, lambda d: normalise_person(d.get("Done_By"))),
        "proposals": len(proposals),
        "proposals_by_person": tally(
            proposals, lambda p: normalise_person(p.get("Proposal_Sender_Name"))),
        "surveys_scheduled": sum(1 for e in events if _meeting_type(e) == "survey"),
        "meetings_done": sum(1 for e in events if _meeting_type(e) != "survey"),
        "meetings_tomorrow": sum(1 for e in tomorrow
                                 if _meeting_type(e) != "survey"),
        "surveys_tomorrow": sum(1 for e in tomorrow
                                if _meeting_type(e) == "survey"),
        # Meetings with no Meeting Type set and nothing survey-like in the
        # title. Counted as client meetings above; surfaced here so the report
        # can say how much of the split is inferred rather than recorded.
        "meetings_unclassified": sum(1 for e in events
                                     if _meeting_type(e) == "unclassified"),
        "leads": len(leads),
        "leads_by_status": status,
    }


def ladder_by_month(idx, since: datetime, until: datetime):
    """One bucket per calendar month from `since` to `until`.

    Each bucket counts leads CREATED in that month that are still sitting in one
    of the four states now. So a month's bar falling over time is the team
    clearing that cohort, not fewer leads having arrived.
    """
    months = []
    y, m = since.year, since.month
    while (y, m) <= (until.year, until.month):
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1

    buckets = {ym: {k: 0 for k in LADDER} for ym in months}
    arrived = {ym: 0 for ym in months}
    earliest = None
    for when, lead in idx["leads"]:
        if not when:
            continue
        if earliest is None or when < earliest:
            earliest = when
        ym = (when.year, when.month)
        if ym not in buckets:
            continue
        arrived[ym] += 1
        st = (lead.get("Lead_Status") or "").strip()
        if st in buckets[ym]:
            buckets[ym][st] += 1

    # A month with no leads at all that also sits before the oldest lead in the
    # snapshot is not a quiet month — it is a month the sync never reached. Say
    # so, rather than drawing four zeros that read as an intake collapse.
    floor = (earliest.year, earliest.month) if earliest else None

    out = []
    for ym in months:
        counts = buckets[ym]
        covered = floor is None or ym >= floor or arrived[ym] > 0
        out.append({
            "label": datetime(ym[0], ym[1], 1).strftime("%b"),
            "year": ym[0],
            "counts": counts,
            "stuck": sum(counts.values()),
            "arrived": arrived[ym],
            "covered": covered,
        })
    return out


def ladder_backlog(idx, since: datetime):
    """Every lead created since `since` still sitting in one of the four states.

    Cumulative, not window-scoped, and read from each lead's status as it stands
    now. This is the queue the team has to clear, not a measure of the shift.
    """
    counts = {k: 0 for k in LADDER}
    oldest = None
    total = 0
    for when, lead in idx["leads"]:
        if not when or when < since:
            continue
        total += 1
        st = (lead.get("Lead_Status") or "").strip()
        if st in counts:
            counts[st] += 1
            if oldest is None or when < oldest:
                oldest = when
    return {"counts": counts, "since": since, "leads_seen": total,
            "oldest_stuck": oldest, "stuck": sum(counts.values())}


def deal_aging(deals, now: datetime):
    """Open deals by stage, bucketed by age.

    Age runs from the deal's creation — the day the lead converted. The CRM
    keeps no stage-change timestamp, so how long a deal has sat in its current
    stage cannot be measured; age since conversion is the closest honest
    figure. `idle` counts deals with no activity for DEAL_IDLE_DAYS or more,
    which is what separates an old deal still being worked from a stalled one.

    Like the backlog, this is the standing pipeline, not scoped to a window.
    """
    closed = WON_STAGES | LOST_STAGES
    rows = {}
    for d in deals:
        stage = (d.get("Stage") or "").strip() or "Not set"
        created = _dt(d.get("Created_Time"))
        if stage.lower() in closed or not created:
            continue
        age = max(0, (now - created).days)
        r = rows.setdefault(stage, {"buckets": {b[0]: 0 for b in DEAL_AGE_BUCKETS},
                                    "total": 0, "idle": 0, "oldest": 0})
        for label, lo, hi in DEAL_AGE_BUCKETS:
            if age >= lo and (hi is None or age <= hi):
                r["buckets"][label] += 1
                break
        r["total"] += 1
        r["oldest"] = max(r["oldest"], age)
        last = _activity_at(d)
        if not last or (now - last).days >= DEAL_IDLE_DAYS:
            r["idle"] += 1

    order = ([s for s in DEAL_STAGES if s in rows]
             + sorted(s for s in rows if s not in DEAL_STAGES))
    return {"stages": [(s, rows[s]) for s in order]}


def ladder_since() -> datetime:
    """LADDER_SINCE as an IST datetime — the start of the standing lead backlog."""
    try:
        y, m, d = (int(x) for x in str(settings.LADDER_SINCE).split("-")[:3])
        return IST.localize(datetime(y, m, d))
    except Exception:
        log.error("Bad LADDER_SINCE %r — falling back to 1 Jan 2026.",
                  settings.LADDER_SINCE)
        return IST.localize(datetime(2026, 1, 1))


def build_shift_data(db_path: str, now: datetime | None = None,
                     start: datetime | None = None, sync_ok: bool = True) -> dict:
    """Build the report payload.

    `start` overrides the computed window start, so a caller can ask for an
    arbitrary span — a whole day, say — rather than the scheduled shift window.
    The two comparison columns still shift back by one and two days, keeping
    like-for-like.

    `sync_ok` is False when the sync for this run failed. The caller knows that
    for certain, so it is passed in rather than inferred, and it raises the stale
    banner however recent the previous snapshot is. Builds that did not try to
    sync leave it True and rely on the time-based check in stale_message().
    """
    data = load_records(db_path)
    idx = _index(data)
    now = now or datetime.now(IST)
    if start is not None:
        start, end = start, now
    else:
        start, end = window_for(now)

    windows = [(start, end)]
    for back in (1, 2):
        windows.append((start - timedelta(days=back), end - timedelta(days=back)))

    since = ladder_since()

    return {
        "windows": [window_metrics(idx, s, e) for s, e in windows],
        "ladder": ladder_backlog(idx, since),
        "ladder_months": ladder_by_month(idx, since, now),
        "deal_aging": deal_aging(data.get("Deals", []), now),
        "events_synced": len(data.get("Events", [])),
        "snapshot_at": snapshot_time(db_path),
        "sync_ok": sync_ok,
        "generated_at": now,
    }


# ── the chart ─────────────────────────────────────────────────────────



class LadderChart(Flowable):
    """Horizontal bars for the standing lead backlog by escalation stage.

    Bars stay in escalation order rather than sorted by size, because the order
    is the information. Every bar is labelled, so identity never rests on colour
    alone and the chart still reads printed in black and white.
    """

    def __init__(self, counts: dict, width=170 * mm, row=15):
        super().__init__()
        self.counts = counts
        self.width = width
        self.row = row
        self.height = row * len(LADDER) + 22

    def draw(self):
        c = self.canv
        label_w, right_w = 34 * mm, 34 * mm
        plot_w = self.width - label_w - right_w
        top = self.height - 16
        total = sum(self.counts.get(k, 0) for k in LADDER)
        peak = max([self.counts.get(k, 0) for k in LADDER] + [1])

        c.setFont("Helvetica-Bold", 7.2)
        c.setFillColor(MUTE)
        c.drawString(0, self.height - 9, "LEADS WAITING, BY STAGE")
        c.setFont("Helvetica", 7.2)
        c.drawRightString(self.width, self.height - 9, f"{total:,} in total")

        c.setStrokeColor(GRID)
        c.setLineWidth(0.4)
        for frac in (0.25, 0.5, 0.75, 1.0):
            x = label_w + plot_w * frac
            c.line(x, top - self.row * len(LADDER) + 4, x, top + 4)

        for i, key in enumerate(LADDER):
            y = top - (i + 1) * self.row + 4
            v = self.counts.get(key, 0)

            c.setFont("Helvetica", 8.6)
            c.setFillColor(INK)
            c.drawString(0, y + 2, key)

            if v:
                c.setFillColor(LADDER_FILL[i])
                c.roundRect(label_w, y, max(1.8, plot_w * v / peak), 9, 2.5,
                            stroke=0, fill=1)

            c.setFont("Helvetica-Bold", 9)
            c.setFillColor(ZERO if v == 0 else INK)
            c.drawString(label_w + plot_w + 5, y + 2, f"{v:,}")

            if total:
                c.setFont("Helvetica", 7.4)
                c.setFillColor(MUTE)
                c.drawString(label_w + plot_w + 22, y + 2, f"{v / total * 100:.0f}%")

        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.line(label_w, top - self.row * len(LADDER) + 4, label_w, top + 4)


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


def _table(rows, widths, flag_zero=True, bold_first_col=True, total=True, numeric=True):
    """`total` styles the last row as a totals row; `numeric` right-aligns every
    column after the first. A list of records passes False for both."""
    t = Table(rows, colWidths=widths)
    last = len(rows) - 1
    cmds = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.2),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("FONTSIZE", (0, 1), (-1, -1), 8.6),
    ]
    if numeric:
        cmds.append(("ALIGN", (1, 0), (-1, -1), "RIGHT"))
    cmds += [
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
    ]
    if total:
        cmds += [
            ("LINEBELOW", (0, 1), (-1, last - 1), 0.4, LINE),
            ("LINEABOVE", (0, last), (-1, last), 0.9, INK),
            ("FONTNAME", (0, last), (-1, last), "Helvetica-Bold"),
        ]
    else:
        cmds.append(("LINEBELOW", (0, 1), (-1, last), 0.4, LINE))
    cmds += [
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]
    # In the window tables the first data column is today, so it carries the
    # emphasis. The month table has no such column and stays even.
    if bold_first_col:
        cmds.append(("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"))
    if flag_zero:
        for r in range(1, last):
            if rows[r][1] == "0":
                cmds.append(("TEXTCOLOR", (1, r), (1, r), ZERO))
    t.setStyle(TableStyle(cmds))
    return t


# ── shared page furniture, also used by the day-start report ──────────

def _styles():
    """(stylesheet, h1, sub, sec, note) — the type scale every report uses."""
    st = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=st["Title"], fontName="Helvetica-Bold",
                        fontSize=17, leading=20, textColor=INK, alignment=TA_LEFT,
                        spaceAfter=2)
    sub = ParagraphStyle("sub", parent=st["Normal"], fontSize=9.5, leading=13, textColor=MUTE)
    sec = ParagraphStyle("sec", parent=st["Normal"], fontName="Helvetica-Bold",
                         fontSize=10.5, leading=14, textColor=INK, spaceBefore=12, spaceAfter=4)
    note = ParagraphStyle("note", parent=st["Normal"], fontSize=7.8, leading=10.5, textColor=MUTE)
    return st, h1, sub, sec, note


def _doc(out_path, title, company):
    return SimpleDocTemplate(out_path, pagesize=A4,
                             leftMargin=15 * mm, rightMargin=15 * mm,
                             topMargin=13 * mm, bottomMargin=12 * mm,
                             title=f"{title} — {company}", author=company)


def _stale_banner(st, message):
    """White-on-red banner for a snapshot too old to trust. `message` is markup."""
    warn = ParagraphStyle("warn", parent=st["Normal"], fontSize=9, leading=12.5,
                          textColor=colors.white, backColor=ZERO,
                          borderPadding=7, spaceAfter=8)
    return Paragraph(message, warn)


def sync_failed_message(snap, missing: str) -> str:
    """Banner text for a run whose own sync failed. Shared with the day-start."""
    when = f"{snap:%d %b %H:%M IST}" if snap else "never — no sync has ever completed"
    return (f"<b>THE CRM SYNC FOR THIS REPORT FAILED.</b> The figures below come from "
            f"an earlier snapshot, taken {when}, not from data downloaded for this "
            f"report. {missing}")


def stale_message(snap, start, end, sync_ok=True) -> str | None:
    """Banner markup when the figures cannot be trusted as current, else None.

    Two independent routes, and either is enough:

      * the fact: this run's own sync failed (sync_ok False). The job knows this
        for certain, and it holds however recent the previous snapshot is — the
        09:00 sync can succeed and the 20:00 one fail.
      * the backstop: the snapshot is older than the END of the window, less
        SNAPSHOT_GRACE. This covers builds that never tried to sync — the
        --no-sync tools and any manual build — where there is no fact to pass.
    """
    if not sync_ok:
        return sync_failed_message(
            snap, "Anything logged since then is missing, so a low figure may mean "
                  "missing data rather than no work.")
    if snap is None:
        return ("<b>THESE NUMBERS ARE NOT REAL.</b> No sync of the CRM data has ever "
                "completed, so every figure below is empty, not zero.")
    if snap < start:
        return (f"<b>THESE NUMBERS ARE NOT REAL.</b> The CRM data was last refreshed "
                f"{snap:%d %b %H:%M IST}, before this window even began. Every zero below "
                f"means no data was downloaded, not that no work was done.")
    if snap < end - SNAPSHOT_GRACE:
        return (f"<b>THESE NUMBERS ARE INCOMPLETE.</b> The CRM data was last refreshed "
                f"{snap:%d %b %H:%M IST}, partway through this window, which runs to "
                f"{end:%H:%M}. Anything logged after that is missing, so the figures "
                f"below understate the window.")
    return None


def build_pdf(rep: dict, out_path: str, company: str = "Horizon Renewable Power") -> str:
    wins = rep["windows"]
    w1 = wins[0]
    gen = rep["generated_at"]

    st, h1, sub, sec, note = _styles()
    doc = _doc(out_path, "Shift Report", company)

    label = "Overnight" if w1["hours"] > 12 else "Working shift"
    W = [66 * mm, 26 * mm, 26 * mm, 26 * mm]
    story = [
        Paragraph(f"Shift Report — {label}", h1),
        Paragraph(
            f"{company} &nbsp;·&nbsp; <b>{w1['start']:%a %d %b %H:%M}</b> to "
            f"<b>{w1['end']:%H:%M}</b> ({w1['hours']:g} hours). The two columns beside it "
            f"are the same window on the previous two days, so every comparison is like "
            f"for like.", sub),
        Spacer(1, 8),
    ]

    snap = rep.get("snapshot_at")
    stale = stale_message(snap, w1["start"], w1["end"], rep.get("sync_ok", True))
    if stale:
        story.append(_stale_banner(st, stale))

    # headline
    rows = [_hdr(wins)]
    for lbl, key in [("Calls made", "calls"),
                     ("...follow-up calls", "followups"),
                     ("...first contact", "first_contact"),
                     ("Leads touched, any activity", "leads_worked"),
                     ("Deals worked", "deals_worked"),
                     ("...converted this window", "deal_new_worked"),
                     ("...follow-ups", "deal_followups"),
                     ("Proposals sent", "proposals"),
                     ("Deals converted", "deals_converted"),
                     ("New leads", "leads"),
                     ("Surveys", "surveys_scheduled"),
                     ("Client meetings", "meetings_done"),
                     ("Client meetings tomorrow", "meetings_tomorrow"),
                     ("Surveys tomorrow", "surveys_tomorrow")]:
        rows.append([lbl] + [f"{w.get(key, 0):,}" for w in wins])
    rows.append(["", "", "", ""])
    story.append(_table(rows, W))

    # month-by-month first, then the cumulative view
    months = rep.get("ladder_months") or []
    if months:
        story.append(Paragraph("Lead backlog by month of arrival", sec))
        story.append(Paragraph(
            "Newest month first. Each row is that month's intake and how much of it "
            "is still waiting now &mdash; so a row falling over time is the team "
            "clearing that cohort, not fewer leads having arrived. "
            "<b>Arrived</b> is every lead created that month; <b>Waiting</b> is how "
            "many of them still sit in one of the four states.", note))
        story.append(Spacer(1, 4))

        mrows = [["Month", "Not Rated", "Missed 1", "Missed 2", "Missed 3",
                  "Waiting", "Arrived"]]
        tot = {k: 0 for k in LADDER}
        tot_wait = tot_arr = 0
        for mo in reversed(months):          # August down to January
            label = f"{mo['label']} {mo['year']}"
            if not mo.get("covered", True):
                mrows.append([label, "—", "—", "—", "—", "no data", "—"])
                continue
            mrows.append([label] + [f"{mo['counts'].get(k, 0):,}" for k in LADDER]
                         + [f"{mo['stuck']:,}", f"{mo['arrived']:,}"])
            for k in LADDER:
                tot[k] += mo["counts"].get(k, 0)
            tot_wait += mo["stuck"]
            tot_arr += mo["arrived"]
        mrows.append(["Total"] + [f"{tot[k]:,}" for k in LADDER]
                     + [f"{tot_wait:,}", f"{tot_arr:,}"])

        first = 30 * mm
        rest = (180 * mm - first) / 6
        story.append(_table(mrows, [first] + [rest] * 6,
                            flag_zero=False, bold_first_col=False))
        story.append(Spacer(1, 8))

    # the chart — standing backlog, not this window
    lad = rep["ladder"]
    story.append(Paragraph("Lead backlog — all months combined", sec))
    story.append(LadderChart(lad["counts"]))
    story.append(Spacer(1, 4))
    oldest = lad.get("oldest_stuck")
    story.append(Paragraph(
        f"Every lead created since <b>{lad['since']:%d %B %Y}</b> that still sits in one of "
        f"these four states &mdash; <b>{lad['stuck']:,}</b> of the {lad['leads_seen']:,} "
        f"created in that period. Shaded darker as the escalation deepens. "
        + (f"The oldest has been waiting since {oldest:%d %b %Y}. " if oldest else "") +
        "This is the only figure on the page that is not window-scoped: it is the queue "
        "the team has to clear, not a measure of the last few hours.", note))

    # open deals by stage and age — standing pipeline, like the backlog
    aging = (rep.get("deal_aging") or {}).get("stages") or []
    if aging:
        labels = [b[0] for b in DEAL_AGE_BUCKETS]
        arows = [["Stage"] + [f"{l}\ndays" for l in labels]
                 + ["Total", f"Idle\n{DEAL_IDLE_DAYS}d+", "Oldest\n(days)"]]
        tot = {l: 0 for l in labels}
        tot_all = tot_idle = oldest = 0
        for stage, r in aging:
            arows.append([stage] + [f"{r['buckets'][l]:,}" for l in labels]
                         + [f"{r['total']:,}", f"{r['idle']:,}", f"{r['oldest']:,}"])
            for l in labels:
                tot[l] += r["buckets"][l]
            tot_all += r["total"]
            tot_idle += r["idle"]
            oldest = max(oldest, r["oldest"])
        arows.append(["Total"] + [f"{tot[l]:,}" for l in labels]
                     + [f"{tot_all:,}", f"{tot_idle:,}", f"{oldest:,}"])
        first = 34 * mm
        rest = (180 * mm - first) / (len(labels) + 3)
        story.append(KeepTogether([
            Paragraph("Open deals by stage and age", sec),
            Paragraph(
                "Every deal not yet Closed Won or Closed Lost, by its current stage. "
                "<b>Age</b> is days since the deal was created &mdash; the day the lead "
                "converted. The CRM does not record when a deal entered its current "
                "stage, so time-in-stage cannot be shown. "
                f"<b>Idle</b> counts deals with no activity for {DEAL_IDLE_DAYS} days or "
                "more: an old deal that is idle has stalled, an old deal that is not is "
                "still being worked. Not window-scoped &mdash; this is the whole open "
                "pipeline as of this report.", note),
            Spacer(1, 4),
            _table(arows, [first] + [rest] * (len(labels) + 3), flag_zero=False, bold_first_col=False),
        ]))
        story.append(Spacer(1, 8))

    # per-person blocks
    for title, key, unattr in [
        ("Calls made — by person", "calls_by_person", False),
        ("Follow-up calls — by person", "followups_by_person", False),
        ("Deals worked — by person", "deals_worked_by_person", False),
        ("Proposals sent — by person", "proposals_by_person", False),
        ("Deals converted — by person", "deals_by_person", False),
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
            "the team starts logging. Set <b>Meeting Type</b> to <i>Survey</i> or "
            "<i>Client Discussion</i> when creating the meeting; that picklist is what "
            "splits these rows.", note))
    elif any(w.get("meetings_unclassified") for w in wins):
        n = wins[0].get("meetings_unclassified", 0)
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"<b>{n} meeting{'s' if n != 1 else ''} in this window had no Meeting Type "
            f"set</b> and nothing survey-like in the title, so {'they were' if n != 1 else 'it was'} "
            f"counted as {'client meetings' if n != 1 else 'a client meeting'}. Setting the "
            f"picklist when the meeting is created removes the guesswork.", note))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"<b>How to read this.</b> This report is sent once a day, at the end of the "
        f"working day, and covers {w1['start']:%H:%M} to {w1['end']:%H:%M}. Each column is "
        f"that same window on a different day, so the columns compare like with like. "
        f"What has to happen next is in the 09:00 Day Start report, which looks forward "
        f"rather than back. "
        "<b>Where Calls made comes from.</b> It is counted from the Leads module, not "
        "the Calls module. A call is a lead whose last activity falls in this window and "
        "which carries a Latest Call Action — Missed, Interested, Call Back, Not "
        "Interested and so on — which the team sets every time they ring someone. Between "
        "92% and 99% of leads touched on any day carry one. The Calls module is not used, "
        "because on a typical day two thirds of its records sit at Overdue: the call was "
        "scheduled, the call was made, and the record was never closed. Counting it would "
        "have understated the team's work by more than half. "
        "<b>The known limit</b> is that this counts leads, not dials: a lead rung twice in "
        "the same window counts once, so the true number of dials is somewhat higher. "
        "<b>Follow-up</b> means the lead existed before this window; <b>first contact</b> "
        "means it was created inside it. Together they equal Calls made. "
        "<b>Leads touched</b> is every lead worked including those with no call outcome "
        "recorded; the gap between it and Calls made is activity that was not logged as a "
        "call. <b>Deals worked</b> is every open deal whose last activity falls in the "
        "window, matching Zoho's Deals chart on Last Activity Time grouped by Done By. It "
        "is not part of Calls made. It splits into deals <b>converted this window</b> "
        "(a converted lead leaves the Leads module, so that call is counted here and "
        "nowhere else) and <b>follow-ups</b> on deals that already existed. Deals now at "
        "Closed Won or Closed Lost are left out because their last activity is taken to "
        "be the closing. "
        "<b>By person</b> uses Done By on the lead or deal, which holds the most recent "
        "actor — so a lead worked by two people in one window credits only the second. "
        "<b>Proposals by person</b> is exact, and proposals are placed in a window by "
        "record creation time since Proposal Date carries no time of day. "
        "<b>Deals converted</b> counts deals created in the window, credited to Done By. "
        "There is no conversion timestamp in this CRM — Zoho Lead ID is empty on every "
        "deal and Lead Conversion Time holds a number of days rather than a date — so "
        "deal creation is what places a conversion in time. "
        "<b>The backlog chart is the exception</b> to everything above: it counts every "
        "lead since the start date regardless of window, which is why its numbers are so "
        "much larger than the columns.", note))
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        f"Data snapshot: {snap:%d %b %Y %H:%M IST} &nbsp;·&nbsp; generated "
        f"{gen:%d %b %Y %H:%M IST}" if snap else f"generated {gen:%d %b %Y %H:%M IST}", note))

    doc.build(story)
    return out_path


# ── email ─────────────────────────────────────────────────────────────

def _stale_for(rep):
    w1 = rep["windows"][0]
    return stale_message(rep.get("snapshot_at"), w1["start"], w1["end"],
                         rep.get("sync_ok", True))


def _strip_tags(markup):
    return markup.replace("<b>", "").replace("</b>", "")


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
    # The same warning as the PDF banner: the email repeats the headline
    # figures, and is often all anyone reads.
    stale = _stale_for(rep)
    if stale:
        out[3:3] = ["", "WARNING: " + _strip_tags(stale)]
    for lbl, key in [("Calls made", "calls"),
                     ("...follow-up calls", "followups"),
                     ("...first contact", "first_contact"),
                     ("Leads touched", "leads_worked"),
                     ("Deals worked", "deals_worked"),
                     ("...converted this window", "deal_new_worked"),
                     ("...follow-ups", "deal_followups"),
                     ("Proposals sent", "proposals"),
                     ("Deals converted", "deals_converted"),
                     ("New leads", "leads"),
                     ("Surveys", "surveys_scheduled"),
                     ("Client meetings", "meetings_done"),
                     ("Client meetings tomorrow", "meetings_tomorrow"),
                     ("Surveys tomorrow", "surveys_tomorrow")]:
        out.append(f"{lbl:28}" + "".join(f"{w.get(key,0):>14,}" for w in wins))
    lad = rep["ladder"]
    out += ["", f"Lead backlog since {lad['since']:%d %b %Y} "
                f"({lad['stuck']:,} still waiting)", "-" * 70]
    for k in LADDER:
        out.append(f"{k:28}{lad['counts'].get(k, 0):>14,}")
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

    lad = rep["ladder"]
    ladder_rows = "".join(
        f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;">{k}</td>'
        f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;'
        f'text-align:right;font-weight:700;color:'
        f'{"#9c2b28" if lad["counts"].get(k, 0) == 0 else "#1a3c5e"};">'
        f'{lad["counts"].get(k, 0):,}</td></tr>' for k in LADDER)

    stale = _stale_for(rep)
    warning = (f'\n    <p style="background:#9c2b28;color:#fff;padding:10px 12px;font-size:13px;'
               f'margin:0 0 14px;">{stale}</p>' if stale else "")

    return f"""<!DOCTYPE html><html><body style="margin:0;padding:20px;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
<div style="max-width:660px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e2e5ea;">
  <div style="background:#16324f;padding:22px 26px;">
    <h1 style="color:#fff;margin:0;font-size:19px;">Shift Report</h1>
    <p style="color:rgba(255,255,255,.75);margin:5px 0 0;font-size:13px;">
      {config.COMPANY_NAME} &middot; {w1['start']:%a %d %b %H:%M} to {w1['end']:%H:%M} &middot; {w1['hours']:g} hours
    </p>
  </div>
  <div style="padding:18px 22px;">{warning}
    <table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">This window</th>{hdr}</tr>
      {row('Calls made', [w['calls'] for w in wins], True)}
      {row('&hellip;follow-up calls', [w['followups'] for w in wins], True)}
      {row('&hellip;first contact', [w['first_contact'] for w in wins], True)}
      {row('Leads touched', [w['leads_worked'] for w in wins], True)}
      {row('Deals worked', [w['deals_worked'] for w in wins], True)}
      {row('&hellip;converted this window', [w['deal_new_worked'] for w in wins], True)}
      {row('&hellip;follow-ups', [w['deal_followups'] for w in wins], True)}
      {row('Proposals sent', [w['proposals'] for w in wins], True)}
      {row('Deals converted', [w['deals_converted'] for w in wins], True)}
      {row('New leads', [w['leads'] for w in wins], True)}
      {row('Surveys', [w['surveys_scheduled'] for w in wins], True)}
      {row('Client meetings', [w['meetings_done'] for w in wins], True)}
      {row('Client meetings tomorrow', [w['meetings_tomorrow'] for w in wins], True)}
      {row('Surveys tomorrow', [w['surveys_tomorrow'] for w in wins], True)}
    </table>
    <h3 style="color:#16324f;font-size:13px;margin:18px 0 8px;">
      Lead backlog since {lad['since']:%d %b %Y} &mdash; {lad['stuck']:,} still waiting
    </h3>
    <table style="width:100%;border-collapse:collapse;">
      <tr><th style="text-align:left;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">Stage</th>
          <th style="text-align:right;font-size:11px;color:#888;padding:0 10px 6px;border-bottom:2px solid #16324f;">Leads</th></tr>
      {ladder_rows}
    </table>
    <p style="font-size:12px;color:#666;margin:16px 0 0;">
      The chart and the per-person breakdowns are in the attached PDF.
    </p>
    <p style="font-size:11px;color:#999;margin:10px 0 0;">
      This report covers {w1['start']:%H:%M} to {w1['end']:%H:%M}, and each column is that
      same window on a different day. What has to happen next is in the 09:00 Day Start
      report.
    </p>
  </div>
</div></body></html>"""


def _refresh_snapshot(report: str) -> Exception | None:
    """Sync before building. Returns None on success, or the error.

    Never raises. A failed sync is logged loudly and the report is built from
    the previous snapshot with sync_ok=False, so its banner says this run's sync
    failed. The caller raises SyncFailed once the report has been sent.
    """
    try:
        import crm_sync
        crm_sync.sync(days=settings.SYNC_DAYS)
        return None
    except Exception as exc:
        log.error("CRM SYNC FAILED (%s) — building the %s from the previous snapshot. "
                  "It will still be sent; this run will be recorded as failed.", exc, report)
        return exc


def _mail_report(name, subject, text_body, html_body, pdf_path) -> str | None:
    """Send one report to RECIPIENTS. Returns the PDF path, or None on failure."""
    try:
        send_mail(subject=subject, text_body=text_body, html_body=html_body,
                  to=config.RECIPIENTS, attachment=pdf_path)
        log.info("%s sent to %s", name, config.RECIPIENTS)
        return pdf_path
    except MailError as exc:
        log.error("%s email failed: %s", name, exc)
        return None


def send_shift_report(now=None) -> str | None:
    """Sync, build and send. Returns the PDF path, or None if the email failed.

    A failed sync does not stop the report: it is built from the previous
    snapshot, its banner says this run's sync failed, and it is sent. SyncFailed
    is raised afterwards so the scheduler records the run as failed.
    """
    sync_error = _refresh_snapshot("shift report")

    rep = build_shift_data(DB_PATH, now=now, sync_ok=sync_error is None)
    w1 = rep["windows"][0]
    tag = "overnight" if w1["hours"] > 12 else "shift"

    os.makedirs(OUT_DIR, exist_ok=True)
    pdf_path = os.path.join(
        OUT_DIR, f"HRP_Shift_{w1['end']:%Y-%m-%d_%H%M}_{tag}.pdf")
    build_pdf(rep, pdf_path, company=config.COMPANY_NAME)

    sent = _mail_report(
        "Shift report",
        subject=(f"Shift Report {w1['start']:%H:%M}-{w1['end']:%H:%M} "
                 f"- {w1['end']:%d %b %Y} | {config.COMPANY_NAME}"),
        text_body=_body_text(rep), html_body=_body_html(rep), pdf_path=pdf_path)

    if sync_error is not None:
        raise SyncFailed("shift report", sync_error, bool(sent)) from sync_error
    return sent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    send_shift_report()