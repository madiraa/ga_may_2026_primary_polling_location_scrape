"""
Google Sheets Sync — GA May 2026 Primary Polling Locations
===========================================================
Called automatically by check_for_changes.py when changes are detected.
Can also be run standalone to do a full sync at any time.

Spreadsheet : https://docs.google.com/spreadsheets/d/192ufA2ffXqTTsQQxJylg1mMC5TBbHndfgIu5UI4WDGQ
Tab (gid)   : 571990717

Column layout (must match the sheet exactly):
  A  county
  B  name
  C  address
  D  hours
  E  google bucket link   ← never touched by this script
  F  image                ← never touched by this script
  G  status               ← written/cleared automatically by this script

Status column (G) lifecycle:
  • On ADDED row   → stamped "ADDED MM/DD/YYYY"
  • On MODIFIED row → stamped "MODIFIED: address, hours MM/DD/YYYY"
  • Next run where that row's data is stable (matches current SOS data) → cleared automatically

Matching key: (county, name)  — case-insensitive strip comparison

Required env var:
  GOOGLE_CREDENTIALS  — full JSON content of a Google Service Account key
                        that has been granted Editor access to the sheet.

Run standalone:
  GOOGLE_CREDENTIALS=$(cat your-service-account.json) python update_sheet.py
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────
SPREADSHEET_ID = "192ufA2ffXqTTsQQxJylg1mMC5TBbHndfgIu5UI4WDGQ"
WORKSHEET_GID  = 571990717

# Columns (1-indexed for gspread row updates)
COL_COUNTY  = 1   # A
COL_NAME    = 2   # B
COL_ADDRESS = 3   # C
COL_HOURS   = 4   # D
# Columns E (5) and F (6) are never written by this script
COL_STATUS  = 7   # G — auto-stamped on change, auto-cleared when stable


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%m/%d/%Y")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_client() -> gspread.Client:
    creds_json = os.getenv("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise EnvironmentError(
            "GOOGLE_CREDENTIALS env var is not set. "
            "Set it to the full JSON content of your service account key."
        )
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_worksheet(client: gspread.Client) -> gspread.Worksheet:
    sh = client.open_by_key(SPREADSHEET_ID)
    try:
        return sh.get_worksheet_by_id(WORKSHEET_GID)
    except Exception:
        # Fallback: first sheet
        return sh.get_worksheet(0)


# ── Core sync logic ───────────────────────────────────────────────────────────

def _normalize_key(county: str, name: str) -> str:
    return f"{county.strip().upper()}||{name.strip().upper()}"


def _read_sheet(ws: gspread.Worksheet) -> tuple[list[list], dict[str, int]]:
    """
    Returns (all_rows, key_to_row_index).
    all_rows is 0-indexed list of lists (row 0 = header).
    key_to_row_index maps normalized 'COUNTY||NAME' → 0-based row index.
    """
    all_rows = ws.get_all_values()
    index: dict[str, int] = {}
    for i, row in enumerate(all_rows):
        if i == 0:
            continue  # skip header
        county = row[0] if len(row) > 0 else ""
        name   = row[1] if len(row) > 1 else ""
        if county or name:
            index[_normalize_key(county, name)] = i
    return all_rows, index


def sync_changes(diff: dict) -> dict[str, int]:
    """
    Apply a diff (from check_for_changes.compare()) to the Google Sheet.

    diff keys: added (list of row dicts), removed (list), modified (list of {record, changes})

    Returns counts: {"appended": N, "updated": N, "skipped_removed": N}
    """
    if not (diff.get("added") or diff.get("modified")):
        print("  No adds/modifications to sync to sheet.")
        return {"appended": 0, "updated": 0, "skipped_removed": len(diff.get("removed", []))}

    print("  Connecting to Google Sheets...")
    client = _get_client()
    ws     = _get_worksheet(client)

    all_rows, key_index = _read_sheet(ws)
    total_rows = len(all_rows)

    appended = 0
    updated  = 0

    # ── Handle modified rows — collect all cell updates, write in one batch ───
    all_cell_updates: list[gspread.Cell] = []
    fallback_new: list[list] = []

    for entry in diff.get("modified", []):
        rec     = entry["record"]
        changes = entry["changes"]
        key     = _normalize_key(rec["county"], rec["name"])
        row_idx = key_index.get(key)

        if row_idx is None:
            print(f"  [NEW via modified] {rec['county']} — {rec['name']}")
            fallback_new.append([rec["county"], rec["name"], rec["address"], rec["hours"],
                                  "", "", f"ADDED {_today()}"])
            continue

        sheet_row_num = row_idx + 1
        changed_fields = [f for f in ("county", "name", "address", "hours") if f in changes]
        if "county" in changes:
            all_cell_updates.append(gspread.Cell(sheet_row_num, COL_COUNTY, rec["county"]))
        if "name" in changes:
            all_cell_updates.append(gspread.Cell(sheet_row_num, COL_NAME, rec["name"]))
        if "address" in changes:
            all_cell_updates.append(gspread.Cell(sheet_row_num, COL_ADDRESS, rec["address"]))
        if "hours" in changes:
            all_cell_updates.append(gspread.Cell(sheet_row_num, COL_HOURS, rec["hours"]))
        if changed_fields:
            status = f"MODIFIED: {', '.join(changed_fields)} {_today()}"
            all_cell_updates.append(gspread.Cell(sheet_row_num, COL_STATUS, status))
            print(f"  [UPDATE {'+'.join(changed_fields)}] row {sheet_row_num}: "
                  f"{rec['county']} — {rec['name']}")
            updated += 1

    if all_cell_updates:
        ws.update_cells(all_cell_updates, value_input_option="RAW")
        time.sleep(1.2)

    # ── Handle new locations (append rows) ────────────────────────────────────
    # Columns: A county | B name | C address | D hours | E blank | F blank | G status
    new_rows = fallback_new[:]
    for rec in diff.get("added", []):
        print(f"  [APPEND] {rec['county']} — {rec['name']}  |  {rec['address']}")
        new_rows.append([rec["county"], rec["name"], rec["address"], rec["hours"],
                         "", "", f"ADDED {_today()}"])

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW",
                       insert_data_option="INSERT_ROWS", table_range="A1")
        appended += len(new_rows)

    # ── Removed locations: log only, don't delete from sheet ─────────────────
    skipped = 0
    for rec in diff.get("removed", []):
        print(f"  [SKIP REMOVE] {rec.get('county','')} — {rec.get('name','')} "
              f"(kept in sheet; check manually)")
        skipped += 1

    print(f"\n  Sheet sync complete — appended: {appended}, updated: {updated}, "
          f"removals skipped: {skipped}")
    return {"appended": appended, "updated": updated, "skipped_removed": skipped}


def clear_resolved_statuses(current: list[dict]) -> int:
    """
    For every row in the sheet that has a status tag in column G:
      - If the row's county, name, address, and hours now match the current
        scraped data → clear column G (the change has been resolved / confirmed)
      - Otherwise → leave the tag in place

    Called on every monitor run so tags auto-clear one cycle after the data
    stabilises.  Returns the number of cells cleared.
    """
    print("  Checking for resolved status tags to clear...")
    client = _get_client()
    ws     = _get_worksheet(client)

    all_rows = ws.get_all_values()

    # Build lookup: COUNTY||NAME → scraped row dict
    current_lookup: dict[str, dict] = {
        _normalize_key(r.get("county", ""), r.get("name", "")): r
        for r in current
    }

    clears: list[gspread.Cell] = []

    for i, row in enumerate(all_rows):
        if i == 0:
            continue  # header
        status = row[6].strip() if len(row) > 6 else ""
        if not status:
            continue  # no tag, skip

        county  = row[0].strip() if len(row) > 0 else ""
        name    = row[1].strip() if len(row) > 1 else ""
        address = row[2].strip() if len(row) > 2 else ""
        hours   = row[3].strip() if len(row) > 3 else ""

        key = _normalize_key(county, name)
        scraped = current_lookup.get(key)

        if scraped and (
            scraped.get("address", "").strip() == address
            and scraped.get("hours",   "").strip() == hours
            and scraped.get("county",  "").strip().upper() == county.upper()
            and scraped.get("name",    "").strip().upper() == name.upper()
        ):
            clears.append(gspread.Cell(i + 1, COL_STATUS, ""))
            print(f"  [CLEAR status] row {i+1}: {county} — {name}  (was: {status})")

    if clears:
        ws.update_cells(clears, value_input_option="RAW")
        print(f"  Cleared {len(clears)} resolved status tag(s)")
    else:
        print("  No resolved tags to clear.")

    return len(clears)


def full_sync(current_rows: list[dict]) -> dict[str, int]:
    """
    One-shot: compare ALL scraped rows against the sheet and push any missing
    or changed county/name/address/hours values.  Preserves columns E and F.

    Batches ALL cell updates into a single API call to stay within the
    Google Sheets write-quota (60 requests/min).
    """
    print("  Connecting to Google Sheets (full sync)...")
    client = _get_client()
    ws     = _get_worksheet(client)

    all_sheet_rows, key_index = _read_sheet(ws)

    all_cell_updates: list[gspread.Cell] = []
    new_batch: list[list] = []
    updated = 0

    for rec in current_rows:
        key     = _normalize_key(rec["county"], rec["name"])
        row_idx = key_index.get(key)

        if row_idx is None:
            new_batch.append([rec["county"], rec["name"], rec["address"], rec["hours"]])
            print(f"  [NEW] {rec['county']} — {rec['name']}")
        else:
            sheet_row     = all_sheet_rows[row_idx]
            sheet_addr    = sheet_row[2] if len(sheet_row) > 2 else ""
            sheet_hrs     = sheet_row[3] if len(sheet_row) > 3 else ""
            sheet_row_num = row_idx + 1
            row_changed   = False

            if rec["address"].strip() != sheet_addr.strip():
                all_cell_updates.append(gspread.Cell(sheet_row_num, COL_ADDRESS, rec["address"]))
                row_changed = True
            if rec["hours"].strip() != sheet_hrs.strip():
                all_cell_updates.append(gspread.Cell(sheet_row_num, COL_HOURS, rec["hours"]))
                row_changed = True

            if row_changed:
                updated += 1
                print(f"  [UPDATE] row {sheet_row_num}: {rec['county']} — {rec['name']}")

    # Single batched write for all cell changes
    if all_cell_updates:
        print(f"\n  Writing {len(all_cell_updates)} cell updates in one batch...")
        # Google Sheets API limits to 2MB per request; chunk at 1000 cells to be safe
        chunk_size = 1000
        for i in range(0, len(all_cell_updates), chunk_size):
            chunk = all_cell_updates[i:i + chunk_size]
            ws.update_cells(chunk, value_input_option="RAW")
            if i + chunk_size < len(all_cell_updates):
                time.sleep(1.2)   # stay under 60 writes/min quota

    # Append all new rows at once
    if new_batch:
        print(f"\n  Appending {len(new_batch)} new rows...")
        time.sleep(1.2)
        ws.append_rows(new_batch, value_input_option="RAW",
                       insert_data_option="INSERT_ROWS", table_range="A1")

    appended = len(new_batch)
    print(f"\n  Full sync done — appended: {appended}, updated: {updated}")
    return {"appended": appended, "updated": updated}


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import csv
    from pathlib import Path

    csv_path = Path("ga_may_2026_primary_polling_locations.csv")
    if not csv_path.exists():
        print(f"[!] {csv_path} not found. Run scrape_polling_places.py first.")
        raise SystemExit(1)

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"Loaded {len(rows)} rows from {csv_path}")
    result = full_sync(rows)
    print(f"\nResult: {result}")
