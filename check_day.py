"""
check_day.py — reconcile the report against a Zoho chart for one date.

The report's comparison columns are the SAME CLOCK WINDOW on earlier days, not
whole days. A Zoho chart filtered on "Last Activity Time: Yesterday" is a whole
day. Those two numbers are supposed to differ, and this prints both so you can
see which one a disagreement is actually about.

    python check_day.py 2026-08-25
    python check_day.py 2026-08-25 --window 11:00-20:00

No sync, no email — it only reads the local snapshot.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, time

import pytz

import settings
from hrp_report.metrics import _dt, load_records, normalise_person, snapshot_time

IST = pytz.timezone(settings.TIMEZONE)

CALL_ATTENDED = {"scheduled attended", "scheduled attended delay",
                 "attended dialled", "received"}


def activity_at(rec):
    return _dt(rec.get("Last_Activity_Time")) or _dt(rec.get("Modified_Time"))


def show(title, leads, calls):
    by = Counter(normalise_person(l.get("Done_By")) for l in leads)
    att = sum(1 for c in calls
              if (c.get("Call_Status") or "").strip().lower() in CALL_ATTENDED)
    print()
    print(title)
    print("-" * len(title))
    print(f"  Leads worked        {len(leads):>6,}")
    for name, n in by.most_common():
        print(f"      {name:<26}{n:>6,}")
    print(f"  Call records        {len(calls):>6,}")
    print(f"  ...attended         {att:>6,}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("--window", default=None,
                    help="HH:MM-HH:MM slice to also show, e.g. 09:00-20:00")
    ap.add_argument("--pdf", nargs="?", const="auto", default=None,
                    help="also build the shift PDF for this date. Covers the "
                         "--window span if given, otherwise the whole day. "
                         "Optionally give a file path.")
    args = ap.parse_args()

    try:
        day = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        sys.exit("Date must be YYYY-MM-DD.")

    snap = snapshot_time(settings.DB_PATH)
    print(f"Snapshot taken: {snap}")
    print(f"Database:       {settings.DB_PATH}")

    data = load_records(settings.DB_PATH)
    leads, calls = data.get("Leads", []), data.get("Calls", [])

    have_laa = sum(1 for l in leads if l.get("Last_Activity_Time"))
    print(f"Leads with Last_Activity_Time: {have_laa:,} of {len(leads):,}")
    if have_laa == 0:
        print("  ** None. The sync has not run since the field was added, so")
        print("  ** these numbers fall back to Modified_Time and will not")
        print("  ** match Zoho exactly. Run a full sync first.")

    def call_when(c):
        return _dt(c.get("Call_Start_Time")) or _dt(c.get("Created_Time"))

    # whole day
    d_leads = [l for l in leads
               if (w := activity_at(l)) and w.astimezone(IST).date() == day]
    d_calls = [c for c in calls
               if (w := call_when(c)) and w.astimezone(IST).date() == day]
    show(f"WHOLE DAY {day:%a %d %b %Y}  — compare this with a Zoho chart "
         f"filtered on Last Activity Time", d_leads, d_calls)

    if args.window:
        try:
            a, b = args.window.split("-")
            t1 = time(*[int(x) for x in a.split(":")[:2]])
            t2 = time(*[int(x) for x in b.split(":")[:2]])
        except Exception:
            sys.exit("--window must look like 11:00-20:00")
        start = IST.localize(datetime.combine(day, t1))
        end = IST.localize(datetime.combine(day, t2))
        w_leads = [l for l in leads
                   if (w := activity_at(l)) and start <= w < end]
        w_calls = [c for c in calls
                   if (w := call_when(c)) and start <= w < end]
        show(f"WINDOW {t1:%H:%M}-{t2:%H:%M} on {day:%a %d %b}  — this is what "
             f"the shift report's column shows", w_leads, w_calls)
        if d_leads:
            pct = len(w_leads) / len(d_leads) * 100
            print(f"\n  The window holds {pct:.0f}% of the day's leads worked.")

    if args.pdf:
        from hrp_report import shift as shift_mod
        if args.window:
            p_start = IST.localize(datetime.combine(day, t1))
            p_end = IST.localize(datetime.combine(day, t2))
            tag = f"{t1:%H%M}-{t2:%H%M}"
        else:
            p_start = IST.localize(datetime.combine(day, time(0, 0)))
            p_end = p_start + __import__("datetime").timedelta(days=1)
            tag = "fullday"
        rep = shift_mod.build_shift_data(settings.DB_PATH, now=p_end,
                                         start=p_start)
        out = (args.pdf if args.pdf != "auto"
               else os.path.join(settings.REPORTS_DIR or ".",
                                 f"HRP_Shift_{day:%Y-%m-%d}_{tag}.pdf"))
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        shift_mod.build_pdf(rep, out)
        print(f"\nPDF written: {out}")
        print("  Comparison columns are the same span on the two previous days.")

    print()


if __name__ == "__main__":
    main()