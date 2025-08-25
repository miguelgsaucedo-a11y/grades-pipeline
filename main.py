import os
import json
import datetime as dt
from typing import List
import gspread

from scraper import run_scrape

HEADERS = [
    "ImportedAt","Student","Period","Course","Teacher","DueDate","AssignedDate",
    "Assignment","PtsPossible","Score","Pct","Status","Comments","SourceURL"
]

def _now_ts() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _mk_key(row: List[str]) -> str:
    # Stable de-dup key by student/course/assignment/duedate
    (student, period, course, teacher, due, assigned, asg, pts, score, pct, status, comments, url) = row
    return "||".join([
        student.strip().lower(),
        course.strip().lower(),
        asg.strip().lower(),
        due.strip().lower(),
    ])

def _open_sheet():
    """Authorize with Google using gspread's google-auth and return the worksheet."""
    creds_json = os.environ.get("GOOGLE_CREDS_JSON", "")
    spreadsheet_id = os.environ["SPREADSHEET_ID"]

    # Accept either a JSON string or a path to the JSON file
    if creds_json.strip().startswith("{"):
        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
    else:
        gc = gspread.service_account(filename=creds_json)

    sh = gc.open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet("Assignments")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Assignments", rows="2", cols=str(len(HEADERS)))

    # Ensure headers (use named args to avoid deprecation warnings)
    ws.update(range_name="A1", values=[HEADERS])
    return ws

def append_rows_dedup(ws, scraped: List[List[str]]):
    # Build a set of existing keys to avoid duplicates
    existing = ws.get_all_values()
    existing_keys = set()
    if len(existing) > 1:
        rows = existing[1:]
        for r in rows:
            if len(r) >= len(HEADERS):
                existing_keys.add(_mk_key(r[1:1+13]))  # ignore ImportedAt

    new_rows = []
    for r in scraped:
        if _mk_key(r) not in existing_keys:
            new_rows.append([_now_ts()] + r)

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")
    print(
        f"Imported {len(new_rows)} new rows. "
        f"Skipped as duplicates (before append): {len(scraped) - len(new_rows)}. "
        f"Removed legacy dups during cleanup: 0."
    )

def main():
    username = os.environ["PORTAL_USER"]
    password = os.environ["PORTAL_PASS"]
    students_env = os.environ.get("STUDENTS", "")
    students = [s.strip() for s in students_env.split(",") if s.strip()]
    print(f"DEBUG — students: {students}")

    rows, _metrics = run_scrape(username, password, students)
    print(f"DEBUG — scraped {len(rows)} rows from portal")

    ws = _open_sheet()
    ws.update(range_name="A1", values=[HEADERS])  # re-ensure
    append_rows_dedup(ws, rows)

if __name__ == "__main__":
    main()
