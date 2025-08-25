import os
import json
import datetime as dt
from typing import List, Tuple
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from scraper import run_scrape

HEADERS = ["ImportedAt","Student","Period","Course","Teacher","DueDate","AssignedDate",
           "Assignment","PtsPossible","Score","Pct","Status","Comments","SourceURL"]

def _now_ts() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _mk_key(row: List[str]) -> str:
    # Stable de-dup key by student/course/assignment/duedate
    # row layout follows OUR_COLS in scraper.py
    (student, period, course, teacher, due, assigned, asg, pts, score, pct, status, comments, url) = row
    return "||".join([
        student.strip().lower(),
        course.strip().lower(),
        asg.strip().lower(),
        due.strip().lower()
    ])

def _open_sheet() -> gspread.Worksheet:
    creds_json = os.environ.get("GOOGLE_CREDS_JSON", "")
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    if creds_json.strip().startswith("{"):
        creds_dict = json.loads(creds_json)
    else:
        # path in env var
        with open(creds_json, "r") as f:
            creds_dict = json.load(f)

    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet("Assignments")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Assignments", rows="2", cols=str(len(HEADERS)))
    # ensure headers
    ws.update([HEADERS], "A1")
    return ws

def append_rows_dedup(ws, scraped: List[List[str]]):
    # fetch existing for de-dup
    existing = ws.get_all_values()
    existing_keys = set()
    if len(existing) > 1:
        header = existing[0]
        rows = existing[1:]
        # current sheet columns match HEADERS, data rows we add prepend ImportedAt
        # so rows[i][1:] aligns to OUR_COLS
        for r in rows:
            if len(r) >= len(HEADERS):
                # strip ImportedAt when computing key
                key = _mk_key(r[1:1+13])
                existing_keys.add(key)

    new_rows = []
    for r in scraped:
        if _mk_key(r) not in existing_keys:
            new_rows.append([_now_ts()] + r)

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")
    print(f"Imported {len(new_rows)} new rows. Skipped as duplicates (before append): {len(scraped)-len(new_rows)}. Removed legacy dups during cleanup: 0.")

def main():
    username = os.environ["PORTAL_USER"]
    password = os.environ["PORTAL_PASS"]
    students_env = os.environ.get("STUDENTS", "")
    students = [s.strip() for s in students_env.split(",") if s.strip()]
    print(f"DEBUG — students: {students}")

    rows, _metrics = run_scrape(username, password, students)
    print(f"DEBUG — scraped {len(rows)} rows from portal")

    ws = _open_sheet()
    # ensure headers again (API changed warnings otherwise)
    ws.update([HEADERS], "A1")
    append_rows_dedup(ws, rows)

if __name__ == "__main__":
    main()
