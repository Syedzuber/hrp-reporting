"""
make_env.py — turn your existing config.py into the Railway variable block.

Run it once, paste the output into Railway's Raw Editor. This exists so you
never retype a refresh token by hand, which is where the typos come from.

    python make_env.py                 # print the block (secrets visible)
    python make_env.py --masked        # print it with secrets hidden, to check
                                       # the shape before revealing anything
    python make_env.py --out .env      # write a local .env instead

The output contains live credentials. Paste it into Railway, then close the
window. Never commit it, never paste it into a chat or a ticket.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import config
except ImportError:
    raise SystemExit(
        "config.py not found in this folder.\n\n"
        "Copy it in temporarily, run this, then delete it again:\n"
        '  copy "D:\\HRP\\CEO Dashboard\\hrp_ceo_dashboard_2\\hrp_dashboard\\config.py" .\n'
    )

MASK = "--masked" in sys.argv
SECRET_KEYS = {"CLIENT_SECRET", "REFRESH_TOKEN", "SMTP_PASSWORD", "ZEPTO_TOKEN"}


def val(name, default=""):
    v = getattr(config, name, default)
    if isinstance(v, (list, tuple)):
        v = ",".join(str(x) for x in v)
    v = "" if v is None else str(v)
    if MASK and name in SECRET_KEYS and v:
        v = v[:4] + "…" + v[-4:] + f"  ({len(v)} chars)"
    return v


def derive_alert_to():
    """Operator addresses only — anything not on the company domain."""
    explicit = getattr(config, "ALERT_TO", None)
    if explicit:
        return ",".join(explicit) if isinstance(explicit, (list, tuple)) else str(explicit)
    sender = str(getattr(config, "SMTP_FROM", "") or "")
    domain = sender.split("@")[-1].lower()
    outside = [r for r in getattr(config, "RECIPIENTS", [])
               if domain and not r.lower().endswith("@" + domain)]
    return ",".join(outside)


lines = [
    "# ── Zoho CRM ──",
    f"CLIENT_ID={val('CLIENT_ID')}",
    f"CLIENT_SECRET={val('CLIENT_SECRET')}",
    f"REFRESH_TOKEN={val('REFRESH_TOKEN')}",
    f"ZOHO_TOKEN_URL={val('ZOHO_TOKEN_URL', 'https://accounts.zoho.in/oauth/v2/token')}",
    f"ZOHO_CRM_BASE={val('ZOHO_CRM_BASE', 'https://www.zohoapis.in/crm/v2')}",
    "",
    "# ── Company and delivery ──",
    f"COMPANY_NAME={val('COMPANY_NAME', 'Horizon Renewable Power')}",
    f"SMTP_FROM={val('SMTP_FROM')}",
    f"RECIPIENTS={val('RECIPIENTS')}",
    f"ALERT_TO={derive_alert_to()}",
    "",
    "# ── Mail transport (API, because Railway blocks SMTP on Hobby) ──",
    "MAIL_TRANSPORT=api",
    f"ZEPTO_TOKEN={val('SMTP_PASSWORD')}",
    "ZEPTO_API_URL=https://api.zeptomail.in/v1.1/email",
    "",
    "# ── Storage (must match the Railway volume mount path) ──",
    "DATA_DIR=/data",
    "REPORTS_DIR=/data/reports",
    "",
    "# ── Schedules, in IST ──",
    "TIMEZONE=Asia/Kolkata",
    f"MORNING_AT={val('MORNING_AT', '08:00')}",
    "EVENING_AT=18:30",
    "HEALTHCHECK_AT=21:00",
    "MORNING_ENABLED=true",
    "EVENING_ENABLED=true",
    "HEALTHCHECK_ENABLED=true",
]
block = "\n".join(lines)

out = None
for i, a in enumerate(sys.argv):
    if a == "--out" and i + 1 < len(sys.argv):
        out = sys.argv[i + 1]

if out:
    if MASK:
        raise SystemExit("Refusing to write a masked file — it would not work.")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(block + "\n")
    print(f"Written to {out}. This file contains live credentials — it is in .gitignore.")
else:
    print(block)
    print()
    if MASK:
        print("Masked view. Re-run without --masked to get the real values.")
    else:
        print("^ Paste the block above into Railway → Variables → Raw Editor.")

missing = [k for k in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN",
                       "SMTP_FROM", "SMTP_PASSWORD") if not getattr(config, k, None)]
if missing:
    print("\nWARNING — these are empty in config.py and will break the service:")
    for k in missing:
        print("  -", k)
