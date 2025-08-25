import os
import time
import json
import datetime as dt
from typing import Dict, List, Tuple

import gspread
from google.oauth2.service_account import Credentials

from scraper import run_scrape

# ======== Config from env ========
PORTAL_USER = os.getenv("PORTAL_USER", "")
PORTAL_PASS = os.getenv("PORTAL_PASS", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "")
STUDENTS_CSV = os.getenv("STUDENTS", "")

if not (PORTAL_USER and PORTAL_PASS and SPREADSHEET_ID and GOOGLE_CREDS_JSON and STUDENTS_CSV):
    print("ERROR: Missing one or more required environment variables.")
    exit(1)

students: List[str] = [s.strip() for s in STUDENTS_CSV.split(",") if s.strip()]
print(f"DEBUG – students: {students!r}")

# ======== Sheets setup ========
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds_info = json.loads(GOOGLE_CREDS_JSON)
credentials = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
gc = gspread.authorize(credentials)
ss = gc.open_by_key(SPREADSHEET_ID)

ASSIGN_SHEET_NAME = "Assignments"
GRADES_SHEET_NAME = "Grades"

ASSIGN_HEADERS = [
    "ImportedAt", "Student", "Period", "Course", "Teacher",
    "DueDate", "AssignedDate", "Assignment",
    "PtsPossible", "Score", "Pct",
    "Status", "Comments", "SourceURL"
]

GRADES_HEADERS = [
    "ImportedAt", "Student", "Course", "Teacher",
    "GradeLetter", "PointsEarned", "PointsPossible", "SourceURL"
]


def get_or_create_ws(title: str, headers: List[str]):
    try:
        ws = ss.worksheet(title)
        # Make sure headers exist (don’t overwrite data)
        try:
            current = ws.row_values(1)
            if current != headers:
                ws.update('A1', [headers])
        except Exception:
            ws.update('A1', [headers])
        return ws
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=len(headers) + 2)
        ws.update('A1', [headers])
        return ws


def normalize(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def dedup_key(row: Dict[str, str]) -> Tuple[str, str, str, str]:
    # A stable key that shouldn’t change when we fix course/period later
    return (
        normalize(row.get("Student", "")),
        normalize(row.get("Assignment", "")),
        normalize(row.get("DueDate", "")),
        normalize(row.get("Teacher", "")),
    )


def sheet_existing_keys(ws) -> set:
    keys = set()
    data = ws.get_all_records()
    for r in data:
        keys.add(dedup_key(r))
    return keys


def append_rows(ws, rows: List[Dict[str, str]], header_order: List[str]) -> int:
    """Append only non-duplicate rows; return # inserted."""
    if not rows:
        return 0
    existing = sheet_existing_keys(ws)
    to_add = []
    for r in rows:
        key = dedup_key(r)
        if key not in existing:
            to_add.append([r.get(h, "") for h in header_order])
            existing.add(key)
    if not to_add:
        return 0
    ws.append_rows(to_add, value_input_option="RAW")
    return len(to_add)


def legacy_cleanup(ws, header_order: List[str]) -> int:
    """Optional cleanup: enforce uniqueness by our key across the whole sheet."""
    data = ws.get_all_records()
    seen = set()
    cleaned = []
    removed = 0
    for r in data:
        k = dedup_key(r)
        if k in seen:
            removed += 1
            continue
        seen.add(k)
        cleaned.append([r.get(h, "") for h in header_order])
    if removed:
        ws.clear()
        ws.update('A1', [header_order])
        if cleaned:
            ws.append_rows(cleaned, value_input_option="RAW")
    return removed


def main():
    assign_ws = get_or_create_ws(ASSIGN_SHEET_NAME, ASSIGN_HEADERS)
    grades_ws = get_or_create_ws(GRADES_SHEET_NAME, GRADES_HEADERS)

    # Scrape portal
    try:
        rows, grade_rows = run_scrape(PORTAL_USER, PORTAL_PASS, students)
    except Exception as e:
        print("ERROR during scrape:", repr(e))
        raise

    print(f"DEBUG – scraped {len(rows)} rows from portal")
    imported_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # stamp ImportedAt
    for r in rows:
        r["ImportedAt"] = imported_at
    for g in grade_rows:
        g["ImportedAt"] = imported_at

    # Append with de-dup
    inserted = append_rows(assign_ws, rows, ASSIGN_HEADERS)
    print(f"Imported {inserted} new rows. Skipped as duplicates (before append): {len(rows) - inserted}.")

    # Optional cleanup pass to remove legacy dupes
    removed = legacy_cleanup(assign_ws, ASSIGN_HEADERS)
    print(f"Removed legacy dups during cleanup: {removed}.")

    # Append grade snapshots
    grades_inserted = append_rows(grades_ws, grade_rows, GRADES_HEADERS)
    print(f"Inserted {grades_inserted} grade snapshots to '{GRADES_SHEET_NAME}'.")


if __name__ == "__main__":
    main()
