"""
GA May 2026 Primary - Advanced Voting Location Scraper
=======================================================
Scrapes all advanced polling locations from the Georgia Secretary of State MVP
portal for the May 19, 2026 General Primary Election.

Source: https://mvp.sos.ga.gov/s/advanced-voting-location-information
        ?election=a0pcs00000J6e6HAAR&countyName=&page=advpollingplace

Strategy:
  1. Open the page with Playwright (to pass Cloudflare + get session cookies)
  2. Intercept the first Salesforce Aura API response to capture:
       - fwuid (framework hash)
       - loaded (app version key)
       - session cookies
  3. Use page.evaluate() to make paginated `fetch()` calls from inside the
     authenticated browser context — this bypasses Cloudflare bot detection
     since the requests originate from a real browser tab.
  4. Parse the JSON, flatten the hours-of-operation list, and write a CSV.

Output: ga_may_2026_primary_polling_locations.csv
"""

import asyncio
import json
import csv
import time
import urllib.parse
from pathlib import Path
from playwright.async_api import async_playwright

# ── Constants ───────────────────────────────────────────────────────────────
ELECTION_ID   = "a0pcs00000J6e6HAAR"
PAGE_URI      = (
    "/s/advanced-voting-location-information"
    f"?election={ELECTION_ID}&countyName=&page=advpollingplace"
)
BASE_URL      = "https://mvp.sos.ga.gov"
AURA_ENDPOINT = f"{BASE_URL}/s/sfsites/aura"
PAGE_URL      = f"{BASE_URL}{PAGE_URI}"
RECORDS_PER_PAGE = 50   # max the API allows in one call
OUTPUT_CSV    = "ga_may_2026_primary_polling_locations.csv"


# ── Helpers ──────────────────────────────────────────────────────────────────

def safe_str(val) -> str:
    """Return a clean string from any JSON value."""
    if val is None:
        return ""
    if isinstance(val, list):
        return " | ".join(safe_str(v) for v in val)
    return str(val).strip()


def flatten_hours(event_list) -> str:
    """
    Convert the eventList array from the API into a readable string.
    Each event object:
      {"startDate":"04/27/2026","endDate":"05/02/2026",
       "openTime":"9:00 AM","closeTime":"5:00 PM",
       "eventType":"Advanced Polling Location","eventId":null,"description":null}
    Returns pipe-separated entries: "04/27 - 05/02 9:00 AM - 5:00 PM (Advanced Polling Location)"
    """
    if not event_list:
        return ""
    if isinstance(event_list, str):
        return event_list.strip()
    if isinstance(event_list, list):
        parts = []
        for h in event_list:
            if isinstance(h, dict):
                start  = h.get("startDate", "")
                end    = h.get("endDate",   "")
                ot     = h.get("openTime",  h.get("startTime", ""))
                ct     = h.get("closeTime", h.get("endTime",   ""))
                etype  = h.get("eventType", h.get("type",      ""))
                parts.append(f"{start} - {end} {ot} - {ct} ({etype})")
            else:
                parts.append(str(h))
        return " | ".join(parts)
    return safe_str(event_list)


def parse_records(actions_list: list) -> tuple[list[dict], int]:
    """
    Walk the Aura 'actions' array and find the getAdvPollingPlaces response.
    Returns (records_list, total_count).

    Response structure from vrWebIntegrationController.getAdvPollingPlaces:
      returnValue.returnValue = {
        "totalRecords": "300",      <- string, not int
        "ppList": "[{...}, ...]"    <- JSON-encoded STRING, must be re-parsed
      }
    """
    for action in actions_list:
        rv = action.get("returnValue", {})
        if not rv:
            continue
        inner = rv.get("returnValue", rv)

        if not isinstance(inner, dict):
            continue

        # Primary path: ppList is a JSON-encoded string
        if "ppList" in inner:
            pp_raw = inner["ppList"]
            total  = int(inner.get("totalRecords", 0))
            if isinstance(pp_raw, str):
                try:
                    records = json.loads(pp_raw)
                    return records, total
                except json.JSONDecodeError:
                    pass
            elif isinstance(pp_raw, list):
                return pp_raw, total

        # Fallback: look for any list-valued key whose items look like records
        for key, val in inner.items():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                if any(k in val[0] for k in ("name", "county", "address", "eventList")):
                    total = int(inner.get("totalRecords", inner.get("totalCount", len(val))))
                    return val, total

    return [], 0


# ── Core scrape function ──────────────────────────────────────────────────────

