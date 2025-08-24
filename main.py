import os
import json
from collections import Counter

from scraper import run_scrape

# If you already have a sheets.py helper, this import will keep working.
# It must expose: append_rows(spreadsheet_id: str, worksheet: str, rows: List[List[str]], creds_json: str)
from sheets import append_rows


def dprint(*args):
    """Uniform debug print so logs are easy to scan in Actions."""
    print("DEBUG —", *args, flush=True)


def env(name: str, default: str | None = None) -> str:
    val = os.getenv(name, default)
    if val is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def main() -> None:
    # --- read secrets / config --- #
    username = env("PORTAL_USER")
    password = env("PORTAL_PASS")
    students_env = os.getenv("STUDENTS", "")  # "Adrian,Jacob" or list via workflow env
    spreadsheet_id = env("SPREADSHEET_ID")
    creds_json = env("GOOGLE_CREDS_JSON")
    worksheet = os.getenv("WORKSHEET", "DB")  # your sheet/tab name

    # Keep the type loose: run_scrape accepts string, list, or tuple.
    students: str | list[str] | tuple[str, ...] = students_env

    # --- run scraper --- #
    rows = run_scrape(username, password, students)

    # --- append to Google Sheet (if any) --- #
    if rows:
        append_rows(spreadsheet_id, worksheet, rows, creds_json)

    # --- summarize for the log --- #
    # Expect the last column to be a category we set in scraper: MISSING | LOW | WIN | OK
    cats = Counter([r[-1] for r in rows]) if rows else Counter()
    missing_low = cats.get("MISSING", 0) + cats.get("LOW", 0)
    wins = cats.get("WIN", 0)
    dprint(f"Imported {len(rows)} rows. Missing/Low: {missing_low}. Wins: {wins}")


if __name__ == "__main__":
    main()
