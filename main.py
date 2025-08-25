import os
import json
from datetime import datetime
from typing import List, Dict

import gspread

from scraper import run_scrape

HEADERS = [
    "ImportedAt","Student","Period","Course","Teacher","DueDate","AssignedDate",
    "Assignment","PtsPossible","Score","Pct","Status","Comments","SourceURL"
]

def get_env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v if v is not None else default

def get_students_from_env() -> List[str]:
    raw = get_env("STUDENTS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]

def make_gspread_client():
    """
    Supports either:
      - GOOGLE_CREDS_JSON = path to the service account JSON file, or
      - GOOGLE_CREDS_JSON = the *contents* of that JSON (one-line secret).
    """
    creds_raw = get_env("GOOGLE_CREDS_JSON", "").strip()
    if not creds_raw:
        raise RuntimeError("GOOGLE_CREDS_JSON is not set")

    if os.path.exists(creds_raw):
        # Path to file
        return gspread.service_account(filename=creds_raw)

    # JSON contents
    try:
        info = json.loads(creds_raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("GOOGLE_CREDS_JSON is neither a valid path nor valid JSON") from e

    return gspread.service_account_from_dict(info)

def connect_sheet(spreadsheet_id: str):
    gc = make_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.sheet1

    # Ensure header row (new gspread signature: update(range, values))
    existing = ws.get_all_values()
    if not existing:
        ws.update("1:1", [HEADERS])
    else:
        # keep header refreshed in case of schema changes
        ws.update("1:1", [HEADERS])
    return ws

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

def append_rows(ws, rows: List[Dict[str, str]]):
    if not rows:
        return 0
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        r["ImportedAt"] = now

    values = [[r.get(h, "") for h in HEADERS] for r in rows]
    # Modern append API; avoids range math and deprecation warnings
    ws.append_rows(values, value_input_option="RAW")
    return len(values)

def main():
    username = get_env("PORTAL_USER")
    password = get_env("PORTAL_PASS")
    spreadsheet_id = get_env("SPREADSHEET_ID")
    students = get_students_from_env()
    print(f"DEBUG — students: {students}")

    ws = connect_sheet(spreadsheet_id)

    rows = run_scrape(username, password, students)
    print(f"DEBUG — scraped {len(rows)} rows from portal")

    before = len(rows)
    rows = dedup_rows(rows)
    removed = before - len(rows)

    added = append_rows(ws, rows)
    print(f"Imported {added} new rows. Skipped as duplicates (before append): {removed}. Removed legacy dups during cleanup: 0.")

if __name__ == "__main__":
    main()
