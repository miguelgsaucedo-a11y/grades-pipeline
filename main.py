import os
import json
from scraper import run_scrape

def parse_env_list(name, default=""):
    raw = os.getenv(name, default)
    if raw is None:
        raw = ""
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [s.strip() for s in str(raw).split(",") if s.strip()]

def append_to_sheet(rows):
    """
    Best-effort Google Sheets append.
    Expects GOOGLE_CREDS_JSON and SPREADSHEET_ID to be set.
    Appends to the first worksheet.
    """
    import gspread

    creds_json = os.getenv("GOOGLE_CREDS_JSON", "")
    spreadsheet_id = os.getenv("SPREADSHEET_ID", "")
    if not rows or not creds_json or not spreadsheet_id:
        return False

    try:
        creds = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds)
        sh = gc.open_by_key(spreadsheet_id)
        ws = sh.get_worksheet(0) or sh.sheet1
        ws.append_rows(rows, value_input_option="RAW")
        return True
    except Exception as e:
        print(f"DEBUG – sheets append failed: {e}")
        return False

def main():
    username = os.getenv("PORTAL_USER", "")
    password = os.getenv("PORTAL_PASS", "")
    students = parse_env_list("STUDENTS")

    print(f"DEBUG – landed: https://parentportal.cajonvalley.net/")
    rows, metrics = run_scrape(username, password, students)

    imported = 0
    if rows:
        ok = append_to_sheet(rows)
        imported = len(rows) if ok else 0

    print(
        f"Imported {imported} rows. Missing/Low: {metrics.get('flags',0)}. Wins: {metrics.get('wins',0)}"
    )

if __name__ == "__main__":
    main()
