import os
import json
from datetime import datetime
from typing import List, Dict, Tuple
import gspread

# ---- Sheet schema (must match your Google Sheet header row) ----
HEADERS = [
    "ImportedAt", "Student", "Period", "Course", "Teacher",
    "DueDate", "AssignedDate", "Assignment", "PtsPossible",
    "Score", "Pct", "Status", "Comments", "SourceURL"
]

# ---- Utils -----------------------------------------------------

def _norm_text(val: str) -> str:
    s = (val or "").strip()
    # collapse whitespace and lowercase
    return " ".join(s.split()).lower()

def _norm_date(val: str) -> str:
    if not val:
        return ""
    s = str(val).strip()
    if not s or s.lower() in {"missing", "n/a", "na", "not available"}:
        return s.lower()  # keep the sentinel as-is (e.g., "missing")
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    # fallback—normalize but don't break unparseable content
    return " ".join(s.split()).lower()

def key_from_row(row: Dict[str, str]) -> Tuple[str, str, str, str]:
    """
    Build a stable dedup key:
    Student + Period + Course(=assignment title) + DueDate
    """
    return (
        _norm_text(row.get("Student", "")),
        _norm_text(row.get("Period", "")),
        _norm_text(row.get("Course", "")),
        _norm_date(row.get("DueDate", "")),
    )

def now_iso_seconds() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def ensure_worksheet(spreadsheet, title: str):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=len(HEADERS))
        ws.update("1:1", [HEADERS])
        return ws

def get_existing_keys(ws) -> set:
    """
    Build a set of existing keys from the sheet (skips header).
    """
    values = ws.get_all_values()
    if not values:
        return set()
    header = values[0]
    col_idx = {name: header.index(name) for name in HEADERS if name in header}
    keys = set()
    for row_vals in values[1:]:
        # guard for ragged rows
        def col(name):
            i = col_idx.get(name, -1)
            return row_vals[i] if 0 <= i < len(row_vals) else ""
        row_obj = {
            "Student": col("Student"),
            "Period": col("Period"),
            "Course": col("Course"),
            "DueDate": col("DueDate"),
        }
        keys.add(key_from_row(row_obj))
    return keys

def rows_to_matrix(rows: List[Dict[str, str]]) -> List[List[str]]:
    """Map dict rows to the exact HEADERS order; missing keys -> ''."""
    matrix = []
    for r in rows:
        matrix.append([str(r.get(h, "") or "") for h in HEADERS])
    return matrix

def dedupe_entire_sheet(ws):
    """
    One-pass de-dup across the whole sheet.
    Keep the row with the most recent ImportedAt for each key.
    """
    values = ws.get_all_values()
    if not values:
        return 0

    header = values[0]
    # Map header names to indices—even if the sheet has columns in the expected order,
    # this protects us if a user reorders columns by hand.
    idx = {name: header.index(name) for name in HEADERS if name in header}

    def get(row, name):
        i = idx.get(name, -1)
        return row[i] if 0 <= i < len(row) else ""

    winners: Dict[Tuple[str, str, str, str], List[str]] = {}
    skipped = 0

    for row_vals in values[1:]:
        row_obj = {
            "Student": get(row_vals, "Student"),
            "Period":  get(row_vals, "Period"),
            "Course":  get(row_vals, "Course"),
            "DueDate": get(row_vals, "DueDate"),
        }
        k = key_from_row(row_obj)

        # choose the one with the newest ImportedAt (lexicographic OK because it's ISO-ish)
        existing = winners.get(k)
        if not existing:
            winners[k] = row_vals
        else:
            new_ts = get(row_vals, "ImportedAt")
            old_ts = get(existing,  "ImportedAt")
            if (new_ts or "") > (old_ts or ""):
                winners[k] = row_vals
            else:
                skipped += 1

    # Rebuild: header + deduped rows in a stable order (by ImportedAt desc)
    def ts(v): 
        i = idx.get("ImportedAt", -1)
        return v[i] if 0 <= i < len(v) else ""
    deduped_rows = sorted(list(winners.values()), key=lambda r: ts(r), reverse=True)

    ws.clear()
    ws.update("1:1", [header])
    if deduped_rows:
        ws.append_rows(deduped_rows, value_input_option="RAW")
    return skipped

# ---- Import from scraper -----------------------------------------------------
# We assume your existing scraper exposes `run_scrape(username, password, students)`
# and returns: List[Dict[str, Any]] rows
from scraper import run_scrape  # noqa

# ---- Main --------------------------------------------------------------------

def main():
    username = os.environ.get("PORTAL_USER", "").strip()
    password = os.environ.get("PORTAL_PASS", "").strip()
    students_csv = os.environ.get("STUDENTS", "").strip()
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    creds_json = os.environ.get("GOOGLE_CREDS_JSON", "").strip()

    if not (username and password and students_csv and spreadsheet_id and creds_json):
        raise SystemExit("Missing env vars: PORTAL_USER, PORTAL_PASS, STUDENTS, SPREADSHEET_ID, GOOGLE_CREDS_JSON")

    students = [s.strip() for s in students_csv.split(",") if s.strip()]
    print(f"DEBUG — students: {students}")

    # Scrape
    rows = run_scrape(username, password, students)  # your existing function
    print(f"DEBUG — scraped {len(rows)} rows from portal")

    # Stamp ImportedAt and normalize empty keys
    imported_at = now_iso_seconds()
    for r in rows:
        r["ImportedAt"] = r.get("ImportedAt") or imported_at
        # ensure all expected keys exist so append_rows lines up
        for h in HEADERS:
            r.setdefault(h, "")

    # Sheets client
    try:
        creds = json.loads(creds_json)
    except Exception as e:
        raise SystemExit(f"GOOGLE_CREDS_JSON is not valid JSON: {e}")

    gc = gspread.service_account_from_dict(creds)
    ss = gc.open_by_key(spreadsheet_id)
    ws = ensure_worksheet(ss, "Assignments")

    # 1) Only append non-duplicates (based on existing keys)
    existing = get_existing_keys(ws)
    to_add = [r for r in rows if key_from_row(r) not in existing]

    if to_add:
        ws.append_rows(rows_to_matrix(to_add), value_input_option="RAW")

    # 2) Full-sheet de-dup to clean legacy repeats; keep the newest ImportedAt
    legacy_skipped = dedupe_entire_sheet(ws)

    print(
        f"Imported {len(to_add)} new rows. "
        f"Skipped as duplicates (before append): {len(rows) - len(to_add)}. "
        f"Removed legacy dups during cleanup: {legacy_skipped}."
    )

if __name__ == "__main__":
    main()
