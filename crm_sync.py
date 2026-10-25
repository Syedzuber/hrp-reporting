"""
Horizon Renewable Power — CRM history extractor.

Pulls Leads, Calls, Tasks and Deals from Zoho CRM into a local SQLite
database so that historical / cohort analysis is possible.

Why a local DB: the live dashboard is stateless — it asks Zoho "what is true
right now" and throws the answer away. Journey analysis needs "what happened
to the leads that arrived on Tuesday, and how long did each step take", which
requires stored history. Hence this.

Run:  python crm_sync.py [days]        (default 90)
"""

import json
import logging
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta

import pytz
import requests

import settings as config
import settings

log = logging.getLogger("crm_sync")

IST = pytz.timezone("Asia/Kolkata")
DB_PATH = settings.DB_PATH

# COQL caps a single response at 200 rows; we page with OFFSET inside each
# time chunk, and chunk by week so no single chunk approaches the offset cap.
COQL_PAGE = 200
COQL_MAX_OFFSET = 2000
CHUNK_DAYS = 7


# ── Auth ──────────────────────────────────────────────────────
_access_token = None
_token_expiry = None
_token_lock = threading.Lock()


class AuthError(RuntimeError):
    """Zoho would not issue an access token, so nothing can be synced.

    Raised rather than logged. The fetch functions used to catch it like any
    other request error, carry on, and write a sync_log row for every module
    stamped with the current time — so an expired token made stale data look
    freshly synced.
    """


def get_access_token():
    global _access_token, _token_expiry
    with _token_lock:
        if _access_token and _token_expiry and datetime.now() < _token_expiry:
            return _access_token
        try:
            r = requests.post(config.ZOHO_TOKEN_URL, params={
                "refresh_token": config.REFRESH_TOKEN,
                "client_id":     config.CLIENT_ID,
                "client_secret": config.CLIENT_SECRET,
                "grant_type":    "refresh_token",
            }, timeout=15)
            data = r.json()
        except Exception as exc:
            raise AuthError(f"Token refresh request failed: {exc}") from exc
        if "access_token" not in data:
            raise AuthError(f"Token refresh failed: {data}")
        _access_token = data["access_token"]
        _token_expiry = datetime.now() + timedelta(seconds=3300)
        return _access_token


def headers():
    return {
        "Authorization": f"Zoho-oauthtoken {get_access_token()}",
        "Content-Type": "application/json",
    }


