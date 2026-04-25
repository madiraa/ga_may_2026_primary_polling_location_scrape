"""
Google Sheets Sync — GA May 2026 Primary Polling Locations
===========================================================
Column layout (A–O, 15 columns):
  A  address_id
  B  polling_place_county
  C  polling_place_name
  D  polling_place_address_full
  E  polling_place_address_line_1
  F  polling_place_address_city
  G  polling_place_address_state
  H  polling_place_address_zip
  I  image_url            ← never overwritten if already set
  J  hours_advanced_polling
  K  Latitude             ← blank for new rows (requires geocoding)
  L  Longitude            ← blank for new rows
  M  status               ← auto-stamped on change, auto-cleared when stable
  N  date_added           ← stamped once on first append, never cleared
  O  date_removed         ← stamped once on first removal, never cleared

Matching key : COUNTY||NAME  (internal whitespace normalised)
Status (M) lifecycle:
  ADDED MM/DD/YYYY       → auto-cleared next stable run
  MODIFIED: fields date  → auto-cleared next stable run
  REMOVED MM/DD/YYYY     → cleared only if location reappears
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "192ufA2ffXqTTsQQxJylg1mMC5TBbHndfgIu5UI4WDGQ"
WORKSHEET_GID  = 571990717

# Column numbers (1-indexed)
COL_ADDRESS_ID   =  1   # A
COL_COUNTY       =  2   # B
COL_NAME         =  3   # C
COL_ADDR_FULL    =  4   # D
COL_ADDR_LINE1   =  5   # E
COL_ADDR_CITY    =  6   # F
COL_ADDR_STATE   =  7   # G
COL_ADDR_ZIP     =  8   # H
COL_IMAGE_URL    =  9   # I  ← never overwritten if populated
COL_HOURS_ADV    = 10   # J
COL_LATITUDE     = 11   # K
COL_LONGITUDE    = 12   # L
COL_STATUS       = 13   # M
COL_DATE_ADDED   = 14   # N
COL_DATE_REMOVED = 15   # O

FIELDS = [
    "address_id", "polling_place_county", "polling_place_name",
    "polling_place_address_full", "polling_place_address_line_1",
    "polling_place_address_city", "polling_place_address_state",
    "polling_place_address_zip", "image_url", "hours_advanced_polling",
    "Latitude", "Longitude", "status", "date_added", "date_removed",
]

# Fields compared for change detection
COMPARE_FIELDS = ["polling_place_county", "polling_place_name",
                  "polling_place_address_full", "hours_advanced_polling"]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%m/%d/%Y")


def _norm(s: str) -> str:
    """Normalise whitespace: strip + collapse internal spaces."""
    return re.sub(r'\s+', ' ', s.strip())


def _normalize_key(county: str, name: str) -> str:
    return f"{_norm(county).upper()}||{_norm(name).upper()}"


def _get_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS environment variable not set")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds)


def _get_worksheet(client: gspread.Client) -> gspread.Worksheet:
    sh = client.open_by_key(SPREADSHEET_ID)
    return sh.get_worksheet_by_id(WORKSHEET_GID)


def _read_sheet(ws: gspread.Worksheet) -> tuple[list[list], dict[str, int]]:
    """Returns (all_rows, {COUNTY||NAME: 0-based row index})."""
    all_rows = ws.get_all_values()
    index: dict[str, int] = {}
    for i, row in enumerate(all_rows):
        if i == 0:
            continue
        county = row[COL_COUNTY - 1] if len(row) >= COL_COUNTY else ""
        name   = row[COL_NAME   - 1] if len(row) >= COL_NAME   else ""
        if county or name:
            index[_normalize_key(county, name)] = i
    return all_rows, index


def _build_new_row(rec: dict) -> list:
    """Build a 15-column sheet row for a newly added location."""
    return [
        rec.get("address_id",                  ""),  # A
        rec.get("polling_place_county",        ""),  # B
        rec.get("polling_place_name",          ""),  # C
        rec.get("polling_place_address_full",  ""),  # D
        rec.get("polling_place_address_line_1",""),  # E
        rec.get("polling_place_address_city",  ""),  # F
        rec.get("polling_place_address_state", ""),  # G
        rec.get("polling_place_address_zip",   ""),  # H
        rec.get("image_url",                   ""),  # I
        rec.get("hours_advanced_polling",      ""),  # J
        "",                                          # K Latitude (blank)
        "",                                          # L Longitude (blank)
        f"ADDED {_today()}",                         # M status
        _today(),                                    # N date_added (permanent)
        "",                                          # O date_removed
    ]


def sync_changes(diff: dict) -> dict[str, int]:
    """Apply a diff to the Google Sheet. Returns {appended, updated, removed}."""
    if not (diff.get("added") or diff.get("modified") or diff.get("removed")):
        print("  No changes to sync to sheet.")
        return {"appended": 0, "updated": 0, "removed_tagged": 0}

    print("  Connecting to Google Sheets...")
    client = _get_client()
    ws     = _get_worksheet(client)
    all_rows, key_index = _read_sheet(ws)

    cell_updates: list[gspread.Cell] = []
    new_rows: list[list] = []
    appended = updated = removed_tagged = 0

    # ── Modified rows ────────────────────────────────────────────────────────
    field_col = {
        "polling_place_county":       COL_COUNTY,
        "polling_place_name":         COL_NAME,
        "polling_place_address_full": COL_ADDR_FULL,
        "hours_advanced_polling":     COL_HOURS_ADV,
    }

    for entry in diff.get("modified", []):
        rec     = entry["record"]
        changes = entry["changes"]
        key     = _normalize_key(rec.get("polling_place_county", ""),
                                 rec.get("polling_place_name", ""))
        row_idx = key_index.get(key)
        if row_idx is None:
            new_rows.append(_build_new_row(rec))
            continue

        sheet_row_num  = row_idx + 1
        changed_fields = list(changes.keys())
        for field, col in field_col.items():
            if field in changes:
                cell_updates.append(gspread.Cell(sheet_row_num, col, rec.get(field, "")))
        if changed_fields:
            status = f"MODIFIED: {', '.join(changed_fields)} {_today()}"
            cell_updates.append(gspread.Cell(sheet_row_num, COL_STATUS, status))
            print(f"  [UPDATE {'+'.join(changed_fields)}] row {sheet_row_num}: "
                  f"{rec.get('polling_place_county','')} — {rec.get('polling_place_name','')}")
            updated += 1

    if cell_updates:
        ws.update_cells(cell_updates, value_input_option="RAW")
        time.sleep(1.2)

    # ── New locations ─────────────────────────────────────────────────────────
    for rec in diff.get("added", []):
        print(f"  [APPEND] {rec.get('polling_place_county','')} — "
              f"{rec.get('polling_place_name','')}  |  "
              f"{rec.get('polling_place_address_full','')}")
        new_rows.append(_build_new_row(rec))

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW",
                       insert_data_option="INSERT_ROWS", table_range="A1")
        appended += len(new_rows)

    # ── Removed: stamp status + date_removed ─────────────────────────────────
    remove_updates: list[gspread.Cell] = []
    for rec in diff.get("removed", []):
        county   = rec.get("polling_place_county", "")
        name     = rec.get("polling_place_name", "")
        key      = _normalize_key(county, name)
        row_idx  = key_index.get(key)
        if row_idx is None:
            print(f"  [REMOVED — not found in sheet] {county} — {name}")
            continue
        sheet_row_num = row_idx + 1
        remove_updates.append(gspread.Cell(sheet_row_num, COL_STATUS, f"REMOVED {_today()}"))
        # Only stamp date_removed on first removal
        sheet_row = all_rows[row_idx]
        existing  = sheet_row[COL_DATE_REMOVED - 1] if len(sheet_row) >= COL_DATE_REMOVED else ""
        if not existing.strip():
            remove_updates.append(gspread.Cell(sheet_row_num, COL_DATE_REMOVED, _today()))
        print(f"  [REMOVED tag] row {sheet_row_num}: {county} — {name}")
        removed_tagged += 1

    if remove_updates:
        if cell_updates or new_rows:
            time.sleep(1.2)
        ws.update_cells(remove_updates, value_input_option="RAW")

    print(f"\n  Sheet sync — appended: {appended}, updated: {updated}, "
          f"removed tagged: {removed_tagged}")
    return {"appended": appended, "updated": updated, "removed_tagged": removed_tagged}


def clear_resolved_statuses(current: list[dict]) -> int:
    """Clear status tags when the underlying data has stabilised."""
    print("  Checking for resolved status tags to clear...")
    client   = _get_client()
    ws       = _get_worksheet(client)
    all_rows = ws.get_all_values()

    current_lookup = {
        _normalize_key(r.get("polling_place_county", ""),
                       r.get("polling_place_name", "")): r
        for r in current
    }

    clears: list[gspread.Cell] = []
    for i, row in enumerate(all_rows):
        if i == 0:
            continue
        status = row[COL_STATUS - 1].strip() if len(row) >= COL_STATUS else ""
        if not status:
            continue

        county = row[COL_COUNTY - 1].strip() if len(row) >= COL_COUNTY else ""
        name   = row[COL_NAME   - 1].strip() if len(row) >= COL_NAME   else ""
        key    = _normalize_key(county, name)
        scraped = current_lookup.get(key)

        if status.startswith("REMOVED"):
            if scraped:   # location reappeared → clear REMOVED tag
                clears.append(gspread.Cell(i + 1, COL_STATUS, ""))
                print(f"  [CLEAR REMOVED] row {i+1}: {county} — {name}")
            continue

        # ADDED / MODIFIED: clear if all compared fields now match
        if scraped and all(
            _norm(scraped.get(f, "")) == _norm(row[col - 1] if len(row) >= col else "")
            for f, col in [
                ("polling_place_county",       COL_COUNTY),
                ("polling_place_name",         COL_NAME),
                ("polling_place_address_full", COL_ADDR_FULL),
                ("hours_advanced_polling",     COL_HOURS_ADV),
            ]
        ):
            clears.append(gspread.Cell(i + 1, COL_STATUS, ""))
            print(f"  [CLEAR status] row {i+1}: {county} — {name}  (was: {status})")

    if clears:
        ws.update_cells(clears, value_input_option="RAW")
        print(f"  Cleared {len(clears)} resolved status tag(s)")
    else:
        print("  No resolved tags to clear.")
    return len(clears)


def pull_from_sheet(csv_path: str = "ga_may_2026_primary_polling_locations.csv") -> int:
    """Export all 15 sheet columns to the local baseline CSV."""
    import csv as _csv
    print("  Connecting to Google Sheets (pull to CSV)...")
    client = _get_client()
    ws     = _get_worksheet(client)
    rows   = ws.get_all_values()

    out_rows = []
    for i, row in enumerate(rows):
        if i == 0:
            continue
        row = row + [""] * (15 - len(row))
        county = row[COL_COUNTY - 1].strip()
        name   = row[COL_NAME   - 1].strip()
        if not county and not name:
            continue
        out_rows.append(dict(zip(FIELDS, [v.strip() for v in row[:15]])))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"  Pulled {len(out_rows)} rows from sheet → {csv_path}")
    return len(out_rows)


if __name__ == "__main__":
    import sys
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        print("Set GOOGLE_CREDENTIALS env var first.")
        sys.exit(1)
    if len(sys.argv) > 1 and sys.argv[1] == "pull":
        pull_from_sheet()
    else:
        print("Run check_for_changes.py to trigger a sync.")
