"""
GA Polling Location Change Monitor — November 2026 General & Special Elections
================================================================================
Runs daily via GitHub Actions. Scrapes the current data from the GA
SOS MVP portal, compares it against the Google Sheet (primary baseline), and:

  • Sends an email notification if anything changed (new location, removed
    location, address change, hours change)
  • Updates the Google Sheet with the changes
  • Appends a structured entry to change_log.json
  • Overwrites the baseline CSV (kept as a backup/archive)
  • Exits with code 0 always (GitHub Actions will commit any file changes)

Baseline priority:
  1. Google Sheet (columns A–D) — when GOOGLE_CREDENTIALS is set
  2. Local baseline CSV               — fallback for local runs without credentials

Required environment variables (set as GitHub Actions secrets):
  EMAIL_SENDER        Gmail address to send from
  EMAIL_APP_PASSWORD  Gmail app password (not your account password)
  EMAIL_RECIPIENT     Address(es) to notify — comma-separated for multiple
  GOOGLE_CREDENTIALS  Full JSON of Google service account key
"""

import asyncio
import csv
import json
import os
import smtplib
import time
import urllib.parse
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright

import address_standardization

# ── Constants ────────────────────────────────────────────────────────────────
ELECTION_ID      = "a0pcs00000J6eJBAAZ"
PAGE_URI         = (
    "/s/advanced-voting-location-information"
    f"?election={ELECTION_ID}&countyName=&page=advpollingplace"
)
BASE_URL         = "https://mvp.sos.ga.gov"
AURA_ENDPOINT    = f"{BASE_URL}/s/sfsites/aura"
PAGE_URL         = f"{BASE_URL}{PAGE_URI}"
RECORDS_PER_PAGE = 50

BASELINE_CSV     = Path("ga_nov_2026_general_polling_locations.csv")
CHANGE_LOG       = Path("change_log_nov2026.json")
CHECK_LOG_TXT    = Path("check_log_nov2026.txt")
HISTORY_FILE     = Path("location_history.json")
REPO_URL         = "https://github.com/madiraa/ga_may_2026_primary_polling_location_scrape"

# Run headless in CI (GitHub Actions sets CI=true), visible locally
IS_CI = os.getenv("CI", "false").lower() == "true"


# ── Scraping helpers (mirrors scrape_polling_places.py) ──────────────────────

def flatten_hours(event_list) -> str:
    if not event_list:
        return ""
    if isinstance(event_list, str):
        return event_list.strip()
    parts = []
    for h in event_list:
        if isinstance(h, dict):
            parts.append(
                f"{h.get('startDate','')} - {h.get('endDate','')} "
                f"{h.get('openTime','')} - {h.get('closeTime','')} "
                f"({h.get('eventType','')})"
            )
        else:
            parts.append(str(h))
    return " | ".join(parts)


def parse_records(actions_list: list) -> tuple[list[dict], int]:
    for action in actions_list:
        rv = action.get("returnValue", {})
        if not rv:
            continue
        inner = rv.get("returnValue", rv)
        if not isinstance(inner, dict):
            continue
        if "ppList" in inner:
            pp_raw = inner["ppList"]
            total  = int(inner.get("totalRecords", 0))
            if isinstance(pp_raw, str):
                try:
                    return json.loads(pp_raw), total
                except json.JSONDecodeError:
                    pass
            elif isinstance(pp_raw, list):
                return pp_raw, total
    return [], 0


async def fetch_page(page, aura_context: dict, skip: int, per_page: int) -> dict:
    search_param = json.dumps({"election": ELECTION_ID, "countyName": ""})
    context_str  = json.dumps(aura_context)
    js = f"""
    async () => {{
        const message = {{
            actions: [{{
                id: "monitor;a",
                descriptor: "aura://ApexActionController/ACTION$execute",
                callingDescriptor: "UNKNOWN",
                params: {{
                    namespace: "",
                    classname: "vrWebIntegrationController",
                    method: "getAdvPollingPlaces",
                    params: {{
                        sarchParam: {json.dumps(search_param)},
                        recordPerPage: {per_page},
                        skipRecords: {skip}
                    }},
                    cacheable: false,
                    isContinuation: false
                }}
            }}]
        }};
        const body = new URLSearchParams({{
            message: JSON.stringify(message),
            "aura.context": {json.dumps(context_str)},
            "aura.pageURI": {json.dumps(PAGE_URI)},
            "aura.token": "null"
        }});
        const resp = await fetch(
            "{AURA_ENDPOINT}?r=monitor&aura.ApexAction.execute=1",
            {{
                method: "POST",
                headers: {{"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}},
                body: body.toString()
            }}
        );
        return await resp.json();
    }}
    """
    return await page.evaluate(js)


