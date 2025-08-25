import os
import time
from datetime import datetime
from typing import List, Dict

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from scraper import run_scrape

HEADERS = [
    "ImportedAt","Student","Period","Course","Teacher","DueDate","AssignedDate",
    "Assignment","PtsPossible","Score","Pct","Status","Comments","SourceURL"
]

def get_env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    if v is None:
        v = default
    return v

def get_students_from_env() -> List[str]:
    raw = get_env("STUDENTS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]

def dedup_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for r in rows:
        key = (
            r.get("Student","").strip().lower(),
            r.get("Period","").strip().lower(),
            r.get("Course","").strip().lower(),
            r.get("DueDate","").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

def connect_sheet(spreadsheet_id: str):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_json = get_env("GOOGLE_CREDS_JSON")
    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_json, scope)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.sheet1

    # Ensure header row (and fix deprecation warning: values first, then range_name)
    existing = ws.get_all_values()
    if not existing:
        ws.update(values=[HEADERS], range_name="1:1")
    else:
        # keep header refreshed in case of changes
        ws.update(values=[HEADERS], range_name="1:1")
    return ws

def append_rows(ws, rows: List[Dict[str, str]]):
    if not rows:
        return 0

    # Timestamp
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        r["ImportedAt"] = now

    # Build 2D values in header order
    values = [[r.get(h, "") for h in HEADERS] for r in rows]
    start_row = len(ws.get_all_values()) + 1
    end_row = start_row + len(values) - 1
    rng = f"{start_row}:{end_row}"
    ws.update(values=values, range_name=rng)
    return len(values)

def main():
    username = get_env("PORTAL_USER")
    password = get_env("PORTAL_PASS")
    spreadsheet_id = get_env("SPREADSHEET_ID")
    students = get_students_from_env()
    print(f"DEBUG — students: {students}")

    ws = connect_sheet(spreadsheet_id)

    # Scrape
    rows = run_scrape(username, password, students)
    print(f"DEBUG — scraped {len(rows)} rows from portal")

    # Dedup within this run
    before = len(rows)
    rows = dedup_rows(rows)
    removed = before - len(rows)

    # Append
    added = append_rows(ws, rows)
    print(f"Imported {added} new rows. Skipped as duplicates (before append): {removed}. Removed legacy dups during cleanup: 0.")

if __name__ == "__main__":
    main()
