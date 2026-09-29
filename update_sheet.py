"""
Google Sheets Sync — GA November 2026 General & Special Elections Polling Locations
======================================================================================
Two tabs, both written as wholesale clear+rewrite every run — never per-cell
matched updates. That's deliberate: the old design matched existing sheet rows
by COUNTY||NAME text and patched individual cells, which meant a stray manual
sort/edit on the sheet between runs could silently pair one location's name+
address with a different row's county. Rewriting the full tab each run from
data keyed by the stable Salesforce location_id removes that failure mode —
worst case a bad run redraws the whole tab correctly next time, rather than
leaving one corrupted row sitting there indefinitely.

  "raw"  tab — exactly what was scraped this run, one row per location_id,
               no history, no manual columns. Overwritten completely each run.

  main GA_EV tab (gid 0) — the human-facing view. Columns A–O are unchanged
  from before; location_id is appended as a new column P so nothing that
  already reads this sheet by position (e.g. county_coverage.gs, which reads
  column B for county) breaks.
    A  address_id
    B  polling_place_county
    C  polling_place_name
    D  polling_place_address_full
    E  polling_place_address_line_1
    F  polling_place_address_city
    G  polling_place_address_state
    H  polling_place_address_zip
    I  image_url            ← preserved across rebuilds; not scraped data
    J  hours_advanced_polling
    K  Latitude             ← preserved across rebuilds; requires geocoding
    L  Longitude            ← preserved across rebuilds
    M  status               ← recomputed fresh each run from history + diff
    N  date_added
    O  date_removed
    P  location_id          ← stable join key

The row set and M/N/O values come from check_for_changes.build_formatted_rows()
(driven by location_history.json), not from anything read out of this sheet.
Only I/K/L (image_url, Latitude, Longitude) are read back from the existing
sheet before rewriting, since those are populated by a separate/manual process
this script doesn't own and has no other source for.
"""

import json
import os

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1MPKVyxdFGWwQt2-uXDTQ6LTGa6UPH61tTrab1D4cFCs"
WORKSHEET_GID  = 0
RAW_TAB_TITLE  = "raw"

FORMATTED_HEADER = [
    "address_id", "county", "name", "address_full", "address_line_1",
    "address_city", "address_state", "address_zip", "image_url",
    "hours_advanced_polling", "Latitude", "Longitude", "status",
    "date_added", "date_removed", "location_id",
]

RAW_HEADER = [
    "location_id", "county", "name", "address_id", "address_full",
    "address_line_1", "address_city", "address_state", "address_zip",
    "hours_advanced_polling", "scraped_at_utc",
]


def _get_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS environment variable not set")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)


def _get_formatted_worksheet(client: gspread.Client) -> gspread.Worksheet:
    sh = client.open_by_key(SPREADSHEET_ID)
    return sh.get_worksheet_by_id(WORKSHEET_GID)


def _get_or_create_raw_worksheet(client: gspread.Client) -> gspread.Worksheet:
    sh = client.open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(RAW_TAB_TITLE)
    except gspread.WorksheetNotFound:
        print(f"  '{RAW_TAB_TITLE}' tab not found — creating it.")
        return sh.add_worksheet(title=RAW_TAB_TITLE, rows=1000, cols=len(RAW_HEADER))


def write_raw_tab(current: list[dict]) -> int:
    """Wholesale overwrite of the 'raw' tab with exactly this run's scrape."""
    from datetime import datetime, timezone
    print("  Connecting to Google Sheets (raw tab)...")
    client = _get_client()
    ws     = _get_or_create_raw_worksheet(client)

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [[
        r.get("location_id", ""),
        r.get("polling_place_county", ""),
        r.get("polling_place_name", ""),
        r.get("address_id", ""),
        r.get("polling_place_address_full", ""),
        r.get("polling_place_address_line_1", ""),
        r.get("polling_place_address_city", ""),
        r.get("polling_place_address_state", ""),
        r.get("polling_place_address_zip", ""),
        r.get("hours_advanced_polling", ""),
        scraped_at,
    ] for r in current]

    ws.clear()
    ws.update("A1", [RAW_HEADER] + rows, value_input_option="RAW")
    print(f"  Raw tab rewritten: {len(rows)} rows")
    return len(rows)


def rebuild_formatted_tab(rows: list[dict]) -> int:
    """
    Wholesale overwrite of the main GA_EV tab from `rows` (as built by
    check_for_changes.build_formatted_rows()), preserving image_url/
    Latitude/Longitude for any location_id that already had them set.
    """
    print("  Connecting to Google Sheets (formatted tab)...")
    client = _get_client()
    ws     = _get_formatted_worksheet(client)

    existing = ws.get_all_values()
    preserved: dict[str, tuple[str, str, str]] = {}
    if existing:
        header = existing[0]
        try:
            i_img, i_lat, i_lon, i_id = (
                header.index("image_url"), header.index("Latitude"),
                header.index("Longitude"), header.index("location_id"),
            )
            for row in existing[1:]:
                if len(row) <= i_id or not row[i_id]:
                    continue
                get = lambda i: row[i] if len(row) > i else ""
                preserved[row[i_id]] = (get(i_img), get(i_lat), get(i_lon))
        except ValueError:
            pass  # sheet doesn't have the expected header yet (first run)

    out_rows = []
    for r in rows:
        image_url, lat, lon = preserved.get(r["location_id"], ("", "", ""))
        out_rows.append([
            r.get("address_id", ""),
            r.get("polling_place_county", ""),
            r.get("polling_place_name", ""),
            r.get("polling_place_address_full", ""),
            r.get("polling_place_address_line_1", ""),
            r.get("polling_place_address_city", ""),
            r.get("polling_place_address_state", ""),
            r.get("polling_place_address_zip", ""),
            image_url,
            r.get("hours_advanced_polling", ""),
            lat,
            lon,
            r.get("status", ""),
            r.get("date_added", ""),
            r.get("date_removed", ""),
            r["location_id"],
        ])

    ws.clear()
    ws.update("A1", [FORMATTED_HEADER] + out_rows, value_input_option="RAW")
    print(f"  Formatted tab rewritten: {len(out_rows)} rows "
          f"({len(preserved)} had preserved image/lat/lon)")
    return len(out_rows)


if __name__ == "__main__":
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        print("Set GOOGLE_CREDENTIALS env var first.")
    else:
        print("Run check_for_changes.py to trigger a sync.")
