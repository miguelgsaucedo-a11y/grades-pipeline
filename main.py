# main.py
import os, json
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
    raw = os.environ.get("GOOGLE_CREDS_JSON","").strip()
    if not raw:
        raise RuntimeError("GOOGLE_CREDS_JSON is empty")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)

def _open_ws(spreadsheet_id: str):
    gc = gspread.authorize(_creds_from_env())
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.sheet1
    # ensure a Grades sheet exists (we’ll populate when we add a solid source)
    try:
        sh.worksheet("Grades")
    except gspread.WorksheetNotFound:
        sh.add_worksheet(title="Grades", rows=1000, cols=10).update(
            "A1", [["ImportedAt","Student","Course","GradeText","SourceURL"]]
        )
    return ws

def _ensure_headers(ws):
    try:
        row1 = [h.strip() for h in ws.row_values(1)]
    except Exception:
        row1 = []
    if row1 != HEADERS:
        # gspread new arg order: values first, then range_name
        ws.update(values=[HEADERS], range_name="A1")

def _existing_keys(ws) -> set:
    vals = ws.get_all_values()
    if not vals or len(vals) < 2:
        return set()
    header = vals[0]
    idx = {h:i for i,h in enumerate(header)}
    kset = set()
    for r in vals[1:]:
        def g(col): return r[idx[col]] if col in idx and idx[col] < len(r) else ""
        kset.add((g("Student"), g("Course"), g("Assignment"), g("DueDate"), g("AssignedDate")))
    return kset

def _row_key(d: Dict) -> Tuple[str,str,str,str,str]:
    return (d.get("Student",""), d.get("Course",""), d.get("Assignment",""),
            d.get("DueDate",""), d.get("AssignedDate",""))

def _rows_to_values(rows: List[Dict]) -> List[List[str]]:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    out = []
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
    students = [s.strip() for s in os.environ.get("STUDENTS","").split(",") if s.strip()]

    print(f"DEBUG — students: {students}")

    rows, metrics = run_scrape(username, password, students)
    print(f"DEBUG — UI SNAPSHOT — url: {metrics.get('ui_url')}")
    print(f"DEBUG — UI SNAPSHOT — sample: {metrics.get('ui_sample') or '(none)'}")
    for s, n in metrics.get("per_student_table_counts", {}).items():
        print(f"DEBUG — class tables for {s}: {n}")

    ws = _open_ws(spreadsheet_id)
    _ensure_headers(ws)

    existing = _existing_keys(ws)
    new_rows = [r for r in rows if _row_key(r) not in existing]

    print(f"DEBUG — scraped {len(rows)} rows from portal")
    print(f"Imported {len(new_rows)} new rows. Skipped as duplicates (before append): {len(rows) - len(new_rows)}.")
    if new_rows:
        ws.append_rows(_rows_to_values(new_rows), value_input_option="USER_ENTERED")

    # (Grades snapshot left as future hook.)
    print("Removed legacy dups during cleanup: 0.")
    print("Inserted 0 grade snapshots to 'Grades'.")

if __name__ == "__main__":
    main()
