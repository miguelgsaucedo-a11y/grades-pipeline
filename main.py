# main.py
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Tuple

import gspread
from google.oauth2.service_account import Credentials

from scraper import run_scrape

HEADERS = [
    "ImportedAt",
    "Student",
    "Period",
    "Course",
    "Teacher",
    "DueDate",
    "AssignedDate",
    "Assignment",
    "PtsPossible",
    "Score",
    "Pct",
    "Status",
    "Comments",
    "SourceURL",
]

DEDUP_COLS = ["Student", "Course", "Assignment", "DueDate", "AssignedDate"]


def dbg(msg: str):
    print(f"DEBUG — {msg}")


def _load_service_account() -> Credentials:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    raw = os.environ.get("GOOGLE_CREDS_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_CREDS_JSON is missing")
    if raw.strip().startswith("{"):
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=scopes)
    return Credentials.from_service_account_file(raw, scopes=scopes)


def _open_worksheet() -> gspread.Worksheet:
    sheet_id = os.environ.get("SPREADSHEET_ID", "")
    if not sheet_id:
        raise RuntimeError("SPREADSHEET_ID is missing")
    gc = gspread.authorize(_load_service_account())
    sh = gc.open_by_key(sheet_id)
    return sh.sheet1


def _ensure_header(ws):
    try:
        existing = ws.row_values(1)
    except Exception:
        existing = []
    if existing != HEADERS:
        ws.update(values=[HEADERS], range_name="1:1")


def _sheet_records(ws) -> Tuple[List[List[str]], Dict[str, int]]:
    values = ws.get_all_values()
    if not values:
        return [], {}
    header = values[0]
    return values, {name: i for i, name in enumerate(header)}


def _key_for_row(row: List[str], index: Dict[str, int]) -> str:
    parts = []
    for col in DEDUP_COLS:
        i = index.get(col, -1)
        parts.append((row[i] if 0 <= i < len(row) else "").strip().casefold())
    return "|".join(parts)


def _existing_keys(ws) -> Dict[str, int]:
    values, index = _sheet_records(ws)
    if not values:
        return {}
    keys: Dict[str, int] = {}
    for r in range(1, len(values)):
        k = _key_for_row(values[r], index)
        if k and k not in keys:
            keys[k] = r
    return keys


def _deduplicate_in_sheet(ws) -> int:
    values, index = _sheet_records(ws)
    if not values or len(values) <= 2:
        return 0
    header = values[0]
    seen = set()
    unique = [header]
    removed = 0
    for r in range(1, len(values)):
        k = _key_for_row(values[r], index)
        if k in seen:
            removed += 1
        else:
            seen.add(k)
            unique.append(values[r])
    if removed > 0:
        ws.clear()
        ws.update(values=unique, range_name="A1")
    return removed


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _shape_for_sheet(imported_at: str, raw_row: List[str]) -> List[str]:
    row = [imported_at] + list(raw_row)
    if len(row) < len(HEADERS):
        row += [""] * (len(HEADERS) - len(row))
    else:
        row = row[: len(HEADERS)]
    return row


def main():
    username = os.environ.get("PORTAL_USER", "")
    password = os.environ.get("PORTAL_PASS", "")
    students = [s.strip() for s in os.environ.get("STUDENTS", "").split(",") if s.strip()]

    dbg(f"students: {students!r}")

    ws = _open_worksheet()
    _ensure_header(ws)

    rows_raw, metrics = run_scrape(username, password, students)

    imported_at = _now_iso()
    shaped = [_shape_for_sheet(imported_at, r) for r in rows_raw]

    existing = _existing_keys(ws)
    before = len(shaped)

    index = {name: i for i, name in enumerate(HEADERS)}

    def _k(row: List[str]) -> str:
        return "|".join(((row[index[c]] if index[c] < len(row) else "").strip().casefold()) for c in DEDUP_COLS)

    shaped = [r for r in shaped if _k(r) not in existing]
    skipped = before - len(shaped)

    imported = 0
    if shaped:
        ws.append_rows(shaped, value_input_option="USER_ENTERED")
        imported = len(shaped)

    removed_dups = _deduplicate_in_sheet(ws)

    print(
        f"Imported {imported} new rows. Skipped as duplicates (before append): {skipped}. "
        f"Removed legacy dups during cleanup: {removed_dups}."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
