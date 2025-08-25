# main.py
import os, json, time, hashlib
from datetime import datetime
from typing import List, Dict, Tuple

import gspread
from google.oauth2.service_account import Credentials

from scraper import run_scrape

HEADERS = [
    "ImportedAt","Student","Period","Course","Teacher",
    "DueDate","AssignedDate","Assignment","PtsPossible","Score","Pct",
    "Status","Comments","SourceURL"
]

def _creds_from_env():
    info = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
    if not info:
        raise RuntimeError("GOOGLE_CREDS_JSON is empty")
    data = json.loads(info)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(data, scopes=scopes)

def _open_sheets(spreadsheet_id: str):
    gc = gspread.authorize(_creds_from_env())
    sh = gc.open_by_key(spreadsheet_id)

    # Raw data sheet (first sheet)
    ws = sh.sheet1

    # Create/ensure Grades sheet for snapshotting overall grades later
    try:
        grades_ws = sh.worksheet("Grades")
    except gspread.WorksheetNotFound:
        grades_ws = sh.add_worksheet(title="Grades", rows=1000, cols=10)
        grades_ws.update("A1", [["ImportedAt","Student","Course","GradeText","SourceURL"]])
    return ws, grades_ws

def _sheet_headers(ws):
    try:
        got = ws.row_values(1)
        return [h.strip() for h in got]
    except Exception:
        return []

def _ensure_headers(ws):
    current = _sheet_headers(ws)
    if current != HEADERS:
        # gspread changed arg order; pass values first, then range_name via kw.
        ws.update(values=[HEADERS], range_name="A1")

def _existing_keys(ws) -> set:
    # Build a set of de-dup keys from existing rows
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return set()
    idx = {h:i for i,h in enumerate(values[0])}
    keyset = set()
    for r in values[1:]:
        def g(col): return r[idx[col]] if col in idx and idx[col] < len(r) else ""
        key = (g("Student"), g("Course"), g("Assignment"), g("DueDate"), g("AssignedDate"))
        keyset.add(key)
    return keyset

def _row_key(row: Dict) -> Tuple[str,str,str,str,str]:
    return (
        row.get("Student",""),
        row.get("Course",""),
        row.get("Assignment",""),
        row.get("DueDate",""),
        row.get("AssignedDate",""),
    )

def _rows_to_values(rows: List[Dict]) -> List[List[str]]:
    out = []
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        out.append([
            now,
            r.get("Student",""),
            r.get("Period",""),
            r.get("Course",""),
            r.get("Teacher",""),
            r.get("DueDate",""),
            r.get("AssignedDate",""),
            r.get("Assignment",""),
            r.get("PtsPossible",""),
            r.get("Score",""),
            r.get("Pct",""),
            r.get("Status",""),
            r.get("Comments",""),
            r.get("SourceURL",""),
        ])
    return out

def main():
    username = os.environ.get("PORTAL_USER","").strip()
    password = os.environ.get("PORTAL_PASS","").strip()
    spreadsheet_id = os.environ.get("SPREADSHEET_ID","").strip()
    students_env = os.environ.get("STUDENTS","").strip()
    students = [s.strip() for s in students_env.split(",") if s.strip()]

    print(f"DEBUG — students: {students}")

    if not (username and password and spreadsheet_id and students):
        raise RuntimeError("Missing one of PORTAL_USER, PORTAL_PASS, SPREADSHEET_ID, STUDENTS")

    # Scrape
    rows, metrics = run_scrape(username, password, students)

    print(f"DEBUG — UI SNAPSHOT — url: {metrics.get('ui_url')}")
    print(f"DEBUG — UI SNAPSHOT — sample: {metrics.get('ui_sample') or '(none)'}")
    for s, n in metrics.get("per_student_table_counts", {}).items():
        print(f"DEBUG — class tables for {s}: {n}")

    # Sheets
    ws, grades_ws = _open_sheets(spreadsheet_id)
    _ensure_headers(ws)

    # De-dup before append
    existing = _existing_keys(ws)
    new_rows = [r for r in rows if _row_key(r) not in existing]

    print(f"DEBUG — scraped {len(rows)} rows from portal")
    print(f"Imported {len(new_rows)} new rows. Skipped as duplicates (before append): {len(rows) - len(new_rows)}.")

    if new_rows:
        ws.append_rows(_rows_to_values(new_rows), value_input_option="USER_ENTERED")

    # (Optional) snapshot overall grade later if/when we add a reliable source.
    # For now we insert nothing unless you wire up a grade string.
    print("Inserted 0 grade snapshots to 'Grades'.")

if __name__ == "__main__":
    main()