def is_dropbox_only(rec: dict) -> bool:
    """Returns True if every event is a Dropbox Polling Location (no staffed voting)."""
    events = rec.get("eventList", [])
    if not events:
        return False
    return all(
        e.get("eventType", "") == "Dropbox Polling Location"
        for e in events
        if isinstance(e, dict)
    )


def _parse_address(raw_addr: str) -> dict:
    """Standardize 'STREET, CITY STATE ZIP' via usaddress-based normalization."""
    result = address_standardization.standardize(raw_addr)
    address_id = f"{result['line_1']}_{result['city']}_{result['state']}_{result['zip']}".replace(" ", "_")
    return {**result, "address_id": address_id}


def _clean_polling_name(name: str) -> str:
    """Strip AIP, AV, EV, AIP/EV prefixes and suffixes added by SOS to location names."""
    import re
    s = name.strip()
    prefixes = [
        r'^AIP/EV\s*[-–]?\s*',
        r'^AIP\s*[-–/]\s*',
        r'^AIP\s+',
        r'^AV\s*[-–/]\s*',
        r'^AV\s+',
        r'^EV\s*[-–/]\s*',
        r'^EV\s+',
        r'^ADVANCE\s*[-–]\s*EARLY VOTING\s*',
    ]
    for p in prefixes:
        new = re.sub(p, '', s, flags=re.IGNORECASE).strip()
        if new != s:
            s = new
            break
    suffixes = [
        r'\s*[-–/]\s*AIP/EV\s*$',
        r'\s*[-–/]\s*AIP\s*$',
        r'\s*[-–/]\s*AV\s*$',
        r'\s*[-–/]\s*EV\s*$',
        r'\s+AIP\s*$',
        r'\s+AV\s*$',
        r'\s+EV\s*$',
        r'\s*\(\s*AIP\s*\)\s*$',
    ]
    for p in suffixes:
        s = re.sub(p, '', s, flags=re.IGNORECASE).strip()
    return s


def _hours_advanced_only(events: list) -> str:
    parts = []
    for e in events:
        if isinstance(e, dict) and e.get("eventType") == "Advanced Polling Location":
            parts.append(
                f"{e.get('startDate','')} - {e.get('endDate','')} "
                f"{e.get('openTime','')} - {e.get('closeTime','')} "
                f"(Advanced Polling Location)"
            )
    return "\n".join(parts)


def record_to_row(rec: dict) -> dict:
    import re
    events   = rec.get("eventList", [])
    raw_addr = rec.get("address", "").replace("<br>", ", ").replace("<BR>", ", ").strip()
    addr     = _parse_address(raw_addr)
    name     = _clean_polling_name(re.sub(r'\s+', ' ', rec.get("name", "").strip()))
    return {
        "location_id":                  rec.get("id", ""),
        "address_id":                   addr["address_id"],
        "polling_place_county":         rec.get("county", ""),
        "polling_place_name":           name,
        "polling_place_address_full":   addr["full"],
        "polling_place_address_line_1": addr["line_1"],
        "polling_place_address_city":   addr["city"],
        "polling_place_address_state":  addr["state"],
        "polling_place_address_zip":    addr["zip"],
        "image_url":                    "",
        "hours_advanced_polling":       _hours_advanced_only(events),
        "Latitude":                     "",
        "Longitude":                    "",
        "status":                       "",
        "date_added":                   "",
        "date_removed":                 "",
        "_address_review":              addr["status"] == "review",  # dropped by DictWriter (extrasaction="ignore")
    }


