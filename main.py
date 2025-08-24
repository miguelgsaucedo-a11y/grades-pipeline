import os
import json
from datetime import datetime
from scraper import run_scrape

# If your repo already has sheets.py with append_rows(spreadsheet_id, worksheet, rows, creds_json),
# keep using it so we don't change your workflow.
from sheets import append_rows

WORKSHEET = "DB"  # matches the tab you mentioned

def main():
    username = os.environ.get("PORTAL_USER", "").strip()
    password = os.environ.get("PORTAL_PASS", "").strip()
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    creds_json = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
    students_csv = os.environ.get("STUDENTS", "")

    students = [s.strip() for s in students_csv.split(",") if s.strip()]

    print(f"DEBUG – landed: https://parentportal.cajonvalley.net/")
    if not username or not password:
        raise RuntimeError("Missing PORTAL_USER or PORTAL_PASS")

    rows, metrics = run_scrape(username, password, students)

    flags = metrics.get("flags", 0)  # Missing + Low
    wins = metrics.get("wins", 0)

    if rows:
        append_rows(spreadsheet_id, WORKSHEET, rows, creds_json)
        print(f"Imported {len(rows)} rows. Missing/Low: {flags}. Wins: {wins}")
    else:
        print(f"Imported 0 rows. Missing/Low: {flags}. Wins: {wins}")

if __name__ == "__main__":
    main()