# ── Module definitions ────────────────────────────────────────
# date_field is what we chunk/filter on. fields are what we pull.
MODULES = {
    "Leads": {
        "date_field": "Created_Time",
        "fields": [
            "id", "Created_Time", "Modified_Time", "Lead_Source", "Lead_Status",
            "State", "City", "Full_Name", "Company", "Owner",
            "Rating", "Email", "Phone",
            # added for the daily CEO PDF
            "Capacity_kW", "Installation_Type", "Sanction_Load",
            # per-person attribution: staff are off-roll and share one Zoho
            # login, so the real actor is in Done_By, not Owner.
            "Done_By", "Latest_Call_Action", "Next_Follow_up", "Pending_at",
            # Zoho's own "Last Activity Time" — the field the CRM's built-in
            # charts filter on. It moves when a lead is actually worked, and
            # unlike Modified_Time it is not disturbed by workflow rules or
            # bulk edits. "Leads worked" is counted from this.
            "Last_Activity_Time",
        ],
    },
    "Calls": {
        "date_field": "Call_Start_Time",
        "fields": [
            "id", "Subject", "Call_Type", "Call_Start_Time", "Call_Duration",
            "Call_Duration_in_seconds", "Call_Status", "Call_Result",
            "Who_Id", "What_Id", "Owner", "Created_Time",
            # added for the daily CEO PDF
            "Call_Purpose", "CTI_Entry",
        ],
    },
    "Tasks": {
        "date_field": "Created_Time",
        "fields": [
            "id", "Subject", "Status", "Due_Date", "Created_Time",
            "Closed_Time", "Who_Id", "What_Id", "Owner", "Priority",
        ],
    },
    # Events == the Meetings module in the Zoho UI. The team does not log
    # meetings yet, so this will sync zero rows; the report shows a real zero
    # and starts counting automatically the day they begin logging.
    "Events": {
        "date_field": "Created_Time",
        "fields": [
            "id", "Event_Title", "Start_DateTime", "End_DateTime",
            "Owner", "What_Id", "Who_Id", "Created_Time", "Modified_Time",
            "Venue", "Description",
            # Survey / Client Discussion picklist, added to Zoho in August 2026.
            "Meeting_Type",
        ],
        # Fields whose API name has not been confirmed against Zoho. If one
        # makes a request fail it is dropped and the request retried, so a
        # wrong name costs that field, not the whole module. Logged loudly.
        "optional": ["Meeting_Type"],
    },
    # Proposals is a custom module (API name: Proposals). Proposal Date is
    # Date_1; the sender is a picklist, NOT the record owner.
    "Proposals": {
        "date_field": "Created_Time",
        "fields": [
            "id", "Name", "Owner", "Created_Time", "Modified_Time",
            "Date_1", "Proposal_Sender_Name", "Proposal_Sender_Email",
            "Plant_Capacity", "Bill_Amount", "Units_in_kWh",
            "EPC_Cost_kWp", "Cost_kWp_after_Discount",
            "Deals", "Deal_Reference",
        ],
    },
    "Deals": {
        "date_field": "Created_Time",
        "fields": [
            "id", "Deal_Name", "Stage", "Amount", "Closing_Date",
            "Created_Time", "Modified_Time", "Owner", "Lead_Conversion_Time",
            # added for the daily CEO PDF
            "Date_of_Proposal", "Project_Size_kW", "Capacity_kW",
            "Installation_Type", "Cost_kWp", "Customer_Type",
            "Next_Follow_Up", "Zoho_Lead_ID",
            "Done_By", "Latest_Call_Action", "Next_Action", "Pending_at",
            "Last_Activity_Time",
        ],
    },
}


def _fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")


# ── COQL fetch (primary) ──────────────────────────────────────
def coql_fetch(module, fields, date_field, start, end):
    """Fetch one time-chunk via COQL. Returns (records, ok)."""
    url = f"{config.ZOHO_CRM_BASE.replace('/v2', '/v2')}/coql"
    select = ", ".join(fields)
    out = []
    offset = 0
    while offset < COQL_MAX_OFFSET:
        query = (
            f"select {select} from {module} "
            f"where {date_field} between '{_fmt(start)}' and '{_fmt(end)}' "
            f"order by {date_field} asc "
            f"limit {COQL_PAGE} offset {offset}"
        )
        try:
            r = requests.post(url, headers=headers(),
                              data=json.dumps({"select_query": query}), timeout=30)
        except AuthError:
            raise
        except Exception as exc:
            log.warning("COQL request error on %s: %s", module, exc)
            return out, False

        if r.status_code == 204:
            return out, True
        if r.status_code == 429:
            log.warning("Rate limited — pausing 60s")
            time.sleep(60)
            continue
        if r.status_code != 200:
            # Print the whole error — the usual cause is one unqueryable field
            # in the select list, and the message names it.
            log.warning("COQL %s failed (HTTP %s): %s", module, r.status_code, r.text[:600])
            return out, False

        body = r.json()
        rows = body.get("data", [])
        out.extend(rows)
        if not body.get("info", {}).get("more_records"):
            return out, True
        offset += COQL_PAGE
        time.sleep(0.2)

    log.warning("%s: hit COQL offset cap inside one chunk — data may be truncated", module)
    return out, True


# ── List-endpoint fetch (fallback) ────────────────────────────
# Zoho's list endpoint has no server-side date filter we can trust, so we walk
# it newest-first and stop as soon as a whole page predates the window. The
# page ceiling is a runaway guard, NOT the intended stopping point — if a run
# ever exits because of it, the data is truncated and `complete` says so.
LIST_MAX_PAGES = 250          # 50,000 records
LIST_PER_PAGE = 200


