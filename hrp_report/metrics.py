"""
hrp_report.metrics — three-day comparison metrics for the CEO PDF.

Reads the local SQLite snapshot (records: module, id, payload) that crm_sync.py
already maintains. All figures are computed for COMPLETE days only: the report
runs at 08:00 IST, so "today" has almost no data and is deliberately excluded.

Day 1 = yesterday, Day 2 = two days ago, Day 3 = three days ago.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

import pytz

IST = pytz.timezone("Asia/Kolkata")

PROPOSAL_STAGES = {"proposal/price quote", "proposal", "price quote", "quotation"}
WON_STAGES = {"closed won"}
LOST_STAGES = {"closed lost", "closed lost to competition"}

# Capacity(kW) on Leads/Deals is free text, so values are parsed defensively
# ("5", "5 kW", "5kwp", ""). Proposals carries a proper numeric Plant_Capacity,
# which is preferred. Anything unparseable is reported as "Not recorded".
#
# A call record only means a conversation happened when its status says so.
# 43% of call records in this CRM are scheduled calls that never took place.
CALL_COMPLETED = {
    "scheduled attended", "scheduled attended delay",
    "attended dialled", "received",
}
CALL_MISSED = {"overdue", "missed", "unattended dialled", "cancelled"}

# Off-roll staff share a single Zoho login, so the record Owner is meaningless
# for per-person reporting. The real actor is a picklist: Done_By on Leads and
# Deals, Proposal_Sender_Name on Proposals. Those three lists were maintained
# separately and spell the same people differently, so they are normalised here.
PERSON_ALIASES = {
    "abid khan": "Aabid Khan",
    "aabid khan": "Aabid Khan",
    "neelam": "Neelam Kumari",
    "neelam kumari": "Neelam Kumari",
    "abhishek jha": "Abhishek Kumar",
    "abhishek kumar": "Abhishek Kumar",
    "siddhartha saurabh": "Siddharth Sourabh",
    "siddharth sourabh": "Siddharth Sourabh",
}


def normalise_person(name):
    v = (name or "").strip()
    if not v or v == "-None-":
        return "Not recorded"
    return PERSON_ALIASES.get(v.lower(), v)


CAPACITY_BUCKETS = [
    (0, 3, "Under 3 kW"),
    (3, 5, "3 – 5 kW"),
    (5, 10, "5 – 10 kW"),
    (10, 25, "10 – 25 kW"),
    (25, 100, "25 – 100 kW"),
    (100, float("inf"), "Over 100 kW"),
]


# ── parsing helpers ───────────────────────────────────────────

def _dt(value):
    """Zoho datetime string -> tz-aware IST datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        try:
            d = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = IST.localize(d)
    return d.astimezone(IST)


def _day(value):
    d = _dt(value)
    return d.date() if d else None


def _owner(rec):
    o = rec.get("Owner") or {}
    return o.get("name") or "Unassigned"


def _capacity_kw(rec):
    """Best-effort kW from the free-text Capacity_kW / integer Project_Size_kW."""
    for field in ("Plant_Capacity", "Project_Size_kW", "Capacity_kW", "Sanction_Load"):
        raw = rec.get(field)
        if raw in (None, "", "-None-"):
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
        digits = ""
        for ch in str(raw).replace(",", "").strip():
            if ch.isdigit() or (ch == "." and "." not in digits):
                digits += ch
            elif digits:
                break
        if digits:
            try:
                val = float(digits)
                if val > 0:
                    return val
            except ValueError:
                pass
    return None


def _capacity_bucket(rec):
    kw = _capacity_kw(rec)
    if kw is None:
        return "Not recorded"
    for lo, hi, label in CAPACITY_BUCKETS:
        if lo <= kw < hi:
            return label
    return "Not recorded"


def _link_id(call):
    """Id of the record a call is attached to.

    Zoho puts the Lead on What_Id (with $se_module == 'Leads') for these records,
    NOT on Who_Id — Who_Id is null on ~91% of calls here. Checking only Who_Id
    makes call linkage look far worse than it is.
    """
    for field in ("What_Id", "Who_Id"):
        v = call.get(field)
        if isinstance(v, dict) and v.get("id"):
            return v["id"]
        if isinstance(v, str) and v:
            return v
    return None


