"""
GA Polling Location Change Monitor
===================================
Runs every 4 hours via GitHub Actions. Scrapes the current data from the GA
SOS MVP portal, compares it against the stored baseline CSV, and:

  • Sends an email notification if anything changed (new location, removed
    location, address change, hours change)
  • Appends a structured entry to change_log.json
  • Overwrites the baseline CSV so the next run compares against the latest
  • Exits with code 0 always (GitHub Actions will commit any file changes)

Required environment variables (set as GitHub Actions secrets):
  EMAIL_SENDER        Gmail address to send from
  EMAIL_APP_PASSWORD  Gmail app password (not your account password)
  EMAIL_RECIPIENT     Address(es) to notify — comma-separated for multiple
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


def record_to_row(rec: dict) -> dict:
    addr = rec.get("address", "").replace("<br>", ", ").replace("<BR>", ", ").strip()
    return {
        "id":      rec.get("id", ""),
        "county":  rec.get("county", ""),
        "name":    rec.get("name", ""),
        "address": addr,
        "hours":   flatten_hours(rec.get("eventList", [])),
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

    return [record_to_row(r) for r in all_raw]


# ── Comparison logic ──────────────────────────────────────────────────────────

def load_baseline() -> dict[str, dict]:
    """Returns dict keyed by Salesforce record id."""
    if not BASELINE_CSV.exists():
        return {}
    with open(BASELINE_CSV, newline="", encoding="utf-8") as f:
        return {row["id"]: row for row in csv.DictReader(f) if row.get("id")}


def compare(baseline: dict[str, dict], current: list[dict]) -> dict:
    current_by_id = {r["id"]: r for r in current if r.get("id")}

    added, removed, modified = [], [], []

    for rid, rec in current_by_id.items():
        if rid not in baseline:
            added.append(rec)
        else:
            base = baseline[rid]
            changes: dict[str, dict] = {}
            for field in ("county", "name", "address", "hours"):
                if rec.get(field, "") != base.get(field, ""):
                    changes[field] = {"from": base.get(field, ""), "to": rec.get(field, "")}
            if changes:
                modified.append({"record": rec, "changes": changes})

    for rid, rec in baseline.items():
        if rid not in current_by_id:
            removed.append(rec)

    return {"added": added, "removed": removed, "modified": modified}


def has_changes(diff: dict) -> bool:
    return bool(diff["added"] or diff["removed"] or diff["modified"])


# ── Change log ────────────────────────────────────────────────────────────────

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
        "added":   [{"id": r["id"], "county": r["county"], "name": r["name"], "address": r["address"]}
                    for r in diff["added"]],
        "removed": [{"id": r["id"], "county": r["county"], "name": r["name"]}
                    for r in diff["removed"]],
        "modified":[{
                        "id":      e["record"]["id"],
                        "county":  e["record"]["county"],
                        "name":    e["record"]["name"],
                        "changes": e["changes"]
                    } for e in diff["modified"]],
    }
    log.append(entry)
    CHANGE_LOG.write_text(json.dumps(log, indent=2))
    print(f"  Log updated: {CHANGE_LOG} ({len(log)} total entries)")


# ── Email notification ────────────────────────────────────────────────────────

def _fmt_added(records: list[dict]) -> str:
    lines = []
    for r in records:
        lines.append(f"  + [{r['county']}]  {r['name']}")
        lines.append(f"      {r['address']}")
        if r.get("hours"):
            lines.append(f"      Hours: {r['hours'][:120]}{'...' if len(r.get('hours','')) > 120 else ''}")
    return "\n".join(lines)


def _fmt_removed(records: list[dict]) -> str:
    return "\n".join(f"  - [{r['county']}]  {r['name']}" for r in records)


def _fmt_modified(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        r = e["record"]
        lines.append(f"  ~ [{r['county']}]  {r['name']}")
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

def save_baseline(rows: list[dict]):
    with open(BASELINE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "county", "name", "address", "hours"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Baseline updated: {BASELINE_CSV} ({len(rows)} rows)")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n{'=' * 60}")
    print(f"GA Polling Monitor  —  {timestamp}")
    print(f"{'=' * 60}")

    # 1. Load baseline
    print("\n[1/4] Loading baseline CSV...")
    baseline = load_baseline()
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

    if not has_changes(diff):
        print("\n  No changes detected. Baseline unchanged.")
        return

    # 4. Handle changes
    print(f"\n[4/4] Changes detected — updating files and notifying...")
    append_to_log(diff, timestamp, len(current))
    save_baseline(current)
    send_email(diff, timestamp)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