def list_fetch(module, fields, date_field, start, end, max_pages=LIST_MAX_PAGES,
               optional=()):
    """Fallback: newest-first list endpoint, filtered in Python.

    Returns (records, complete). complete=False means we stopped on the page
    ceiling rather than because we ran past the window — i.e. truncated.
    `optional` fields are dropped and the fetch retried if the request fails.
    """
    url = f"{config.ZOHO_CRM_BASE}/{module}"
    # Sorting on the field we filter on lets us stop early and correctly.
    sort_field = date_field if date_field.endswith("_Time") else "Created_Time"
    keep = []
    complete = False
    stopped_by = None       # why the walk ended early, if it was not the ceiling

    for page in range(1, max_pages + 1):
        params = {
            "fields": ",".join([f for f in fields if f != "id"]),
            "page": page, "per_page": LIST_PER_PAGE,
            "sort_by": sort_field, "sort_order": "desc",
        }
        try:
            r = requests.get(url, headers=headers(), params=params, timeout=30)
        except AuthError:
            raise
        except Exception as exc:
            log.warning("List %s page %d error: %s", module, page, exc)
            stopped_by = f"a request error on page {page}"
            break

        if r.status_code == 204:
            complete = True
            break
        if r.status_code == 429:
            log.warning("Rate limited — pausing 60s")
            time.sleep(60)
            continue
        if r.status_code != 200:
            log.warning("List %s failed (%s): %s", module, r.status_code, r.text[:200])
            requested = [f for f in optional if f in fields]
            if requested:
                _log_optional_retry(module, requested, "list")
                return list_fetch(module, [f for f in fields if f not in requested],
                                  date_field, start, end, max_pages)
            # A bad sort field is the usual cause — retry once unsorted.
            if sort_field != "Created_Time":
                log.info("Retrying %s sorted by Created_Time", module)
                return list_fetch(module, fields, "Created_Time", start, end, max_pages)
            stopped_by = f"HTTP {r.status_code} on page {page}"
            break

        body = r.json()
        rows = body.get("data", [])
        if not rows:
            complete = True
            break

        page_dates = [parse_dt(row.get(date_field)) for row in rows]
        for row, d in zip(rows, page_dates):
            if d and start <= d <= end:
                keep.append(row)

        # Everything on this page is older than the window, and pages only get
        # older from here — nothing left to find.
        newest = max([d for d in page_dates if d], default=None)
        if newest and newest < start:
            complete = True
            break

        if not body.get("info", {}).get("more_records"):
            complete = True
            break
        time.sleep(0.2)

    # Name the real cause. This used to say "page ceiling" for every early stop,
    # including a failure on the very first page.
    if stopped_by:
        log.warning("%s: list fetch stopped after %s — RESULTS ARE INCOMPLETE",
                    module, stopped_by)
    elif not complete:
        log.warning("%s: stopped at the %d-page ceiling — RESULTS ARE TRUNCATED",
                    module, max_pages)
    return keep, complete


def _log_optional_retry(module, dropped, path):
    # A warning, not an error: the request may have failed for another reason.
    # An error is logged once the evidence points at the field itself.
    log.warning("%s: the %s request failed with optional field(s) %s in it — "
                "retrying without them.", module, path, ", ".join(dropped))


def parse_dt(value):
    if not value:
        return None
    try:
        if "T" in str(value):
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            return dt.astimezone(IST)
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        return IST.localize(d)
    except Exception:
        return None


