# main.py
import os, json
from scraper import run_scrape
from sheets import append_rows

WORKSHEET = "DB"

def main():
    username = os.environ["PORTAL_USER"]
    password = os.environ["PORTAL_PASS"]
    spreadsheet_id = os.environ["SPREADSHEET_ID"]  # <- NEW
    students_env = os.environ.get("STUDENTS", "Adrian,Jacob")
    students = tuple(s.strip() for s in students_env.split(",") if s.strip())

    rows = run_scrape(username, password, students)

    creds_json = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    append_rows(spreadsheet_id, WORKSHEET, rows, creds_json)  # <- pass ID

    total = len(rows)
    missing = sum(1 for r in rows if "Missing" in (r.get("Status") or ""))
    wins = sum(1 for r in rows if "Win" in (r.get("Status") or ""))
    print(f"Imported {total} rows. Missing/Low: {missing}. Wins: {wins}")

if __name__ == "__main__":
    main()
