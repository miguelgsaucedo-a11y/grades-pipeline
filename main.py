# main.py
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Dict, Iterable, List, Tuple

import gspread
from google.oauth2.service_account import Credentials

from scraper import run_scrape

# ---- Sheet schema -----------------------------------------------------------

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

# The columns (by header name) used to compute a stable de-dup key:
DEDUP_COLS = ["Student", "Course", "Assignment", "DueDate", "AssignedDate"]


def dbg(msg: str):
    print(f"DEBUG — {msg}")


# ---- Google Sheets helpers --------------------------------------------------

def _load_service_account() -> Credentials:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    raw = os.environ.get("GOOGLE_CREDS_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_CREDS_JSON is missing")

    # It may be a JSON blob or a path to a JSON file
    if raw.strip().startswith("{"):
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=scopes)
    else:
        return Credentials.from_service_account_file(raw, scopes=scopes)


def _open_worksheet() -> gspread.Worksheet:
    sheet_id = os.environ.get("SPREADSHEET_ID", "")
    if not sheet_id:
        raise RuntimeError("SPREADSHEET_ID is missing")
    gc = gspread.authorize(_load_service_account())
    sh = gc.open_by_key(sheet_id)
    # Use the first sheet (or change if you want a named sheet)
    return sh.sheet1


def _ensure_header(ws):
    try:
        existing = ws.row_values(1)
    except Exception:
        existing = []
    if existing != HEADERS:
        # Use named arguments to avoid deprecation warnings
        ws.update(values=[HEADERS], range_name="1:1")


def _sheet_records(ws) -> Tuple[List[List[str]], Dict[str, int]]:
    """
    Return (values, header_index_map) for the whole sheet.
    """
    values = ws.get_all_values()
    if not values:
        return [], {}
    header = values[0]
    index = {name: i for i, name in enumerate(header)}
    return values, index


def _key_for_row(row: List[str], index: Dict[str, int]) -> str:
    bits = []
    for col in DEDUP_COLS:
        i = index.get(col, -1)
        bits.append((row[i] if i >= 0 and i < len(row) else "").strip().casefold())
    return "|".join(bits)


def _existing_keys(ws) -> Dict[str, int]:
    """
    Build a dict of dedup keys already present in the sheet (row index for first occurrence).
    """
    values, index = _sheet_records(ws)
    if not values:
        return {}
    keys: Dict[str, int] = {}
    for r_idx in range(1, len(values)):  # skip header
        row = values[r_idx]
        k = _key_for_row(row, index)
        if k and k not in keys:
            keys[k] = r_idx
    return keys


def _deduplicate_in_sheet(ws) -> int:
    """
    Remove any duplicate rows already in the sheet (keeping the first occurrence).
    Returns the number of removed rows.
    """
    values, index = _sheet_records(ws)
    if not values or len(values) <= 2:
        return 0

    header = values[0]
    seen = set()
    unique = [header]
    removed = 0

    for r_idx in range(1, len(values)):
        row = values[r_idx]
        k = _key_for_row(row, index)
        if k in seen:
            removed += 1
            continue
        seen.add(k)
        unique.append(row)

    if removed > 0:
        # Clear and rewrite
        ws.clear()
        ws.update(values=unique, range_name=f"A1:{gspread.utils.rowcol_to_a1(len(header), len(unique))}")
    return removed


# ---- Row shaping ------------------------------------------------------------

def _now_iso() -> str:
    # Use local time, ISO-like, seconds precision
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _shape_for_sheet(imported_at: str, raw_row: List[str]) -> List[str]:
    """
    raw_row comes from scraper in this order:
      ["Student","Period","Course","Teacher","DueDate","AssignedDate","Assignment",
       "PtsPossible","Score","Pct","Status","Comments","SourceURL"]
    We just prepend ImportedAt and pad to our HEADERS.
    """
    row = [imported_at] + list(raw_row)
    # Pad/trim exactly to HEADERS len
    if len(row) < len(HEADERS):
        row += [""] * (len(HEADERS) - len(row))
    elif len(row) > len(HEADERS):
        row = row[: len(HEADERS)]
    return row


# ---- Main -------------------------------------------------------------------

def main():
    username = os.environ.get("PORTAL_USER", "")
    password = os.environ.get("PORTAL_PASS", "")
    students_csv = os.environ.get("STUDENTS", "")
    students = [s.strip() for s in students_csv.split(",") if s.strip()]

    dbg(f"students: {students!r}")

    # Open Sheet & ensure header exists up-front
    ws = _open_worksheet()
    _ensure_header(ws)

    # Scrape portal
    rows_raw, metrics = run_scrape(username, password, students)

    # Prepare shaped rows & de-dup before appending
    imported_at = _now_iso()
    shaped = [_shape_for_sheet(imported_at, r) for r in rows_raw]

    # Build in-memory dedup set from the sheet
    existing = _existing_keys(ws)
    before_count = len(shaped)

    # Compute index for our shaped rows according to HEADERS
    index = {name: i for i, name in enumerate(HEADERS)}

    def _key_from_shaped(row: List[str]) -> str:
        bits = []
        for col in DEDUP_COLS:
            i = index[col]
            bits.append((row[i] if i < len(row) else "").strip().casefold())
        return "|".join(bits)

    # Filter out any shaped rows whose keys already exist in the sheet
    shaped = [r for r in shaped if _key_from_shaped(r) not in existing]

    skipped = before_count - len(shaped)

    # Append if any
    if shaped:
        # Append starting at the first empty row
        start_row = ws.row_count + 1  # not strictly needed; gspread handles append
        ws.append_rows(shaped, value_input_option="USER_ENTERED")
        imported = len(shaped)
    else:
        imported = 0

    # Optional: clean up legacy duplicates already in sheet
    removed_dups = _deduplicate_in_sheet(ws)

    print(
        f"Imported {imported} new rows. Skipped as duplicates (before append): {skipped}. "
        f"Removed legacy dups during cleanup: {removed_dups}."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Print a concise error; also keep a non-zero exit
        print("ERROR:", e)
        sys.exit(1)
