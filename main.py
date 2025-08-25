import json
import os
import re
from datetime import datetime, timezone

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from scraper import run_scrape

# ====== CONFIG ======
HEADERS = [
    "ImportedAt", "Student", "Period", "Course", "Teacher",
    "DueDate", "AssignedDate", "Assignment",
    "PtsPossible", "Score", "Pct",
    "Status", "Comments",
    "SourceURL",
]

# Unique key used for de-dup. Tweak if you want stricter/looser keys.
DEDUP_KEY_COLS = [
    "Student", "Period", "Course", "Assignment", "DueDate", "AssignedDate", "Score", "PtsPossible"
]

def utc_timestamp_str() -> str:
    # Keep UTC so runs from CI are consistent; easy to convert in Sheets
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def get_env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]

def open_sheet(spreadsheet_id: str, creds_json: str):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(creds_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.sheet1
    # Ensure headers
    current = ws.row_values(1)
    if current != HEADERS:
        # gspread changed the arg order (values first). Use correct order.
        ws.update([HEADERS])
    return ws

def as_key(row: dict) -> str:
    return "|".join(str(row.get(k, "")).strip() for k in DEDUP_KEY_COLS)

def load_existing_keys(ws) -> set[str]:
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return set()
    header = values[0]
    idx = {name: i for i, name in enumerate(header)}
    keys = set()
    for r in values[1:]:
        row_map = {h: (r[idx[h]] if idx[h] < len(r) else "") for h in DEDUP_KEY_COLS}
        keys.add(as_key(row_map))
    return keys

def append_rows(ws, rows: list[dict]):
    if not rows:
        return 0
    matrix = []
    for r in rows:
        matrix.append([r.get(h, "") for h in HEADERS])
    # append_rows takes a list of lists
    ws.append_rows(matrix, value_input_option="RAW")
    return len(matrix)

def cleanup_legacy_dups(ws):
    """
    Optional: dedupe existing sheet rows based on DEDUP_KEY_COLS.
    Leaves the first occurrence, drops later duplicates.
    """
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return 0
    header = values[0]
    idx = {name: i for i, name in enumerate(header)}
    seen = set()
    keep = [header]
    removed = 0
    for r in values[1:]:
        row_map = {h: (r[idx[h]] if idx[h] < len(r) else "") for h in DEDUP_KEY_COLS}
        k = as_key(row_map)
        if k in seen:
            removed += 1
            continue
        seen.add(k)
        keep.append(r)
    if removed:
        ws.clear()
        ws.update(keep)
    return removed

def main():
    username = os.environ.get("PORTAL_USER", "")
    password = os.environ.get("PORTAL_PASS", "")
    creds_json = os.environ.get("GOOGLE_CREDS_JSON", "")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "")
    students = get_env_list("STUDENTS")

    print(f"DEBUG — students: {students}")

    rows_from_portal = run_scrape(username, password, students)  # list of dict rows, no ImportedAt

    # Add ImportedAt
    ts = utc_timestamp_str()
    for r in rows_from_portal:
        r["ImportedAt"] = ts

    print(f"DEBUG — scraped {len(rows_from_portal)} rows from portal")

    if not spreadsheet_id or not creds_json:
        print("ERROR: Missing SPREADSHEET_ID or GOOGLE_CREDS_JSON")
        return

    ws = open_sheet(spreadsheet_id, creds_json)

    # De-dup against existing before appending
    existing = load_existing_keys(ws)
    new_rows = [r for r in rows_from_portal if as_key(r) not in existing]

    appended = append_rows(ws, new_rows)
    removed = cleanup_legacy_dups(ws)

    print(
        f"Imported {appended} new rows. Skipped as duplicates (before append): "
        f"{len(rows_from_portal) - appended}. Removed legacy dups during cleanup: {removed}."
    )

if __name__ == "__main__":
    main()