# ── Storage ───────────────────────────────────────────────────
def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS records (
            module   TEXT NOT NULL,
            id       TEXT NOT NULL,
            payload  TEXT NOT NULL,
            PRIMARY KEY (module, id)
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            module     TEXT,
            method     TEXT,
            rows       INTEGER,
            synced_at  TEXT
        );
    """)
    # `complete` added later — tolerate an existing DB without it.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sync_log)")}
    if "complete" not in cols:
        conn.execute("ALTER TABLE sync_log ADD COLUMN complete INTEGER DEFAULT 1")
    conn.commit()


def store(conn, module, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO records (module, id, payload) VALUES (?,?,?)",
        [(module, str(r.get("id")), json.dumps(r)) for r in rows if r.get("id")],
    )
    conn.commit()


def load(conn, module):
    cur = conn.execute("SELECT payload FROM records WHERE module=?", (module,))
    return [json.loads(row[0]) for row in cur.fetchall()]


# ── Orchestration ─────────────────────────────────────────────
def sync(days=90, db_path=DB_PATH):
    """Pull every module into the snapshot. Raises AuthError if Zoho will not
    issue a token — before anything is written, so a sync that could not run
    leaves no sync_log row behind to look like a fresh one."""
    now = datetime.now(IST)
    window_start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    get_access_token()

    conn = sqlite3.connect(db_path)
    try:
        return _sync_modules(conn, now, window_start)
    finally:
        conn.close()


def _sync_modules(conn, now, window_start):
    init_db(conn)

    summary = {}
    for module, spec in MODULES.items():
        fields = spec["fields"]
        optional = [f for f in spec.get("optional", []) if f in fields]
        date_field = spec["date_field"]
        total = 0
        method = "coql"
        complete = True
        seen = set()            # optional fields Zoho actually returned

        chunk_start = window_start
        coql_worked = True
        while chunk_start < now:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), now)
            rows, ok = coql_fetch(module, fields, date_field, chunk_start, chunk_end)
            if not ok and any(f in fields for f in optional):
                # A wrong API name in the select list fails the whole query.
                # Retry without the optional fields; if that fails too, they
                # were not the cause, so keep them for the list fallback.
                _log_optional_retry(module, optional, "COQL")
                trimmed = [f for f in fields if f not in optional]
                rows, ok = coql_fetch(module, trimmed, date_field, chunk_start, chunk_end)
                if ok:
                    log.error("%s: COQL works without %s, so that API name is wrong. "
                              "It is dropped for this sync and will be missing from "
                              "every %s record until crm_sync.MODULES is fixed.",
                              module, ", ".join(optional), module)
                    fields = trimmed
                else:
                    log.warning("%s: COQL failed without the optional field(s) too, so "
                                "they are not the cause.", module)
            if not ok:
                coql_worked = False
                break
            seen.update(f for f in optional for r in rows if f in r)
            store(conn, module, rows)
            total += len(rows)
            log.info("%s %s→%s: %d rows", module,
                     chunk_start.date(), chunk_end.date(), len(rows))
            chunk_start = chunk_end

        if not coql_worked:
            log.warning("%s: COQL unavailable — falling back to the list endpoint. "
                        "This walks pages newest-first until it passes the window.", module)
            method = "list-fallback"
            rows, complete = list_fetch(module, fields, date_field, window_start, now,
                                        optional=[f for f in optional if f in fields])
            seen.update(f for f in optional for r in rows if f in r)
            store(conn, module, rows)
            total = len(rows)

        # Zoho can also accept an unknown field name and simply leave it out of
        # every record. That fails nothing, so check for it explicitly.
        absent = [f for f in optional if f not in seen]
        if total and absent:
            log.error("%s: none of the %d records stored carries %s. The field was "
                      "dropped above, or Zoho ignored it because the API name is "
                      "wrong. Anything that reads it is falling back.",
                      module, total, ", ".join(absent))

        conn.execute(
            "INSERT INTO sync_log (module, method, rows, synced_at, complete) "
            "VALUES (?,?,?,?,?)",
            (module, method, total, now.isoformat(), 1 if complete else 0),
        )
        conn.commit()
        summary[module] = {"rows": total, "method": method, "complete": complete}
        log.info("%s: %d records stored via %s%s", module, total, method,
                 "" if complete else "  ** INCOMPLETE **")

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    log.info("Syncing last %d days of Zoho CRM history...", days)
    result = sync(days)
    print("\n" + "=" * 62)
    for mod, info in result.items():
        flag = "" if info["complete"] else "   ** TRUNCATED **"
        print(f"  {mod:<8} {info['rows']:>6} records   ({info['method']}){flag}")
    print("=" * 62)
    print(f"\nStored in {DB_PATH}. Now run:  python diagnostic.py\n")