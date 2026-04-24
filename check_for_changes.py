"""
GA Polling Location Change Monitor
===================================
Runs every 4 hours via GitHub Actions. Scrapes the current data from the GA
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

# ── Constants ────────────────────────────────────────────────────────────────
ELECTION_ID      = "a0pcs00000J6e6HAAR"
PAGE_URI         = (
    "/s/advanced-voting-location-information"
    f"?election={ELECTION_ID}&countyName=&page=advpollingplace"
)
BASE_URL         = "https://mvp.sos.ga.gov"
AURA_ENDPOINT    = f"{BASE_URL}/s/sfsites/aura"
PAGE_URL         = f"{BASE_URL}{PAGE_URI}"
RECORDS_PER_PAGE = 50

BASELINE_CSV     = Path("ga_may_2026_primary_polling_locations.csv")
CHANGE_LOG       = Path("change_log.json")
CHECK_LOG_TXT    = Path("check_log.txt")
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
    """Parse 'STREET, CITY STATE ZIP' into components."""
    raw_addr = raw_addr.strip()
    parts = raw_addr.split(", ", 1)
    if len(parts) != 2:
        return {"line_1": raw_addr.title(), "city": "", "state": "", "zip": "",
                "full": raw_addr.title(), "address_id": raw_addr.replace(" ", "_")}
    street, city_state_zip = parts[0].strip(), parts[1].strip()
    tokens = city_state_zip.split()
    if len(tokens) >= 3:
        zip_code, state, city = tokens[-1], tokens[-2], " ".join(tokens[:-2])
    elif len(tokens) == 2:
        zip_code, state, city = "", tokens[-1], tokens[0]
    else:
        zip_code = state = ""; city = city_state_zip
    line_1 = street.title()
    city_tc = city.title()
    full = f"{line_1}, {city_tc}, {state} {zip_code}".strip(", ")
    return {"line_1": line_1, "city": city_tc, "state": state, "zip": zip_code,
            "full": full, "address_id": f"{street}_{city}_{state}_{zip_code}"}


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
    events   = rec.get("eventList", [])
    raw_addr = rec.get("address", "").replace("<br>", ", ").replace("<BR>", ", ").strip()
    addr     = _parse_address(raw_addr)
    return {
        "address_id":                   addr["address_id"],
        "polling_place_county":         rec.get("county", ""),
        "polling_place_name_raw":       rec.get("name", ""),
        "polling_place_name":           rec.get("name", ""),
        "polling_place_address_raw":    raw_addr,
        "polling_place_address_full":   addr["full"],
        "polling_place_address_line_1": addr["line_1"],
        "polling_place_address_city":   addr["city"],
        "polling_place_address_state":  addr["state"],
        "polling_place_address_zip":    addr["zip"],
        "hours_raw":                    flatten_hours(events),
        "image_url":                    "",
        "hours_advanced_polling":       _hours_advanced_only(events),
        "Latitude":                     "",
        "Longitude":                    "",
        "status":                       "",
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
    return [record_to_row(r) for r in filtered]


# ── Comparison logic ──────────────────────────────────────────────────────────

def _row_key(row: dict) -> str:
    """Normalised matching key: 'COUNTY||NAME_RAW' — collapses internal whitespace."""
    import re
    county   = re.sub(r'\s+', ' ', row.get("polling_place_county",   row.get("county", "")).strip()).upper()
    name_raw = re.sub(r'\s+', ' ', row.get("polling_place_name_raw", row.get("name",   "")).strip()).upper()
    return f"{county}||{name_raw}"


def load_baseline_from_sheet() -> dict[str, dict]:
    """
    Read all 16 columns from the Google Sheet.
    Returns a dict keyed by 'COUNTY||NAME_RAW'.
    """
    from update_sheet import _get_client, _get_worksheet
    print("  Loading baseline from Google Sheet...")
    client = _get_client()
    ws     = _get_worksheet(client)
    rows   = ws.get_all_values()

    FIELDS = [
        "address_id", "polling_place_county", "polling_place_name_raw",
        "polling_place_name", "polling_place_address_raw",
        "polling_place_address_full", "polling_place_address_line_1",
        "polling_place_address_city", "polling_place_address_state",
        "polling_place_address_zip", "hours_raw", "image_url",
        "hours_advanced_polling", "Latitude", "Longitude", "status", "date_added",
    ]

    baseline: dict[str, dict] = {}
    for i, row in enumerate(rows):
        if i == 0:
            continue
        row = row + [""] * (17 - len(row))
        rec = dict(zip(FIELDS, [v.strip() for v in row[:17]]))
        county   = rec["polling_place_county"]
        name_raw = rec["polling_place_name_raw"]
        if county or name_raw:
            key = f"{county.upper()}||{name_raw.upper()}"
            baseline[key] = rec

    print(f"  Sheet baseline: {len(baseline)} rows")
    return baseline


def load_baseline_from_csv() -> dict[str, dict]:
    """
    Fallback: read the local CSV when Google credentials are not available.
    Keyed by 'COUNTY||NAME_RAW'.
    """
    if not BASELINE_CSV.exists():
        return {}
    with open(BASELINE_CSV, newline="", encoding="utf-8") as f:
        return {_row_key(row): row for row in csv.DictReader(f)
                if row.get("polling_place_county")}


def compare(baseline: dict[str, dict], current: list[dict]) -> dict:
    """
    Compare scraped current data against the baseline (keyed by COUNTY||NAME_RAW).

    Fields checked for changes:
      polling_place_county      — county reassignment
      polling_place_name_raw    — location renamed
      polling_place_address_raw — location moved
      hours_raw                 — schedule updated

    ADDED  = county+name combo not found in baseline
    REMOVED = combo disappeared from current data
    MODIFIED = any field-level change on a matched row
    """
    current_by_key = {_row_key(r): r for r in current}

    added, removed, modified = [], [], []

    for key, rec in current_by_key.items():
        if key not in baseline:
            added.append(rec)
        else:
            base    = baseline[key]
            changes: dict[str, dict] = {}
            for field in ("polling_place_county", "polling_place_name_raw",
                          "polling_place_address_raw", "hours_raw"):
                before = base.get(field, "").strip()
                after  = rec.get(field,  "").strip()
                if before != after:
                    changes[field] = {"from": before, "to": after}
            if changes:
                modified.append({"record": rec, "changes": changes})

    for key, rec in baseline.items():
        if key not in current_by_key:
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

    print(f"  check_log.txt → {line.strip()}")


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
                     "name":   r.get("polling_place_name_raw",""),
                     "address":r.get("polling_place_address_raw","")}
                    for r in diff["added"]],
        "removed": [{"county": r.get("polling_place_county",""),
                     "name":   r.get("polling_place_name_raw","")}
                    for r in diff["removed"]],
        "modified":[{"county":  e["record"].get("polling_place_county",""),
                     "name":    e["record"].get("polling_place_name_raw",""),
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
        name   = r.get("polling_place_name_raw", "")
        addr   = r.get("polling_place_address_raw", "")
        hrs    = r.get("hours_raw", "")
        lines.append(f"  + [{county}]  {name}")
        lines.append(f"      {addr}")
        if hrs:
            lines.append(f"      Hours: {hrs[:120]}{'...' if len(hrs) > 120 else ''}")
    return "\n".join(lines)


def _fmt_removed(records: list[dict]) -> str:
    return "\n".join(
        f"  - [{r.get('polling_place_county','')}]  {r.get('polling_place_name_raw','')}"
        for r in records
    )


def _fmt_modified(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        r = e["record"]
        county = r.get("polling_place_county", "")
        name   = r.get("polling_place_name_raw", "")
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
        "The GA May 2026 Primary polling location data has changed.\n",
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
        f"Full change log: {REPO_URL}/blob/main/change_log.json",
        f"Updated CSV    : {REPO_URL}/blob/main/ga_may_2026_primary_polling_locations.csv",
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
    "address_id", "polling_place_county", "polling_place_name_raw",
    "polling_place_name", "polling_place_address_raw",
    "polling_place_address_full", "polling_place_address_line_1",
    "polling_place_address_city", "polling_place_address_state",
    "polling_place_address_zip", "hours_raw", "image_url",
    "hours_advanced_polling", "Latitude", "Longitude", "status", "date_added",
]


def save_baseline(rows: list[dict]):
    with open(BASELINE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Baseline updated: {BASELINE_CSV} ({len(rows)} rows)")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n{'=' * 60}")
    print(f"GA Polling Monitor  —  {timestamp}")
    print(f"{'=' * 60}")

    # 1. Load baseline — prefer Google Sheet, fall back to CSV
    if os.getenv("GOOGLE_CREDENTIALS"):
        print("\n[1/4] Loading baseline from Google Sheet...")
        try:
            baseline = load_baseline_from_sheet()
        except Exception as e:
            print(f"  [!] Sheet read failed ({e}), falling back to CSV...")
            baseline = load_baseline_from_csv()
    else:
        print("\n[1/4] Loading baseline from CSV (no Google credentials)...")
        baseline = load_baseline_from_csv()
    print(f"  Baseline records: {len(baseline)}")

    # 2. Scrape current data
    print("\n[2/4] Scraping current data from SOS portal...")
    try:
        current = await scrape_current()
    except Exception as e:
        print(f"  [ERROR] Scrape failed: {e}")
        raise

    print(f"  Current records: {len(current)}")

    # 3. Compare
    print("\n[3/4] Comparing...")
    diff = compare(baseline, current)
    print(f"  Added:    {len(diff['added'])}")
    print(f"  Removed:  {len(diff['removed'])}")
    print(f"  Modified: {len(diff['modified'])}")

    # Always attempt to clear resolved status tags from previous runs
    if os.getenv("GOOGLE_CREDENTIALS"):
        try:
            from update_sheet import clear_resolved_statuses
            clear_resolved_statuses(current)
        except Exception as e:
            print(f"  [!] Status clear failed: {e}")

    if not has_changes(diff):
        append_to_check_log(diff, timestamp, len(current))
        print("\n  No changes detected. Baseline unchanged.")
        return

    # 4. Handle changes
    print(f"\n[4/4] Changes detected — updating files and notifying...")
    append_to_log(diff, timestamp, len(current))
    save_baseline(current)
    send_email(diff, timestamp)

    # 5. Sync to Google Sheet (only runs if GOOGLE_CREDENTIALS is set)
    sheet_result: Optional[dict] = None
    if os.getenv("GOOGLE_CREDENTIALS"):
        print("\n[5/5] Syncing changes to Google Sheet...")
        try:
            from update_sheet import sync_changes
            sheet_result = sync_changes(diff)
            print(f"  Sheet result: {sheet_result}")
        except Exception as e:
            print(f"  [!] Sheet sync failed: {e}")
            sheet_result = {"appended": 0, "updated": 0, "error": str(e)}
    else:
        print("\n[5/5] GOOGLE_CREDENTIALS not set — skipping sheet sync.")

    # Write to check log AFTER sheet sync so the result is included
    append_to_check_log(diff, timestamp, len(current), sheet_result)

    # Keep local CSV in sync with the sheet after every change run
    if os.getenv("GOOGLE_CREDENTIALS"):
        try:
            from update_sheet import pull_from_sheet
            pull_from_sheet()
        except Exception as e:
            print(f"  [!] CSV pull from sheet failed: {e}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
