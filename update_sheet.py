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

    # ── Handle modified rows (update address and/or hours in-place) ───────────
    for entry in diff.get("modified", []):
        rec     = entry["record"]
        changes = entry["changes"]
        key     = _normalize_key(rec["county"], rec["name"])
        row_idx = key_index.get(key)

        if row_idx is None:
            # Not found in sheet — treat as new
            print(f"  [NEW via modified] {rec['county']} — {rec['name']}")
            ws.append_row(
                [rec["county"], rec["name"], rec["address"], rec["hours"]],
                value_input_option="RAW",
                insert_data_option="INSERT_ROWS",
                table_range="A1",
            )
            appended += 1
            time.sleep(0.3)
            continue

        sheet_row_num = row_idx + 1  # gspread uses 1-based row numbers
        cell_updates  = []

        if "address" in changes:
            cell_updates.append(
                gspread.Cell(sheet_row_num, COL_ADDRESS, rec["address"])
            )
            print(f"  [UPDATE address] row {sheet_row_num}: {rec['county']} — {rec['name']}")

        if "hours" in changes:
            cell_updates.append(
                gspread.Cell(sheet_row_num, COL_HOURS, rec["hours"])
            )
            print(f"  [UPDATE hours]   row {sheet_row_num}: {rec['county']} — {rec['name']}")

        if cell_updates:
            ws.update_cells(cell_updates, value_input_option="RAW")
            updated += 1
            time.sleep(0.3)

    # ── Handle new locations (append rows) ────────────────────────────────────
    if diff.get("added"):
        new_rows = []
        for rec in diff["added"]:
            print(f"  [APPEND] {rec['county']} — {rec['name']}  |  {rec['address']}")
            new_rows.append([rec["county"], rec["name"], rec["address"], rec["hours"]])

        # Batch-append all new rows at once
        if new_rows:
            ws.append_rows(
                new_rows,
                value_input_option="RAW",
                insert_data_option="INSERT_ROWS",
                table_range="A1",
            )
            appended += len(new_rows)
            time.sleep(0.5)

    # ── Removed locations: log only, don't delete from sheet ─────────────────
    skipped = 0
    for rec in diff.get("removed", []):
        print(f"  [SKIP REMOVE] {rec.get('county','')} — {rec.get('name','')} "
              f"(kept in sheet; check manually)")
        skipped += 1

    print(f"\n  Sheet sync complete — appended: {appended}, updated: {updated}, "
          f"removals skipped: {skipped}")
    return {"appended": appended, "updated": updated, "skipped_removed": skipped}


def full_sync(current_rows: list[dict]) -> dict[str, int]:
    """
    One-shot: compare ALL scraped rows against the sheet and push any missing
    or changed county/name/address/hours values.  Preserves columns E and F.
    Called from the standalone __main__ block below.
    """
    print("  Connecting to Google Sheets (full sync)...")
    client = _get_client()
    ws     = _get_worksheet(client)

    all_sheet_rows, key_index = _read_sheet(ws)

    appended = 0
    updated  = 0
    new_batch = []

    for rec in current_rows:
        key     = _normalize_key(rec["county"], rec["name"])
        row_idx = key_index.get(key)

        if row_idx is None:
            new_batch.append([rec["county"], rec["name"], rec["address"], rec["hours"]])
            print(f"  [NEW] {rec['county']} — {rec['name']}")
        else:
            sheet_row  = all_sheet_rows[row_idx]
            sheet_addr = sheet_row[2] if len(sheet_row) > 2 else ""
            sheet_hrs  = sheet_row[3] if len(sheet_row) > 3 else ""

            cell_updates = []
            sheet_row_num = row_idx + 1

            if rec["address"].strip() != sheet_addr.strip():
                cell_updates.append(gspread.Cell(sheet_row_num, COL_ADDRESS, rec["address"]))
            if rec["hours"].strip() != sheet_hrs.strip():
                cell_updates.append(gspread.Cell(sheet_row_num, COL_HOURS, rec["hours"]))

            if cell_updates:
                ws.update_cells(cell_updates, value_input_option="RAW")
                updated += 1
                print(f"  [UPDATE] row {sheet_row_num}: {rec['county']} — {rec['name']}")
                time.sleep(0.3)

    if new_batch:
        ws.append_rows(new_batch, value_input_option="RAW",
                       insert_data_option="INSERT_ROWS", table_range="A1")
        appended += len(new_batch)

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
