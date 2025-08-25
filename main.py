import json
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials  # modern auth

from scraper import run_scrape

# ====== SHEET COLUMNS ======
HEADERS = [
    "ImportedAt", "Student", "Period", "Course", "Teacher",
    "DueDate", "AssignedDate", "Assignment",
    "PtsPossible", "Score", "Pct",
    "Status", "Comments",
    "SourceURL",
]

# Columns used to build a unique key to prevent duplicates
DEDUP_KEY_COLS = [
    "Student", "Period", "Course", "Assignment", "DueDate", "AssignedDate", "Score", "PtsPossible"
]


def utc_timestamp_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [s.strip() for s in raw.split(",") if s.strip()] if raw else []


def open_sheet(spreadsheet_id: str, creds_json: str):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(creds_json)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.sheet1

    # Ensure headers on row 1 (write exactly the first row, starting at A1)
    current = ws.row_values(1)
    if current != HEADERS:
        ws.update("A1", [HEADERS])
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


def append_rows(ws, rows: list[dict]) -> int:
    if not rows:
        return 0
    matrix = [[r.get(h, "") for h in HEADERS] for r in rows]
    ws.append_rows(matrix, value_input_option="RAW")
    return len(matrix)


def cleanup_legacy_dups(ws) -> int:
    """Remove older duplicate rows based on DEDUP_KEY_COLS; keep first occurrence."""
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
        else:
            seen.add(k)
            keep.append(r)
    if removed:
        ws.clear()
        ws.update("A1", keep)
    return removed


def main():
    username = os.environ.get("PORTAL_USER", "")
    password = os.environ.get("PORTAL_PASS", "")
    creds_json = os.environ.get("GOOGLE_CREDS_JSON", "")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "")
    students = get_env_list("STUDENTS")

    print(f"DEBUG — students: {students}")

    # Scrape -> list[dict] without ImportedAt
    scraped_rows = run_scrape(username, password, students)

    # Stamp import time
    ts = utc_timestamp_str()
    for r in scraped_rows:
        r["ImportedAt"] = ts

    print(f"DEBUG — scraped {len(scraped_rows)} rows from portal")

    if not spreadsheet_id or not creds_json:
        print("ERROR: Missing SPREADSHEET_ID or GOOGLE_CREDS_JSON")
        return

    ws = open_sheet(spreadsheet_id, creds_json)

    # Skip anything already in the sheet
    existing = load_existing_keys(ws)
    pending = [r for r in scraped_rows if as_key(r) not in existing]

    appended = append_rows(ws, pending)
    removed = cleanup_legacy_dups(ws)

    print(
        f"Imported {appended} new rows. "
        f"Skipped as duplicates (before append): {len(scraped_rows) - appended}. "
        f"Removed legacy dups during cleanup: {removed}."
    )


if __name__ == "__main__":
    main()
