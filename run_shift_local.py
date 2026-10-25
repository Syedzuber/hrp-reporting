"""
run_shift_local.py — build (and optionally send) the shift report by hand.

Put this beside scheduler.py and run it from that folder.

    python run_shift_local.py                 sync, build the PDF, send nothing
    python run_shift_local.py --send          same, then email the CEO list
    python run_shift_local.py --no-sync       build from the existing snapshot
    python run_shift_local.py --at 20:00      pretend it is 20:00 today
    python run_shift_local.py --at 2026-08-25T20:00

Default is to build WITHOUT sending, because the common mistake is firing a
report at the CEO before looking at it.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import pytz

import settings
from hrp_report import shift

IST = pytz.timezone(settings.TIMEZONE)


def parse_when(raw: str | None) -> datetime:
    now = datetime.now(IST)
    if not raw:
        return now
    raw = raw.strip()
    try:
        if "T" in raw or " " in raw:
            naive = datetime.fromisoformat(raw.replace(" ", "T"))
        else:                                   # bare HH:MM — today
            h, m = raw.split(":")[:2]
            naive = now.replace(hour=int(h), minute=int(m),
                                second=0, microsecond=0).replace(tzinfo=None)
        return IST.localize(naive)
    except Exception:
        sys.exit(f"Could not read --at {raw!r}. Use HH:MM or YYYY-MM-DDTHH:MM.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true",
                    help="email it to RECIPIENTS. Off by default.")
    ap.add_argument("--no-sync", action="store_true",
                    help="skip the Zoho pull and use the snapshot as it stands.")
    ap.add_argument("--at", help="the moment the window ends. Default: now.")
    ap.add_argument("--days", type=int, default=None,
                    help="how many days to sync. Default: SYNC_DAYS.")
    args = ap.parse_args()

    when = parse_when(args.at)
    print(f"Config: {settings.summary()}")
    print(f"Window ends: {when:%a %d %b %Y %H:%M %Z}")

    if args.no_sync:
        print("Skipping sync — numbers will be as stale as the snapshot.")
    else:
        import crm_sync
        days = args.days or settings.SYNC_DAYS
        print(f"Syncing Zoho, {days} days. This is the slow part.")
        crm_sync.sync(days=days)

    rep = shift.build_shift_data(settings.DB_PATH, now=when)

    w = rep["windows"][0]
    print()
    print(f"  {'':28}{'this':>10}{'prev':>10}{'prev-1':>10}")
    for label, key in [("Leads worked", "leads_worked"),
                       ("Calls made", "calls"),
                       ("Follow-up calls", "followups"),
                       ("Proposals sent", "proposals"),
                       ("New leads", "leads")]:
        vals = "".join(f"{x.get(key, 0):>10,}" for x in rep["windows"])
        print(f"  {label:28}{vals}")
    print()
    print("  Leads worked by person:")
    for name, n in sorted(w["leads_worked_by_person"].items(),
                          key=lambda kv: -kv[1]):
        print(f"    {name:28}{n:>6,}")

    os.makedirs(settings.REPORTS_DIR, exist_ok=True)
    out = os.path.join(settings.REPORTS_DIR,
                       f"HRP_Shift_{when:%Y-%m-%d_%H%M}.pdf")
    shift.build_pdf(rep, out)
    print(f"\nPDF written: {out}")

    if args.send:
        # Deliberately not calling send_shift_report(), which would sync a
        # second time and rebuild. This mails the exact PDF printed above.
        from mailer import MailError, send as send_mail
        print(f"Sending to: {', '.join(settings.RECIPIENTS)}")
        try:
            send_mail(
                subject=(f"Shift Report {w['start']:%H:%M}-{w['end']:%H:%M} "
                         f"- {w['end']:%d %b %Y} | {settings.COMPANY_NAME}"),
                text_body=shift._body_text(rep),
                html_body=shift._body_html(rep),
                to=settings.RECIPIENTS, attachment=out)
            print("Sent.")
        except MailError as exc:
            sys.exit(f"Email failed: {exc}")
    else:
        print("Nothing was emailed. Add --send when the numbers look right.")


if __name__ == "__main__":
    main()