# ── data loading ──────────────────────────────────────────────

def load_records(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        out = defaultdict(list)
        for module, payload in cur.execute("SELECT module, payload FROM records"):
            try:
                out[module].append(json.loads(payload))
            except (ValueError, TypeError):
                continue
        return dict(out)
    finally:
        conn.close()


def snapshot_time(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT MAX(synced_at) FROM sync_log").fetchone()
        return _dt(row[0]) if row and row[0] else None
    finally:
        conn.close()


# ── per-day computation ───────────────────────────────────────

def _index(data):
    """Pre-index everything once so each day is a cheap lookup."""
    leads = data.get("Leads", [])
    calls = data.get("Calls", [])
    deals = data.get("Deals", [])
    proposals = data.get("Proposals", [])

    idx = {
        "leads_by_day": defaultdict(list),
        "calls_by_day": defaultdict(list),
        "deals_by_day": defaultdict(list),
        "deal_mod_by_day": defaultdict(list),
        "proposals_by_day": defaultdict(list),
        "lead_created": {},
        "actor_by_record": {},
        "first_call_at": {},
        "lead_status": {},
        "leads": leads,
        "calls": calls,
        "deals": deals,
        "proposals": proposals,
    }

    for l in leads:
        d = _day(l.get("Created_Time"))
        if d:
            idx["leads_by_day"][d].append(l)
            idx["lead_created"][l.get("id")] = d
        idx["lead_status"][l.get("id")] = (l.get("Lead_Status") or "").strip()

    # Who actually worked each record, and when it was last touched. Used to
    # attribute calls to a person, because the Call's Owner is a shared login.
    for rec in list(leads) + list(deals):
        actor = normalise_person(rec.get("Done_By"))
        if actor != "Not recorded":
            idx["actor_by_record"][rec.get("id")] = (actor, _day(rec.get("Modified_Time")))

    for c in calls:
        when = _dt(c.get("Call_Start_Time")) or _dt(c.get("Created_Time"))
        if not when:
            continue
        idx["calls_by_day"][when.date()].append(c)
        status = (c.get("Call_Status") or "").strip().lower()
        happened = status in CALL_COMPLETED or (c.get("Call_Duration_in_seconds") or 0) > 0
        target = _link_id(c) if happened else None
        if target:
            prev = idx["first_call_at"].get(target)
            if prev is None or when < prev:
                idx["first_call_at"][target] = when

    for dl in deals:
        d = _day(dl.get("Created_Time"))
        if d:
            idx["deals_by_day"][d].append(dl)
        m = _day(dl.get("Modified_Time"))
        if m:
            idx["deal_mod_by_day"][m].append(dl)
    for pr in proposals:
        # Date_1 is the Proposal Date; fall back to record creation.
        p = _day(pr.get("Date_1")) or _day(pr.get("Created_Time"))
        if p:
            idx["proposals_by_day"][p].append(pr)

    return idx


def _never_called_backlog(idx, upto: date) -> int:
    """Leads created on/before `upto` with no call linked on/before `upto`."""
    n = 0
    for lead_id, created in idx["lead_created"].items():
        if created > upto:
            continue
        fc = idx["first_call_at"].get(lead_id)
        if fc is None or fc.date() > upto:
            n += 1
    return n


def day_metrics(idx, day: date) -> dict:
    leads = idx["leads_by_day"].get(day, [])
    calls = idx["calls_by_day"].get(day, [])
    new_deals = idx["deals_by_day"].get(day, [])
    touched = idx["deal_mod_by_day"].get(day, [])

    lead_ids = {l.get("id") for l in leads}

    # first response, in hours, for leads created that day
    responses = []
    for l in leads:
        fc = idx["first_call_at"].get(l.get("id"))
        c = _dt(l.get("Created_Time"))
        if fc and c and fc >= c:
            responses.append((fc - c).total_seconds() / 3600.0)

    contacted_same_day = sum(
        1 for l in leads
        if (fc := idx["first_call_at"].get(l.get("id"))) and fc.date() == day
    )

    linked = sum(1 for c in calls if _link_id(c))

    def _status(c):
        return (c.get("Call_Status") or "").strip().lower()

    completed = [
        c for c in calls
        if _status(c) in CALL_COMPLETED or (c.get("Call_Duration_in_seconds") or 0) > 0
    ]
    missed = [c for c in calls if _status(c) in CALL_MISSED
              and not (c.get("Call_Duration_in_seconds") or 0) > 0]

    won = [d for d in touched if (d.get("Stage") or "").lower() in WON_STAGES]
    lost = [d for d in touched if (d.get("Stage") or "").lower() in LOST_STAGES]
    proposals = [d for d in touched if (d.get("Stage") or "").lower() in PROPOSAL_STAGES]

    junk_now = sum(1 for l in leads if idx["lead_status"].get(l.get("id"), "").lower() == "junk lead")

    # ── deal-stage activity ────────────────────────────────────────────
    # Once a lead converts it leaves the Leads module, but the team keeps
    # calling, negotiating and re-quoting. None of that shows in lead metrics.
    deal_calls = [c for c in completed if (c.get("$se_module") or "") == "Deals"]
    lead_calls = [c for c in completed if (c.get("$se_module") or "") == "Leads"]
    deals_actioned = [
        d for d in idx["deals"]
        if _day(d.get("Modified_Time")) == day
        and normalise_person(d.get("Done_By")) != "Not recorded"
    ]

    # ── attribute calls to a person via the record they are attached to ──
    # The Call's Owner is a shared login. The linked Lead/Deal carries Done_By.
    # Only trusted when that record was touched the same day as the call,
    # because Done_By holds the most recent actor and drifts over time.
    call_person = defaultdict(int)
    for c in completed:
        rec = idx["actor_by_record"].get(_link_id(c))
        if rec and rec[1] == day:
            call_person[rec[0]] += 1
        else:
            call_person["Not attributable"] += 1
    calls_by_person = dict(sorted(call_person.items(), key=lambda kv: (-kv[1], kv[0])))

    # ── proposals: the real Date_of_Proposal field, not a stage proxy ──
    proposals = idx["proposals_by_day"].get(day, [])

    def tally(items, keyfn):
        c = defaultdict(int)
        for it in items:
            c[keyfn(it)] += 1
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    def _sender(p):
        return normalise_person(p.get("Proposal_Sender_Name"))

    # Leads whose recorded actor last touched them on this day. This is a proxy
    # for per-person activity, NOT a call count: Done_By holds only the most
    # recent actor, so a lead worked by two people in one day shows only the
    # second, and repeat touches collapse to one.
    actioned = [
        l for l in idx["leads"]
        if _day(l.get("Modified_Time")) == day and normalise_person(l.get("Done_By")) != "Not recorded"
    ]

    breakdowns = {
        "proposals_by_user": tally(proposals, _sender),
        "calls_by_user": tally(completed, _owner),
        "leads_actioned_by_person": tally(actioned, lambda l: normalise_person(l.get("Done_By"))),
        "deals_actioned_by_person": tally(deals_actioned, lambda d: normalise_person(d.get("Done_By"))),
        "deal_latest_action": tally(deals_actioned, lambda d: (d.get("Latest_Call_Action") or "Not set").strip() or "Not set"),
        "calls_by_person": calls_by_person,
        "latest_call_action": tally(actioned, lambda l: (l.get("Latest_Call_Action") or "Not set").strip() or "Not set"),
        "leads_by_status": tally(leads, lambda l: (l.get("Lead_Status") or "Not set").strip() or "Not set"),
        "leads_by_source": tally(leads, lambda l: (l.get("Lead_Source") or "Not set").strip() or "Not set"),
        "leads_by_state": tally(leads, lambda l: (l.get("State") or "Not set").strip() or "Not set"),
        # capacity is captured on the Deal, not the Lead — on Leads it is
        # almost never filled in, so bucketing leads produces an all-zero table.
        "deals_by_capacity": tally(new_deals, _capacity_bucket),
        "proposals_by_capacity": tally(proposals, _capacity_bucket),
    }

    return {
        "proposals_sent": len(proposals),
        # EPC_Cost_kWp is mislabelled in the API: its field label is
        # "EPC Cost with Taxes" and it holds the TOTAL quoted value, not a
        # per-kWp rate. Bill_Amount is the customer's monthly electricity bill
        # and must never be used here.
        "proposal_value": sum(float(p.get("EPC_Cost_kWp") or 0) for p in proposals),
        "proposal_bill_total": sum(float(p.get("Bill_Amount") or 0) for p in proposals),
        "proposal_kw": sum(k for p in proposals if (k := _capacity_kw(p))),
        **breakdowns,
        "date": day,
        "new_leads": len(leads),
        "calls_logged": len(calls),
        "deal_calls": len(deal_calls),
        "lead_calls": len(lead_calls),
        "deals_actioned": len(deals_actioned),
        "calls": len(completed),
        "calls_missed": len(missed),
        "call_completion_pct": round(len(completed) / len(calls) * 100, 1) if calls else None,
        "outbound_calls": sum(1 for c in completed if (c.get("Call_Type") or "") == "Outbound"),
        "calls_linked_pct": round(linked / len(calls) * 100, 1) if calls else None,
        "leads_contacted_same_day": contacted_same_day,
        "same_day_contact_pct": round(contacted_same_day / len(leads) * 100, 1) if leads else None,
        "median_first_response_h": round(statistics.median(responses), 1) if responses else None,
        "calls_per_lead": round(len(completed) / len(leads), 1) if leads else None,
        "new_deals": len(new_deals),
        "deals_in_proposal_touched": len(proposals),
        "deals_won": len(won),
        "won_value": sum(float(d.get("Amount") or 0) for d in won),
        "deals_lost": len(lost),
        "junk_of_days_leads": junk_now,
        "junk_pct_of_days_leads": round(junk_now / len(leads) * 100, 1) if leads else None,
        "never_called_backlog": _never_called_backlog(idx, day),
    }


def pipeline_snapshot(idx) -> dict:
    open_deals = [
        d for d in idx["deals"]
        if (d.get("Stage") or "").lower() not in WON_STAGES | LOST_STAGES
    ]
    in_proposal = [d for d in open_deals if (d.get("Stage") or "").lower() in PROPOSAL_STAGES]
    now = datetime.now(IST)
    stalled = [
        d for d in open_deals
        if (m := _dt(d.get("Modified_Time"))) and (now - m).days >= 14
    ]
    no_amount = [d for d in open_deals if not d.get("Amount")]
    today = datetime.now(IST).date()
    followup_set = [d for d in open_deals if d.get("Next_Follow_Up")]
    followup_overdue = [
        d for d in followup_set
        if (f := _day(d.get("Next_Follow_Up"))) and f < today
    ]
    priced = [d for d in open_deals if d.get("Amount")]
    return {
        "open_deals": len(open_deals),
        "open_value": sum(float(d.get("Amount") or 0) for d in open_deals),
        "open_value_from": len(priced),
        "followup_set": len(followup_set),
        "followup_overdue": len(followup_overdue),
        "followup_missing": len(open_deals) - len(followup_set),
        "in_proposal": len(in_proposal),
        "stalled_14d": len(stalled),
        "open_without_amount": len(no_amount),
        "open_without_amount_pct": round(len(no_amount) / len(open_deals) * 100, 1) if open_deals else None,
    }


def build_report_data(db_path: str, as_of: date | None = None) -> dict:
    """Return the three complete days ending yesterday, plus a pipeline snapshot."""
    data = load_records(db_path)
    idx = _index(data)

    if as_of is None:
        # Default stays YESTERDAY, which is what the 08:00 morning report needs.
        # The 18:30 evening report passes as_of=today explicitly.
        as_of = datetime.now(IST).date() - timedelta(days=1)

    days = [as_of, as_of - timedelta(days=1), as_of - timedelta(days=2)]
    return {
        "days": [day_metrics(idx, d) for d in days],
        "pipeline": pipeline_snapshot(idx),
        "snapshot_at": snapshot_time(db_path),
        "generated_at": datetime.now(IST),
    }
