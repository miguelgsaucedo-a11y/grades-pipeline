# main.py
from __future__ import annotations

import os
import json
from typing import List, Tuple, Dict

import gspread
from gspread.exceptions import WorksheetNotFound

from scraper import run_scrape, HEADERS


def env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    if val is None:
        return ""
    return val.strip()


def get_students_from_env() -> List[str]:
    raw = env("STUDENTS", "")
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def open_sheet(spreadsheet_id: str):
    """Authorize via GOOGLE_CREDS_JSON and open the first worksheet."""
    creds_json = env("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDS_JSON not set")

    # Allow either raw JSON or a path. Prefer raw JSON (as used in Actions secrets).
    try:
        creds_dict = json.loads(creds_json)
    except json.JSONDecodeError:
        # maybe the env contains a path
        with open(creds_json, "r", encoding="utf-8") as f:
            creds_dict = json.load(f)

    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open_by_key(spreadsheet_id)
    try:
        ws = sh.sheet1
    except WorksheetNotFound:
        ws = sh.add_worksheet(title="Sheet1", rows=100, cols=len(HEADERS) + 4)

    # Ensure header row exists and matches our order
    existing = ws.get_all_values()
    if not existing:
        ws.update("A1", [HEADERS])
    else:
        header = existing[0]
        if header != HEADERS:
            ws.clear()
            ws.update("A1", [HEADERS])

    return ws


def existing_key_set(ws) -> set[Tuple[str, str, str, str]]:
    """
    Build a de-dup key set from the current sheet:
    (Student, Course, DueDate, Assignment)
    """
    values = ws.get_all_values()
    if not values or len(values) == 1:
        return set()
    keys = set()
    header = values[0]
    cols = {name: i for i, name in enumerate(header)}

    req = ["Student", "Course", "DueDate", "Assignment"]
    if not all(k in cols for k in req):
        return set()

    for row in values[1:]:
        if not row or len(row) <= max(cols.values()):
            continue
        key = (
            row[cols["Student"]],
            row[cols["Course"]],
            row[cols["DueDate"]],
            row[cols["Assignment"]],
        )
        keys.add(key)
    return keys


def filter_new_rows(rows: List[List[str]], existing_keys: set[Tuple[str, str, str, str]]) -> List[List[str]]:
    """Keep only rows whose (Student, Course, DueDate, Assignment) are not already present."""
    keep: List[List[str]] = []
    for r in rows:
        # r indexes follow HEADERS
        try:
            key = (r[1], r[3], r[5], r[7])
        except Exception:
            # malformed row, keep it (or skip — choose to keep)
            keep.append(r)
            continue
        if key not in existing_keys:
            existing_keys.add(key)  # prevent duplicates within same run
            keep.append(r)
    return keep


def main():
    username = env("PORTAL_USER")
    password = env("PORTAL_PASS")
    spreadsheet_id = env("SPREADSHEET_ID")
    students = get_students_from_env()

    print(f"DEBUG — students: {students}")

    if not username or not password:
        raise RuntimeError("PORTAL_USER / PORTAL_PASS are required")
    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID is required")
    if not students:
        raise RuntimeError("STUDENTS is required (comma-separated)")

    # --- SCRAPE ---
    rows, metrics = run_scrape(username, password, students)

    # --- SHEET APPEND with de-dup ---
    ws = open_sheet(spreadsheet_id)
    existing = existing_key_set(ws)
    before = len(existing)

    new_rows = filter_new_rows(rows, existing)
    if new_rows:
        # Append in one batch
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    else:
        print("DEBUG — nothing new to append")

    imported = len(new_rows)
    print(
        f"Imported {imported} new rows. "
        f"Skipped as duplicates (before append): {before and (len(rows) - imported) or 0}. "
        f"Removed legacy dups during cleanup: 0."
    )


if __name__ == "__main__":
    main()