async def fetch_page(page, aura_context: dict, skip: int, per_page: int) -> dict:
    """
    Call vrWebIntegrationController.getAdvPollingPlaces from inside the browser
    context (inherits cookies + Cloudflare clearance automatically).
    """
    search_param = json.dumps({"election": ELECTION_ID, "countyName": ""})
    context_str  = json.dumps(aura_context)

    js = f"""
    async () => {{
        const message = {{
            actions: [{{
                id: "scraper;a",
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
            "{AURA_ENDPOINT}?r=scraper&aura.ApexAction.execute=1",
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


def parse_address(raw_addr: str) -> dict:
    """
    Parse "STREET, CITY STATE ZIP" into components.
    Returns dict with keys: line_1, city, state, zip, full, address_id
    """
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
    address_id = f"{street}_{city}_{state}_{zip_code}"

    return {"line_1": line_1, "city": city_tc, "state": state, "zip": zip_code,
            "full": full, "address_id": address_id}


def hours_advanced_only(event_list: list) -> str:
    """Return only Advanced Polling Location events, newline-separated."""
    parts = []
    for e in event_list:
        if isinstance(e, dict) and e.get("eventType") == "Advanced Polling Location":
            parts.append(
                f"{e.get('startDate','')} - {e.get('endDate','')} "
                f"{e.get('openTime','')} - {e.get('closeTime','')} "
                f"(Advanced Polling Location)"
            )
    return "\n".join(parts)


def is_dropbox_only(rec: dict) -> bool:
    """
    Returns True if every event for this location is a Dropbox Polling Location
    and there are no Advanced Polling Location events.  These rows are excluded
    from the output — they are dropoff boxes, not staffed voting sites.
    """
    events = rec.get("eventList", [])
    if not events:
        return False
    return all(
        e.get("eventType", "") == "Dropbox Polling Location"
        for e in events
        if isinstance(e, dict)
    )


def extract_record_fields(rec: dict) -> dict:
    """
    Normalise a single API record into the 16-column schema.
    """
    county   = safe_str(rec.get("county", ""))
    name_raw = safe_str(rec.get("name",   ""))
    events   = rec.get("eventList", [])

    raw_addr = rec.get("address", "")
    if isinstance(raw_addr, str):
        raw_addr = raw_addr.replace("<br>", ", ").replace("<BR>", ", ").strip()

    addr = parse_address(raw_addr)
    hours_raw = flatten_hours(events)
    hours_adv = hours_advanced_only(events)

    return {
        "address_id":                addr["address_id"],
        "polling_place_county":      county,
        "polling_place_name_raw":    name_raw,
        "polling_place_name":        name_raw,
        "polling_place_address_raw": raw_addr,
        "polling_place_address_full":addr["full"],
        "polling_place_address_line_1": addr["line_1"],
        "polling_place_address_city":   addr["city"],
        "polling_place_address_state":  addr["state"],
        "polling_place_address_zip":    addr["zip"],
        "hours_raw":                 hours_raw,
        "image_url":                 "",
        "hours_advanced_polling":    hours_adv,
        "Latitude":                  "",
        "Longitude":                 "",
        "status":                    "",
    }


async def main():
    print("=" * 60)
    print("GA May 2026 Primary — Advanced Polling Location Scraper")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context_browser = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context_browser.new_page()

        # ── Step 1: Capture aura.context from the initial page load ──────────
        captured_context = {}
        first_response_data = {}

        async def on_request(req):
            nonlocal captured_context
            if (
                "aura" in req.url
                and req.method == "POST"
                and "ApexAction" in req.url
                and not captured_context
            ):
                body = req.post_data or ""
                try:
                    # body is URL-encoded; extract aura.context
                    params = dict(part.split("=", 1) for part in body.split("&") if "=" in part)
                    ctx_raw = params.get("aura.context", "")
                    ctx_json = urllib.parse.unquote(ctx_raw)
                    captured_context = json.loads(ctx_json)
                    print(f"[✓] Captured aura.context (fwuid: {captured_context.get('fwuid','?')[:20]}...)")
                except Exception as e:
                    print(f"[!] Context capture error: {e}")

        async def on_response(resp):
            nonlocal first_response_data
            if (
                "ApexAction" in resp.url
                and not first_response_data
            ):
                try:
                    body = await resp.text()
                    data = json.loads(body)
                    first_response_data = data
                    # Peek at total record count
                    for action in data.get("actions", []):
                        rv = action.get("returnValue", {})
                        if rv and "returnValue" in rv:
                            inner = rv["returnValue"]
                            if isinstance(inner, dict) and "totalCount" in inner:
                                print(f"[✓] Total polling places: {inner['totalCount']}")
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        print(f"\nLoading page: {PAGE_URL}")
        await page.goto(PAGE_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)

        if not captured_context:
            print("[!] Could not auto-capture context. Attempting manual extraction...")
            # Fall back to extracting from JS globals
            try:
                fw = await page.evaluate("window.Aura && window.Aura.appBootstrap ? 'found' : 'missing'")
                print(f"  Aura bootstrap: {fw}")
            except Exception:
                pass

        # Use a minimal working context if capture failed
        if not captured_context:
            print("[!] Using hardcoded context (may need updating if site changes)")
            captured_context = {
                "mode": "PROD",
                "fwuid": "TXFWNVprQUZzQnEtNXVXYTFLQ2ppdzJEa1N5enhOU3R5QWl2VzNveFZTbGcxMy4tMjE0NzQ4MzY0OC4xMzEwNzIwMA",
                "app": "siteforce:communityApp",
                "loaded": {
                    "APPLICATION@markup://siteforce:communityApp": "1537_wmTAUxhOaM_47EClrN56Dw"
                },
                "dn": [],
                "globals": {},
                "uad": True
            }

        # ── Step 2: Probe first page to get total count & field structure ────
        print("\nFetching first page to probe API structure...")
        probe = await fetch_page(page, captured_context, skip=0, per_page=RECORDS_PER_PAGE)

        # Save raw probe for debugging
        with open("probe_response.json", "w") as f:
            json.dump(probe, f, indent=2)
        print("  Probe saved to probe_response.json")

        # Print full structure of first record for debugging
        actions = probe.get("actions", [])
        records, total_count = parse_records(actions)

        if not records:
            print("\n[!] Could not auto-parse records. Inspecting raw response...")
            print(json.dumps(probe, indent=2)[:3000])
            await browser.close()
            return

        print(f"\n[✓] Found {len(records)} records in first batch (total: {total_count})")
        print(f"    Sample record keys: {list(records[0].keys())}")
        print(f"    Sample record:\n{json.dumps(records[0], indent=4)[:600]}")

        # ── Step 3: Paginate through all results ─────────────────────────────
        all_records = list(records)
        skip = RECORDS_PER_PAGE

        if total_count > 0:
            total_pages = (total_count + RECORDS_PER_PAGE - 1) // RECORDS_PER_PAGE
        else:
            # Keep fetching until we get an empty page
            total_pages = 999

        print(f"\nPaginating ({total_pages} pages estimated, {RECORDS_PER_PAGE} records/page)...")

        page_num = 2
        while skip < (total_count if total_count > 0 else 99999):
            print(f"  Page {page_num}: skip={skip}...", end=" ", flush=True)
            time.sleep(0.5)   # polite rate limiting

            batch_resp = await fetch_page(page, captured_context, skip=skip, per_page=RECORDS_PER_PAGE)
            batch, _ = parse_records(batch_resp.get("actions", []))

            if not batch:
                print(f"empty – stopping.")
                break

            all_records.extend(batch)
            print(f"got {len(batch)} records (total so far: {len(all_records)})")
            skip += RECORDS_PER_PAGE
            page_num += 1

        print(f"\n[✓] Collected {len(all_records)} total records")

        # ── Step 4: Write CSV ─────────────────────────────────────────────────
        csv_fields = [
            "address_id", "polling_place_county", "polling_place_name_raw",
            "polling_place_name", "polling_place_address_raw",
            "polling_place_address_full", "polling_place_address_line_1",
            "polling_place_address_city", "polling_place_address_state",
            "polling_place_address_zip", "hours_raw", "image_url",
            "hours_advanced_polling", "Latitude", "Longitude", "status",
        ]

        # Filter dropbox-only locations, then extract fields
        filtered_records = [r for r in all_records if not is_dropbox_only(r)]
        dropped = len(all_records) - len(filtered_records)
        if dropped:
            print(f"  Filtered out {dropped} dropbox-only location(s)")

        rows = [extract_record_fields(r) for r in filtered_records]

        # De-duplicate (same id may appear across pages in some Salesforce setups)
        seen_keys: set[str] = set()
        unique_rows = []
        for r, raw in zip(rows, filtered_records):
            key = raw.get("id") or f"{r['county']}|{r['name']}|{r['address']}"
            if key not in seen_keys:
                seen_keys.add(key)
                unique_rows.append(r)
        rows = unique_rows

        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        print(f"[✓] CSV written: {OUTPUT_CSV}  ({len(rows)} rows)")

        # Also save raw JSON for reference
        with open("all_records_raw.json", "w") as f:
            json.dump(all_records, f, indent=2)
        print(f"[✓] Raw JSON saved: all_records_raw.json")

        await browser.close()

    # Quick summary
    print("\n── Field Coverage Summary ──────────────────────────────────────")
    county_counts: dict[str, int] = {}
    empty_hours = 0
    empty_addr  = 0
    for r in rows:
        county_counts[r["county"]] = county_counts.get(r["county"], 0) + 1
        if not r["hours"]:  empty_hours += 1
        if not r["address"]: empty_addr += 1
    print(f"  Counties found:      {len(county_counts)}")
    print(f"  Total locations:     {len(rows)}")
    print(f"  Missing hours:       {empty_hours}")
    print(f"  Missing address:     {empty_addr}")
    print(f"\nTop 5 counties by location count:")
    for county, cnt in sorted(county_counts.items(), key=lambda x: -x[1])[:5]:
        print(f"  {county:<20} {cnt}")


if __name__ == "__main__":
    asyncio.run(main())
