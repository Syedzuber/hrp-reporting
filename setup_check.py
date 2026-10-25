"""
setup_check.py — verify a machine can build and send the reports.

Run this first on a new laptop. It checks everything in one pass and prints a
to-do list, instead of surfacing one ModuleNotFoundError per run.

    python setup_check.py

It changes nothing, sends nothing, and prints no secrets — only whether each
one is present.
"""

from __future__ import annotations

import importlib
import os
import sys

OK, BAD, WARN = "  OK  ", " MISS ", " WARN "
problems: list[str] = []


def line(state, text, detail=""):
    print(f"[{state}] {text}" + (f"   {detail}" if detail else ""))


# ── 1. Python ─────────────────────────────────────────────────────────
print("\n=== Python ===")
v = sys.version_info
line(OK if v >= (3, 9) else BAD, f"Python {v.major}.{v.minor}.{v.micro}",
     sys.executable)
if v < (3, 9):
    problems.append("Install Python 3.9 or newer.")

# ── 2. Packages ───────────────────────────────────────────────────────
print("\n=== Packages ===")
PKGS = {"pytz": "pytz", "requests": "requests", "reportlab": "reportlab",
        "apscheduler": "APScheduler"}
missing = []
for mod, pipname in PKGS.items():
    try:
        importlib.import_module(mod)
        line(OK, pipname)
    except ImportError:
        line(BAD, pipname)
        missing.append(pipname)
if missing:
    problems.append("python -m pip install " + " ".join(missing))

# ── 3. Files ──────────────────────────────────────────────────────────
print("\n=== Files ===")
HERE = os.path.dirname(os.path.abspath(__file__))
NEEDED = ["settings.py", "mailer.py", "crm_sync.py",
          os.path.join("hrp_report", "__init__.py"),
          os.path.join("hrp_report", "shift.py"),
          os.path.join("hrp_report", "metrics.py"),
          os.path.join("hrp_report", "pdf_report.py")]
for rel in NEEDED:
    p = os.path.join(HERE, rel)
    if not os.path.exists(p):
        line(BAD, rel)
        problems.append(f"Copy {rel} into this folder.")
    elif os.path.getsize(p) == 0 and os.path.basename(p) != "__init__.py":
        line(BAD, rel, "0 bytes — the copy failed")
        problems.append(f"{rel} is empty. Re-copy it.")
    elif os.path.getsize(p) == 0:
        # __init__.py only marks the folder as a package. Empty is correct.
        line(OK, rel, "empty — correct for a package marker")
    else:
        line(OK, rel, f"{os.path.getsize(p):,} bytes")

# ── 4. Credentials ────────────────────────────────────────────────────
print("\n=== Credentials ===")
if not os.path.exists(os.path.join(HERE, "settings.py")):
    print("  Skipped — settings.py is not here yet.")
else:
    sys.path.insert(0, HERE)
    try:
        import settings
    except Exception as exc:
        line(BAD, "settings.py failed to import", str(exc)[:120])
        problems.append("Fix the settings.py import error above.")
        settings = None

    if settings:
        has_config = os.path.exists(os.path.join(HERE, "config.py"))
        line(OK if has_config else WARN, "config.py present" if has_config
             else "config.py absent — relying on environment variables")

        for name in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN",
                     "SMTP_FROM", "ZEPTO_TOKEN"):
            val = getattr(settings, name, None)
            line(OK if val else BAD, name, "set" if val else "not set")
            if not val:
                problems.append(f"{name} is not set. It comes from config.py "
                                f"on your old laptop, or from Railway Variables.")

        rec = getattr(settings, "RECIPIENTS", []) or []
        line(OK if rec else WARN, f"RECIPIENTS: {len(rec)} address(es)")

        # ── 5. Zoho reachable? ────────────────────────────────────────
        print("\n=== Zoho ===")
        if not getattr(settings, "REFRESH_TOKEN", None):
            print("  Skipped — no refresh token.")
        else:
            try:
                import requests
                r = requests.post(settings.ZOHO_TOKEN_URL, params={
                    "refresh_token": settings.REFRESH_TOKEN,
                    "client_id": settings.CLIENT_ID,
                    "client_secret": settings.CLIENT_SECRET,
                    "grant_type": "refresh_token",
                }, timeout=20)
                body = r.json()
                if "access_token" in body:
                    line(OK, "Refresh token works")
                else:
                    line(BAD, "Token refresh refused", str(body)[:160])
                    problems.append("The Zoho refresh token is rejected. It may "
                                    "have been revoked — create a new Self Client "
                                    "token in the Zoho API console.")
            except Exception as exc:
                line(BAD, "Could not reach Zoho", str(exc)[:120])

        # ── 6. Database ───────────────────────────────────────────────
        print("\n=== Snapshot ===")
        db = getattr(settings, "DB_PATH", "")
        if db and os.path.exists(db):
            line(OK, "hrp_crm.db present", f"{os.path.getsize(db):,} bytes")
        else:
            line(WARN, "No local snapshot yet", db or "(no path)")
            print("       Not a problem — the first sync builds it. Expect the "
                  "first run to take several minutes.")

# ── summary ───────────────────────────────────────────────────────────
print("\n" + "=" * 62)
if not problems:
    print("Everything checks out. Run:  python run_shift_local.py")
else:
    print(f"{len(problems)} thing(s) to fix, in order:\n")
    seen = set()
    for i, p in enumerate((x for x in problems if not (x in seen or seen.add(x))), 1):
        print(f"  {i}. {p}")
print("=" * 62 + "\n")