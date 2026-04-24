"""
Google Sheets Sync — GA May 2026 Primary Polling Locations
===========================================================
Called automatically by check_for_changes.py when changes are detected.
Can also be run standalone to do a full sync at any time.

Spreadsheet : https://docs.google.com/spreadsheets/d/192ufA2ffXqTTsQQxJylg1mMC5TBbHndfgIu5UI4WDGQ
Tab (gid)   : 571990717

Column layout (A–P):
  A  address_id                  ← {STREET}_{CITY}_{STATE}_{ZIP}
  B  polling_place_county
  C  polling_place_name_raw
  D  polling_place_name
  E  polling_place_address_raw
  F  polling_place_address_full
  G  polling_place_address_line_1
  H  polling_place_address_city
  I  polling_place_address_state
  J  polling_place_address_zip
  K  hours_raw                   ← all events, pipe-separated
  L  image_url                   ← never overwritten if already set
  M  hours_advanced_polling      ← Advanced Polling Location only, newline-separated
  N  Latitude                    ← blank for new rows (requires geocoding)
  O  Longitude                   ← blank for new rows (requires geocoding)
  P  status                      ← auto-stamped on change, auto-cleared when stable

Status column (P) lifecycle:
  • On ADDED row    → "ADDED MM/DD/YYYY"
  • On MODIFIED row → "MODIFIED: field1, field2 MM/DD/YYYY"
  • Next stable run → cleared automatically

Matching key: polling_place_county || polling_place_name_raw

Required env var:
  GOOGLE_CREDENTIALS  — full JSON content of a Google Service Account key
                        that has been granted Editor access to the sheet.

Run standalone:
  GOOGLE_CREDENTIALS=$(cat your-service-account.json) python update_sheet.py        # push
  GOOGLE_CREDENTIALS=$(cat your-service-account.json) python update_sheet.py pull   # pull
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

# Columns (1-indexed for gspread)
COL_ADDRESS_ID    =  1   # A
COL_COUNTY        =  2   # B
COL_NAME_RAW      =  3   # C
COL_NAME          =  4   # D
COL_ADDRESS_RAW   =  5   # E
COL_ADDRESS_FULL  =  6   # F
COL_ADDRESS_LINE1 =  7   # G
COL_ADDRESS_CITY  =  8   # H
COL_ADDRESS_STATE =  9   # I
COL_ADDRESS_ZIP   = 10   # J
COL_HOURS_RAW     = 11   # K
COL_IMAGE_URL     = 12   # L  ← never overwritten if already populated
COL_HOURS_ADV     = 13   # M
COL_LATITUDE      = 14   # N  ← blank for new rows
COL_LONGITUDE     = 15   # O  ← blank for new rows
COL_STATUS        = 16   # P  ← auto-stamped, auto-cleared
COL_DATE_ADDED    = 17   # Q  ← stamped once on first append, never cleared

# Fields used for change detection (compared against sheet columns)
COMPARE_FIELDS = {
    "polling_place_county":   COL_COUNTY,
    "polling_place_name_raw": COL_NAME_RAW,
    "polling_place_address_raw": COL_ADDRESS_RAW,
    "hours_raw":              COL_HOURS_RAW,
}


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
    import re
    c = re.sub(r'\s+', ' ', county.strip()).upper()
    n = re.sub(r'\s+', ' ', name.strip()).upper()
    return f"{c}||{n}"


def _read_sheet(ws: gspread.Worksheet) -> tuple[list[list], dict[str, int]]:
    """
    Returns (all_rows, key_to_row_index).
    all_rows is 0-indexed (row 0 = header).
    key_to_row_index maps 'COUNTY||NAME_RAW' → 0-based row index.
    Column B = county (index 1), Column C = name_raw (index 2).
    """
    all_rows = ws.get_all_values()
    index: dict[str, int] = {}
    for i, row in enumerate(all_rows):
        if i == 0:
            continue
        county   = row[COL_COUNTY   - 1] if len(row) >= COL_COUNTY   else ""
        name_raw = row[COL_NAME_RAW - 1] if len(row) >= COL_NAME_RAW else ""
        if county or name_raw:
            index[_normalize_key(county, name_raw)] = i
    return all_rows, index


def _build_new_row(rec: dict) -> list:
    """
    Build a 16-column sheet row from a scraped record dict.
    Latitude/Longitude left blank (require geocoding).
    image_url left blank for new rows.
    """
    return [
        rec.get("address_id",              ""),   # A
        rec.get("polling_place_county",    ""),   # B
        rec.get("polling_place_name_raw",  ""),   # C
        rec.get("polling_place_name",      ""),   # D
        rec.get("polling_place_address_raw",  ""), # E
        rec.get("polling_place_address_full", ""), # F
        rec.get("polling_place_address_line_1",""),# G
        rec.get("polling_place_address_city", ""), # H
        rec.get("polling_place_address_state",""), # I
        rec.get("polling_place_address_zip",  ""), # J
        rec.get("hours_raw",               ""),   # K
        rec.get("image_url",               ""),   # L
        rec.get("hours_advanced_polling",  ""),   # M
        "",                                        # N Latitude (blank)
        "",                                        # O Longitude (blank)
        f"ADDED {_today()}",                       # P status
        _today(),                                  # Q date_added (permanent)
    ]


def sync_changes(diff: dict) -> dict[str, int]:
    """
    Apply a diff (from check_for_changes.compare()) to the Google Sheet.
    Returns counts: {"appended": N, "updated": N, "skipped_removed": N}
    """
    if not (diff.get("added") or diff.get("modified") or diff.get("removed")):
        print("  No changes to sync to sheet.")
        return {"appended": 0, "updated": 0, "skipped_removed": 0}

    print("  Connecting to Google Sheets...")
    client = _get_client()
    ws     = _get_worksheet(client)

    all_rows, key_index = _read_sheet(ws)
    appended = 0
    updated  = 0

    # ── Modified rows — collect all cell updates, write in one batch ──────────
    all_cell_updates: list[gspread.Cell] = []
    fallback_new: list[list] = []

    # Map from diff field names → column numbers
    field_col_map = {
        "polling_place_county":      COL_COUNTY,
        "polling_place_name_raw":    COL_NAME_RAW,
        "polling_place_name":        COL_NAME,
        "polling_place_address_raw": COL_ADDRESS_RAW,
        "polling_place_address_full":COL_ADDRESS_FULL,
        "polling_place_address_line_1": COL_ADDRESS_LINE1,
        "polling_place_address_city":COL_ADDRESS_CITY,
        "polling_place_address_state":COL_ADDRESS_STATE,
        "polling_place_address_zip": COL_ADDRESS_ZIP,
        "hours_raw":                 COL_HOURS_RAW,
        "hours_advanced_polling":    COL_HOURS_ADV,
    }

    for entry in diff.get("modified", []):
        rec     = entry["record"]
        changes = entry["changes"]
        key     = _normalize_key(
            rec.get("polling_place_county", ""),
            rec.get("polling_place_name_raw", "")
        )
        row_idx = key_index.get(key)

        if row_idx is None:
            print(f"  [NEW via modified] {rec.get('polling_place_county','')} — "
                  f"{rec.get('polling_place_name_raw','')}")
            fallback_new.append(_build_new_row(rec))
            continue

        sheet_row_num  = row_idx + 1
        changed_fields = list(changes.keys())

        for field, col in field_col_map.items():
            if field in changes:
                all_cell_updates.append(gspread.Cell(sheet_row_num, col, rec.get(field, "")))

        if changed_fields:
            status = f"MODIFIED: {', '.join(changed_fields)} {_today()}"
            all_cell_updates.append(gspread.Cell(sheet_row_num, COL_STATUS, status))
            print(f"  [UPDATE {'+'.join(changed_fields)}] row {sheet_row_num}: "
                  f"{rec.get('polling_place_county','')} — {rec.get('polling_place_name_raw','')}")
            updated += 1

    if all_cell_updates:
        ws.update_cells(all_cell_updates, value_input_option="RAW")
        time.sleep(1.2)

    # ── New locations ─────────────────────────────────────────────────────────
    new_rows = fallback_new[:]
    for rec in diff.get("added", []):
        print(f"  [APPEND] {rec.get('polling_place_county','')} — "
              f"{rec.get('polling_place_name_raw','')}  |  "
              f"{rec.get('polling_place_address_raw','')}")
        new_rows.append(_build_new_row(rec))

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW",
                       insert_data_option="INSERT_ROWS", table_range="A1")
        appended += len(new_rows)

    # ── Removed: stamp status column, keep row ────────────────────────────────
    skipped = 0
    remove_updates: list[gspread.Cell] = []
    for rec in diff.get("removed", []):
        county   = rec.get("polling_place_county", "")
        name_raw = rec.get("polling_place_name_raw", "")
        key      = _normalize_key(county, name_raw)
        row_idx  = key_index.get(key)
        if row_idx is not None:
            sheet_row_num = row_idx + 1
            remove_updates.append(
                gspread.Cell(sheet_row_num, COL_STATUS, f"REMOVED {_today()}")
            )
            print(f"  [REMOVED tag] row {sheet_row_num}: {county} — {name_raw}")
        else:
            print(f"  [SKIP REMOVE — not found in sheet] {county} — {name_raw}")
        skipped += 1

    if remove_updates:
        if all_cell_updates or new_rows:
            time.sleep(1.2)
        ws.update_cells(remove_updates, value_input_option="RAW")

    print(f"\n  Sheet sync complete — appended: {appended}, updated: {updated}, "
          f"removals skipped: {skipped}")
    return {"appended": appended, "updated": updated, "skipped_removed": skipped}


def clear_resolved_statuses(current: list[dict]) -> int:
    """
    For every row in the sheet with a status tag in column P:
      - If the row's county, name_raw, address_raw, and hours_raw now match
        the current scraped data → clear column P
      - Otherwise → leave the tag

    Returns the number of cells cleared.
    """
    print("  Checking for resolved status tags to clear...")
    client = _get_client()
    ws     = _get_worksheet(client)
    all_rows = ws.get_all_values()

    current_lookup: dict[str, dict] = {
        _normalize_key(
            r.get("polling_place_county", ""),
            r.get("polling_place_name_raw", "")
        ): r
        for r in current
    }

    clears: list[gspread.Cell] = []

    for i, row in enumerate(all_rows):
        if i == 0:
            continue
        status = row[COL_STATUS - 1].strip() if len(row) >= COL_STATUS else ""
        if not status:
            continue

        county   = row[COL_COUNTY   - 1].strip() if len(row) >= COL_COUNTY   else ""
        name_raw = row[COL_NAME_RAW - 1].strip() if len(row) >= COL_NAME_RAW else ""
        addr_raw = row[COL_ADDRESS_RAW - 1].strip() if len(row) >= COL_ADDRESS_RAW else ""
        hrs_raw  = row[COL_HOURS_RAW   - 1].strip() if len(row) >= COL_HOURS_RAW   else ""

        key     = _normalize_key(county, name_raw)
        scraped = current_lookup.get(key)

        # For REMOVED rows: clear if the location reappears in current data
        if status.startswith("REMOVED") and scraped:
            clears.append(gspread.Cell(i + 1, COL_STATUS, ""))
            print(f"  [CLEAR status] row {i+1}: {county} — {name_raw}  (was: {status}, location reappeared)")
            continue

        if scraped and (
            scraped.get("polling_place_county",    "").strip().upper() == county.upper()
            and scraped.get("polling_place_name_raw",    "").strip().upper() == name_raw.upper()
            and scraped.get("polling_place_address_raw", "").strip() == addr_raw
            and scraped.get("hours_raw",                 "").strip() == hrs_raw
        ):
            clears.append(gspread.Cell(i + 1, COL_STATUS, ""))
            print(f"  [CLEAR status] row {i+1}: {county} — {name_raw}  (was: {status})")

    if clears:
        ws.update_cells(clears, value_input_option="RAW")
        print(f"  Cleared {len(clears)} resolved status tag(s)")
    else:
        print("  No resolved tags to clear.")

    return len(clears)


def full_sync(current_rows: list[dict]) -> dict[str, int]:
    """
    One-shot: compare ALL scraped rows against the sheet and push any missing
    or changed rows.  Preserves image_url / Latitude / Longitude if already set.
    """
    print("  Connecting to Google Sheets (full sync)...")
    client = _get_client()
    ws     = _get_worksheet(client)
    all_sheet_rows, key_index = _read_sheet(ws)

    all_cell_updates: list[gspread.Cell] = []
    new_batch: list[list] = []
    updated = 0

    field_col_map = {
        "polling_place_address_raw":  COL_ADDRESS_RAW,
        "polling_place_address_full": COL_ADDRESS_FULL,
        "polling_place_address_line_1": COL_ADDRESS_LINE1,
        "polling_place_address_city": COL_ADDRESS_CITY,
        "polling_place_address_state":COL_ADDRESS_STATE,
        "polling_place_address_zip":  COL_ADDRESS_ZIP,
        "hours_raw":                  COL_HOURS_RAW,
        "hours_advanced_polling":     COL_HOURS_ADV,
    }

    for rec in current_rows:
        key     = _normalize_key(
            rec.get("polling_place_county", ""),
            rec.get("polling_place_name_raw", "")
        )
        row_idx = key_index.get(key)

        if row_idx is None:
            new_batch.append(_build_new_row(rec))
            print(f"  [NEW] {rec.get('polling_place_county','')} — "
                  f"{rec.get('polling_place_name_raw','')}")
        else:
            sheet_row     = all_sheet_rows[row_idx]
            sheet_row_num = row_idx + 1
            row_changed   = False
            for field, col in field_col_map.items():
                sheet_val = sheet_row[col - 1].strip() if len(sheet_row) >= col else ""
                if rec.get(field, "").strip() != sheet_val:
                    all_cell_updates.append(gspread.Cell(sheet_row_num, col, rec.get(field, "")))
                    row_changed = True
            if row_changed:
                updated += 1
                print(f"  [UPDATE] row {sheet_row_num}: "
                      f"{rec.get('polling_place_county','')} — "
                      f"{rec.get('polling_place_name_raw','')}")

    if all_cell_updates:
        print(f"\n  Writing {len(all_cell_updates)} cell updates...")
        for i in range(0, len(all_cell_updates), 1000):
            ws.update_cells(all_cell_updates[i:i+1000], value_input_option="RAW")
            if i + 1000 < len(all_cell_updates):
                time.sleep(1.2)

    if new_batch:
        print(f"\n  Appending {len(new_batch)} new rows...")
        time.sleep(1.2)
        ws.append_rows(new_batch, value_input_option="RAW",
                       insert_data_option="INSERT_ROWS", table_range="A1")

    appended = len(new_batch)
    print(f"\n  Full sync done — appended: {appended}, updated: {updated}")
    return {"appended": appended, "updated": updated}


def pull_from_sheet(csv_path: str = "ga_may_2026_primary_polling_locations.csv") -> int:
    """
    Export all 16 columns of the Google Sheet to the local baseline CSV.
    Skips the header row and blank rows.
    Returns the number of rows written.
    """
    import csv as _csv

    FIELDS = [
        "address_id", "polling_place_county", "polling_place_name_raw",
        "polling_place_name", "polling_place_address_raw",
        "polling_place_address_full", "polling_place_address_line_1",
        "polling_place_address_city", "polling_place_address_state",
        "polling_place_address_zip", "hours_raw", "image_url",
        "hours_advanced_polling", "Latitude", "Longitude", "status", "date_added",
    ]

    print("  Connecting to Google Sheets (pull to CSV)...")
    client = _get_client()
    ws     = _get_worksheet(client)
    rows   = ws.get_all_values()

    out_rows = []
    for i, row in enumerate(rows):
        if i == 0:
            continue  # skip header
        # Pad row to full width (17 columns now)
        row = row + [""] * (17 - len(row))
        county   = row[COL_COUNTY   - 1].strip()
        name_raw = row[COL_NAME_RAW - 1].strip()
        if not county and not name_raw:
            continue
        out_rows.append(dict(zip(FIELDS, [v.strip() for v in row[:17]])))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"  Pulled {len(out_rows)} rows from sheet → {csv_path}")
    return len(out_rows)


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import csv as _csv
    from pathlib import Path

    # Usage:
    #   python update_sheet.py          → full sync (push local CSV → sheet)
    #   python update_sheet.py pull     → pull sheet → local CSV
    mode = sys.argv[1] if len(sys.argv) > 1 else "push"

    if mode == "pull":
        count = pull_from_sheet()
        print(f"\nDone — {count} rows saved to ga_may_2026_primary_polling_locations.csv")
    else:
        csv_path = Path("ga_may_2026_primary_polling_locations.csv")
        if not csv_path.exists():
            print(f"[!] {csv_path} not found. Run scrape_polling_places.py first.")
            raise SystemExit(1)

        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))

        print(f"Loaded {len(rows)} rows from {csv_path}")
        result = full_sync(rows)
        print(f"\nResult: {result}")