async def scrape_current() -> list[dict]:
    """Full scrape; returns list of normalised row dicts."""
    print(f"  Headless: {IS_CI}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=IS_CI)
        ctx_browser = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx_browser.new_page()

        captured_context: dict = {}

        async def on_request(req):
            nonlocal captured_context
            if (
                not captured_context
                and "aura" in req.url
                and req.method == "POST"
                and "ApexAction" in req.url
            ):
                body = req.post_data or ""
                try:
                    params   = dict(part.split("=", 1) for part in body.split("&") if "=" in part)
                    ctx_json = urllib.parse.unquote(params.get("aura.context", ""))
                    captured_context = json.loads(ctx_json)
                except Exception:
                    pass

        page.on("request", on_request)
        await page.goto(PAGE_URL, wait_until="networkidle", timeout=90000)
        await page.wait_for_timeout(3000)

        if not captured_context:
            # Hardcoded fallback — update if the site ever changes framework version
            captured_context = {
                "mode": "PROD",
                "fwuid": "TXFWNVprQUZzQnEtNXVXYTFLQ2ppdzJEa1N5enhOU3R5QWl2VzNveFZTbGcxMy4tMjE0NzQ4MzY0OC4xMzEwNzIwMA",
                "app": "siteforce:communityApp",
                "loaded": {"APPLICATION@markup://siteforce:communityApp": "1537_wmTAUxhOaM_47EClrN56Dw"},
                "dn": [], "globals": {}, "uad": True
            }

        all_raw: list[dict] = []
        skip = 0

        while True:
            resp = await fetch_page(page, captured_context, skip=skip, per_page=RECORDS_PER_PAGE)
            batch, total = parse_records(resp.get("actions", []))
            if not batch:
                break
            all_raw.extend(batch)
            print(f"    Fetched {len(all_raw)}/{total} records...")
            skip += RECORDS_PER_PAGE
            if skip >= total:
                break
            time.sleep(0.4)

        await browser.close()

    filtered = [r for r in all_raw if not is_dropbox_only(r)]
    dropped  = len(all_raw) - len(filtered)
    if dropped:
        print(f"    Filtered out {dropped} dropbox-only location(s)")

    rows = [record_to_row(r) for r in filtered]
    review_rows = [r for r in rows if r.get("_address_review")]
    if review_rows:
        print(f"    [!] {len(review_rows)} address(es) could not be confidently parsed:")
        for r in review_rows:
            print(f"        [{r['polling_place_county']}] {r['polling_place_name']}: {r['polling_place_address_full']}")
    return rows


# ── Comparison logic ──────────────────────────────────────────────────────────
# Matched by location_id — the Salesforce record id — never by county/name text.
# County+name text is display data the county can rename; the id is what's stable.

def load_baseline_from_csv() -> dict[str, dict]:
    """Baseline is always the local CSV from the previous run (git-committed by CI)."""
    if not BASELINE_CSV.exists():
        return {}
    with open(BASELINE_CSV, newline="", encoding="utf-8") as f:
        return {row["location_id"]: row for row in csv.DictReader(f)
                if row.get("location_id")}


def compare(baseline: dict[str, dict], current: list[dict]) -> dict:
    """
    Compare scraped data against the baseline (keyed by location_id).

    Fields checked: county, name, address_full, address components, hours
    ADDED    = location_id not in baseline
    REMOVED  = location_id disappeared from current
    MODIFIED = field-level change on a matched location_id
    """
    import re

    def _norm(s: str) -> str:
        return re.sub(r'\s+', ' ', s.strip())

    current_by_id = {r["location_id"]: r for r in current}
    added, removed, modified = [], [], []

    for loc_id, rec in current_by_id.items():
        if loc_id not in baseline:
            added.append(rec)
        else:
            base    = baseline[loc_id]
            changes: dict[str, dict] = {}
            for field in ("polling_place_county", "polling_place_name",
                          "polling_place_address_full", "polling_place_address_line_1",
                          "polling_place_address_city", "polling_place_address_state",
                          "polling_place_address_zip", "hours_advanced_polling"):
                before = _norm(base.get(field, ""))
                after  = _norm(rec.get(field,  ""))
                if before != after:
                    changes[field] = {"from": before, "to": after}
            if changes:
                modified.append({"record": rec, "changes": changes})

    for loc_id, rec in baseline.items():
        if loc_id not in current_by_id:
            removed.append(rec)

    return {"added": added, "removed": removed, "modified": modified}


def has_changes(diff: dict) -> bool:
    return bool(diff["added"] or diff["removed"] or diff["modified"])


# ── Plain-text check log (one line per run, mirrors VA check_log.txt) ────────

def append_to_check_log(diff: dict, timestamp: str, total_current: int,
                        sheet_result: Optional[dict] = None):
    """
    Appends one line to check_log.txt on every run:
      NO CHANGE: 2026-04-20 16:00:01 UTC | NO CHANGE | 300 locations checked
      CHANGED:   2026-04-20 20:00:03 UTC | CHANGED   | +2 added, 0 removed, 1 modified | Sheet: 2 appended, 1 updated
    """
    n_added    = len(diff["added"])
    n_removed  = len(diff["removed"])
    n_modified = len(diff["modified"])

    if n_added or n_removed or n_modified:
        status = "CHANGED  "
        detail = f"+{n_added} added, {n_removed} removed, {n_modified} modified"
        if sheet_result:
            sa = sheet_result.get("appended", 0)
            su = sheet_result.get("updated",  0)
            detail += f" | Sheet: {sa} appended, {su} updated"
        elif sheet_result is None and (n_added or n_modified):
            detail += " | Sheet: sync not configured"
    else:
        status = "NO CHANGE"
        detail = f"{total_current} locations checked"

    line = f"{timestamp} | {status} | {detail}\n"

    with open(CHECK_LOG_TXT, "a", encoding="utf-8") as f:
        f.write(line)

    print(f"  {CHECK_LOG_TXT} → {line.strip()}")


# ── JSON change log ───────────────────────────────────────────────────────────

def append_to_log(diff: dict, timestamp: str, total_current: int):
    log: list[dict] = []
    if CHANGE_LOG.exists():
        try:
            log = json.loads(CHANGE_LOG.read_text())
        except Exception:
            log = []

    entry = {
        "timestamp":     timestamp,
        "total_records": total_current,
        "added_count":   len(diff["added"]),
        "removed_count": len(diff["removed"]),
        "modified_count":len(diff["modified"]),
        "added":   [{"county": r.get("polling_place_county",""),
                     "name":   r.get("polling_place_name",""),
                     "address":r.get("polling_place_address_full","")}
                    for r in diff["added"]],
        "removed": [{"county": r.get("polling_place_county",""),
                     "name":   r.get("polling_place_name","")}
                    for r in diff["removed"]],
        "modified":[{"county":  e["record"].get("polling_place_county",""),
                     "name":    e["record"].get("polling_place_name",""),
                     "changes": e["changes"]}
                    for e in diff["modified"]],
    }
    log.append(entry)
    CHANGE_LOG.write_text(json.dumps(log, indent=2))
    print(f"  Log updated: {CHANGE_LOG} ({len(log)} total entries)")


# ── Email notification ────────────────────────────────────────────────────────

def _fmt_added(records: list[dict]) -> str:
    lines = []
    for r in records:
        county = r.get("polling_place_county", "")
        name   = r.get("polling_place_name", "")
        addr   = r.get("polling_place_address_full", "")
        hrs    = r.get("hours_advanced_polling", "")
        lines.append(f"  + [{county}]  {name}")
        lines.append(f"      {addr}")
        if hrs:
            lines.append(f"      Hours: {hrs[:120]}{'...' if len(hrs) > 120 else ''}")
    return "\n".join(lines)


def _fmt_removed(records: list[dict]) -> str:
    return "\n".join(
        f"  - [{r.get('polling_place_county','')}]  {r.get('polling_place_name','')}"
        for r in records
    )


def _fmt_modified(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        r = e["record"]
        county = r.get("polling_place_county", "")
        name   = r.get("polling_place_name", "")
        lines.append(f"  ~ [{county}]  {name}")
        for field, chg in e["changes"].items():
            lines.append(f"      {field.upper()} changed:")
            lines.append(f"        Before: {chg['from'][:120]}")
            lines.append(f"        After:  {chg['to'][:120]}")
    return "\n".join(lines)


def send_email(diff: dict, timestamp: str):
    sender    = os.getenv("EMAIL_SENDER", "")
    password  = os.getenv("EMAIL_APP_PASSWORD", "")
    recipient = os.getenv("EMAIL_RECIPIENT", sender)

    if not sender or not password:
        print("  [!] EMAIL_SENDER / EMAIL_APP_PASSWORD not set — skipping email.")
        return

    n_added    = len(diff["added"])
    n_removed  = len(diff["removed"])
    n_modified = len(diff["modified"])
    total_changes = n_added + n_removed + n_modified

    subject = (
        f"[GA Polling Monitor] {total_changes} change(s) detected — "
        f"{datetime.now().strftime('%b %d, %Y %I:%M %p ET')}"
    )

    body_parts = [
        "The GA November 2026 General & Special Elections polling location data has changed.\n",
        f"Timestamp : {timestamp}",
        f"Changes   : {n_added} added  |  {n_removed} removed  |  {n_modified} modified\n",
    ]

    if diff["added"]:
        body_parts += [f"── NEW LOCATIONS ({n_added}) ──────────────────────────", _fmt_added(diff["added"]), ""]
    if diff["removed"]:
        body_parts += [f"── REMOVED LOCATIONS ({n_removed}) ──────────────────", _fmt_removed(diff["removed"]), ""]
    if diff["modified"]:
        body_parts += [f"── MODIFIED LOCATIONS ({n_modified}) ─────────────────", _fmt_modified(diff["modified"]), ""]

    body_parts += [
        "─" * 60,
        f"Full change log: {REPO_URL}/blob/main/change_log_nov2026.json",
        f"Updated CSV    : {REPO_URL}/blob/main/ga_nov_2026_general_polling_locations.csv",
    ]

    body = "\n".join(body_parts)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            for addr in [a.strip() for a in recipient.split(",") if a.strip()]:
                server.sendmail(sender, addr, msg.as_string())
        print(f"  Email sent to: {recipient}")
    except Exception as e:
        print(f"  [!] Email failed: {e}")


# ── Baseline CSV writer ───────────────────────────────────────────────────────

CSV_FIELDS = [
    "location_id", "address_id", "polling_place_county", "polling_place_name",
    "polling_place_address_full", "polling_place_address_line_1",
    "polling_place_address_city", "polling_place_address_state",
    "polling_place_address_zip", "image_url", "hours_advanced_polling",
    "Latitude", "Longitude", "status", "date_added", "date_removed",
]


def save_baseline(rows: list[dict]):
    with open(BASELINE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Baseline updated: {BASELINE_CSV} ({len(rows)} rows)")


# ── Persistent per-location history (survives across runs; drives the sheet
#    rebuild's date_added/date_removed/status columns without ever reading
#    those back out of the sheet) ──────────────────────────────────────────

_LAST_KNOWN_FIELDS = [
    "address_id", "polling_place_county", "polling_place_name",
    "polling_place_address_full", "polling_place_address_line_1",
    "polling_place_address_city", "polling_place_address_state",
    "polling_place_address_zip", "hours_advanced_polling",
]


def load_history() -> dict[str, dict]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return {}


def save_history(history: dict[str, dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(history, indent=2, sort_keys=True))


def update_history(history: dict[str, dict], current: list[dict], today: str) -> dict[str, dict]:
    """Advance history one run: refresh last_known for present ids, stamp
    date_added for new ids, stamp date_removed (once) for ids that dropped out."""
    current_ids = set()
    for rec in current:
        loc_id = rec["location_id"]
        current_ids.add(loc_id)
        entry = history.get(loc_id)
        if entry is None:
            entry = {"date_added": today, "date_removed": ""}
            history[loc_id] = entry
        entry["last_known"] = {f: rec.get(f, "") for f in _LAST_KNOWN_FIELDS}

    for loc_id, entry in history.items():
        if loc_id not in current_ids and not entry.get("date_removed"):
            entry["date_removed"] = today

    return history


def build_formatted_rows(history: dict[str, dict], diff: dict, today: str) -> list[dict]:
    """One row per location ever seen, in stable county/name order, with
    status/date columns derived entirely from history + this run's diff —
    never from re-reading the sheet."""
    added_ids    = {r["location_id"] for r in diff["added"]}
    removed_ids  = {r["location_id"] for r in diff["removed"]}
    modified     = {e["record"]["location_id"]: e["changes"] for e in diff["modified"]}

    rows = []
    for loc_id, entry in history.items():
        lk = entry["last_known"]
        if loc_id in added_ids:
            status = f"ADDED {today}"
        elif loc_id in modified:
            status = f"MODIFIED: {', '.join(modified[loc_id].keys())} {today}"
        elif loc_id in removed_ids or entry.get("date_removed"):
            status = f"REMOVED {entry['date_removed']}"
        else:
            status = ""

        rows.append({
            "location_id":                   loc_id,
            "address_id":                    lk.get("address_id", ""),
            "polling_place_county":          lk.get("polling_place_county", ""),
            "polling_place_name":            lk.get("polling_place_name", ""),
            "polling_place_address_full":    lk.get("polling_place_address_full", ""),
            "polling_place_address_line_1":  lk.get("polling_place_address_line_1", ""),
            "polling_place_address_city":    lk.get("polling_place_address_city", ""),
            "polling_place_address_state":   lk.get("polling_place_address_state", ""),
            "polling_place_address_zip":     lk.get("polling_place_address_zip", ""),
            "hours_advanced_polling":        lk.get("hours_advanced_polling", ""),
            "status":                        status,
            "date_added":                    entry.get("date_added", ""),
            "date_removed":                  entry.get("date_removed", ""),
        })

    rows.sort(key=lambda r: (r["polling_place_county"], r["polling_place_name"]))
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today     = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    print(f"\n{'=' * 60}")
    print(f"GA Polling Monitor  —  {timestamp}")
    print(f"{'=' * 60}")

    # 1. Baseline is always the local CSV from the previous run — the sheet is
    #    a write target now, never a read-back source of truth.
    print("\n[1/5] Loading baseline from local CSV...")
    baseline = load_baseline_from_csv()
    print(f"  Baseline records: {len(baseline)}")

    # 2. Scrape current data
    print("\n[2/5] Scraping current data from SOS portal...")
    try:
        current = await scrape_current()
    except Exception as e:
        print(f"  [ERROR] Scrape failed: {e}")
        raise

    print(f"  Current records: {len(current)}")

    # 3. Compare (keyed by location_id)
    print("\n[3/5] Comparing...")
    diff = compare(baseline, current)
    print(f"  Added:    {len(diff['added'])}")
    print(f"  Removed:  {len(diff['removed'])}")
    print(f"  Modified: {len(diff['modified'])}")

    # 4. Advance persistent history and derive the full formatted-tab row set.
    #    This happens every run, changes or not — it's what lets ADDED/MODIFIED
    #    status tags auto-clear the day after they stop showing up in the diff.
    print("\n[4/5] Updating location history...")
    history = load_history()
    update_history(history, current, today)
    save_history(history)
    formatted_rows = build_formatted_rows(history, diff, today)
    print(f"  History: {len(history)} locations tracked")

    save_baseline(current)

    # 5. Rewrite both sheet tabs wholesale (see update_sheet.py docstring for why)
    sheet_result: Optional[dict] = None
    if os.getenv("GOOGLE_CREDENTIALS"):
        print("\n[5/5] Rewriting Google Sheet tabs...")
        try:
            from update_sheet import write_raw_tab, rebuild_formatted_tab
            n_raw       = write_raw_tab(current)
            n_formatted = rebuild_formatted_tab(formatted_rows)
            sheet_result = {"raw_rows": n_raw, "formatted_rows": n_formatted}
            print(f"  Sheet result: {sheet_result}")
        except Exception as e:
            print(f"  [!] Sheet rewrite failed: {e}")
            sheet_result = {"error": str(e)}
    else:
        print("\n[5/5] GOOGLE_CREDENTIALS not set — skipping sheet rewrite.")

    append_to_check_log(diff, timestamp, len(current), sheet_result)

    if has_changes(diff):
        append_to_log(diff, timestamp, len(current))
        send_email(diff, timestamp)
    else:
        print("\n  No content changes today.")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
