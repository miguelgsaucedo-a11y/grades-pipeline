import os
from datetime import datetime
from scraper import run_scrape, append_to_sheet


def main():
    username = os.environ.get("PORTAL_USER", "")
    password = os.environ.get("PORTAL_PASS", "")
    students_csv = os.environ.get("STUDENTS", "")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "")
    google_creds_json = os.environ.get("GOOGLE_CREDS_JSON", "")

    if not username or not password:
        raise SystemExit("Missing PORTAL_USER or PORTAL_PASS env vars.")
    if not spreadsheet_id or not google_creds_json:
        raise SystemExit("Missing SPREADSHEET_ID or GOOGLE_CREDS_JSON env vars.")
    if not students_csv.strip():
        raise SystemExit("Missing STUDENTS env var (comma-separated).")

    students = [s.strip() for s in students_csv.split(",") if s.strip()]
    print(f"DEBUG — students parsed: {students}")

    rows, metrics = run_scrape(username, password, students)

    print(f"DEBUG — collected rows: {len(rows)}")
    if not rows:
        print("Imported 0 rows. Missing/Low: 0. Wins: 0")
        return

    # Append to Google Sheet (creates header row if needed)
    appended = append_to_sheet(
        spreadsheet_id=spreadsheet_id,
        google_creds_json=google_creds_json,
        rows=rows,
        worksheet_title="Assignments"  # change if you prefer another tab name
    )
    print(f"Imported {appended} rows. Missing/Low: {metrics.get('missing_low', 0)}. Wins: {metrics.get('wins', 0)}")


if __name__ == "__main__":
    main()
