import json
import re
import time
from datetime import datetime
from typing import Dict, List, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import gspread
from google.oauth2.service_account import Credentials

BASE_URL = "https://parentportal.cajonvalley.net/"


# ---------- Google Sheets ----------

SHEET_HEADERS = [
    "ImportedAt",   # A
    "Student",      # B
    "Period",       # C  (e.g., '2  CC Math 6 (T2201YF1)')
    "Course",       # D  (assignment title)
    "Teacher",      # E
    "DueDate",      # F
    "AssignedDate", # G
    "Assignment",   # H (left blank, reserved if you later want another label)
    "PtsPossible",  # I
    "Score",        # J
    "Pct",          # K (like '100%')
    "Status",       # L (WIN, MISSING, etc.)
    "Comments",     # M
    "SourceURL",    # N (print progress link per class)
]

def _authorize_gspread(google_creds_json: str):
    info = json.loads(google_creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc

def append_to_sheet(spreadsheet_id: str, google_creds_json: str, rows: List[Dict], worksheet_title: str = "Assignments") -> int:
    gc = _authorize_gspread(google_creds_json)
    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(worksheet_title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_title, rows="100", cols=str(len(SHEET_HEADERS)))
        ws.append_row(SHEET_HEADERS)

    # Ensure headers exist (idempotent)
    try:
        existing_headers = ws.row_values(1)
    except Exception:
        existing_headers = []
    if existing_headers != SHEET_HEADERS:
        if existing_headers:
            ws.delete_rows(1)
        ws.insert_row(SHEET_HEADERS, 1)

    values = []
    for r in rows:
        values.append([r.get(h, "") for h in SHEET_HEADERS])

    if values:
        ws.append_rows(values, value_input_option="USER_ENTERED")
    return len(values)


# ---------- Scraper ----------

def normalize(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "").strip())

def header_key(label: str) -> str:
    """Map varying header text to canonical keys."""
    t = normalize(label).lower()
    t = t.replace(":", "")
    t = t.replace("score", "score")  # no-op, here for clarity
    # Unify column names the portal uses
    mapping = {
        "date due": "due",
        "assigned": "assigned",
        "assignment": "title",
        "pts possible": "pts",
        "score": "score",
        "pct score": "pct",
        "scored as": "status",
        "comments": "comments",
        "extra credit": "extra",
        "not graded": "ng",
    }
    return mapping.get(t, t)

def click_and_wait(page, selector: str, timeout=8000):
    page.click(selector)
    page.wait_for_timeout(300)  # small settle

def login(page, username: str, password: str):
    page.goto(BASE_URL, wait_until="domcontentloaded")
    print(f"DEBUG — landed: {page.url}")
    # Ensure login fields visible
    page.wait_for_selector("#Pin", timeout=10000)
    page.wait_for_selector("#Password", timeout=10000)
    print(f"DEBUG — login fields visible: True True")

    page.fill("#Pin", username)
    page.fill("#Password", password)
    page.click("#LoginButton")
    # After login we stay on main; wait for student banner
    page.wait_for_selector("#divStudentBanner", timeout=20000)
    print("DEBUG — after login: portal loaded")

def open_student_picker(page):
    # open the tiles if they are hidden
    try:
        banner = page.locator("#divStudentBanner")
        # Click the family icon to open chooser
        banner.locator("#imgStudents").click()
        page.wait_for_selector("#divSelectStudent", state="visible", timeout=5000)
        return True
    except PWTimeout:
        return False

def switch_to_student(page, student_name: str) -> bool:
    """Open the student tiles and click the tile matching the nickname or name."""
    ok = open_student_picker(page)
    tiles = page.locator(".studentTile")
    if not tiles.count():
        # Sometimes the tiles are already visible or only 1 student is current;
        # Try toggling again
        page.locator("#imgStudents").click()
        page.wait_for_timeout(300)
    tiles = page.locator(".studentTile")
    if not tiles.count():
        print("DEBUG — saw student tiles: False")
        return False
    print("DEBUG — saw student tiles: True")

    # Prefer nickname match, then full name
    candidate = tiles.filter(has_text=student_name)
    if candidate.count():
        candidate.first.click()
    else:
        # As fallback, try partial contains search on .tileStudentName
        all_tiles = tiles.all()
        clicked = False
        for t in all_tiles:
            txt = (t.inner_text() or "").strip()
            if student_name.lower() in txt.lower():
                t.click()
                clicked = True
                break
        if not clicked:
            print(f"DEBUG — no tile matched for {student_name}")
            return False

    # Wait for the student profile content to refresh
    try:
        page.wait_for_selector("#SP-MainDiv", timeout=15000)
        print(f"DEBUG — switched to student {student_name}")
        return True
    except PWTimeout:
        print(f"DEBUG — failed to switch to student {student_name}")
        return False

def ensure_assignments_ready(page):
    """Make sure Assignments area is loaded/visible."""
    # Left menu id for Assignments is #Assignments. Click to ensure area toggled open.
    try:
        # Make sure the main detail pane exists
        page.wait_for_selector("#SP_Detail", timeout=15000)
        # Focus Assignments area by clicking its menu row (id='Assignments')
        click_and_wait(page, "#Assignments")
    except Exception:
        pass

    # The Assignments section uses #SP_Assignments div and produces tables with class 'tblassign'
    try:
        page.wait_for_selector("#SP_Assignments", timeout=15000)
    except PWTimeout:
        return False

    # Either a table appears or a “No Assignments Available” text does
    try:
        page.wait_for_selector(
            "#SP_Assignments table.tblassign, text=No Assignments Available",
            timeout=10000
        )
    except PWTimeout:
        return False

    # For logging
    try:
        tc = page.locator("input#tablecount")
        if tc.count():
            print(f"DEBUG — tablecount marker: {normalize(tc.first.get_attribute('value') or '') or '—'}")
        else:
            print("DEBUG — tablecount marker: (none)")
    except Exception:
        print("DEBUG — tablecount marker: (read error)")

    return True

def extract_header_map(tbl) -> Dict[str, int]:
    """Return a map like {'due': 1, 'assigned': 2, 'title': 3, ...} based on the thead text."""
    header_map: Dict[str, int] = {}
    ths = tbl.locator("thead th")
    for idx in range(ths.count()):
        raw = ths.nth(idx).inner_text()
        key = header_key(raw)
        if key:
            header_map[key] = idx  # 0-based
    return header_map

def caption_period_text(tbl) -> str:
    """From the <caption> e.g. 'Per: 2   CC Math 6 (T2201YF1)' -> '2  CC Math 6 (T2201YF1)'"""
    cap = normalize(tbl.locator("caption").inner_text() or "")
    # Remove leading 'Per:' if present
    cap = re.sub(r"^per\s*:\s*", "", cap, flags=re.I)
    return cap

def header_teacher(tbl) -> str:
    """Teacher appears in the first header row right-hand cell with a mailto link."""
    try:
        link = tbl.locator("thead a[title='Send Email']")
        if link.count():
            return normalize(link.first.inner_text())
    except Exception:
        pass
    return ""

def table_ids_and_term(tbl) -> Tuple[str, str]:
    """
    Extract:
      - mstuniq (from table id, e.g., 'tblAssign_1150732')
      - term code (from hidden input id='showmrktermc_X' in this header)
    """
    mstuniq = ""
    term = ""
    try:
        tbl_id = tbl.get_attribute("id") or ""
        m = re.search(r"tblAssign_(\d+)", tbl_id)
        if m:
            mstuniq = m.group(1)
    except Exception:
        pass

    try:
        hidden = tbl.locator("input[id^='showmrktermc_']")
        if hidden.count():
            term = (hidden.first.get_attribute("value") or "").strip()
    except Exception:
        pass
    return mstuniq, term

def build_print_url(mstuniq: str, term: str) -> str:
    if not mstuniq or not term:
        return ""
    # Mirrors PrintProgress2 JS: /Home/PrintProgressReport/mstuniq^term
    return f"{BASE_URL.rstrip('/')}/Home/PrintProgressReport/{mstuniq}^{term}"

def extract_rows_from_table(student: str, tbl) -> List[Dict]:
    rows: List[Dict] = []

    # Map headers to indices
    hmap = extract_header_map(tbl)
    period_text = caption_period_text(tbl)
    teacher = header_teacher(tbl)
    mstuniq, term = table_ids_and_term(tbl)
    source_url = build_print_url(mstuniq, term)

    # tbody rows
    body_rows = tbl.locator("tbody tr")
    for rix in range(body_rows.count()):
        row = body_rows.nth(rix)

        # Skip blank/info-only rows
        txt_all = normalize(row.inner_text() or "")
        if not txt_all or "No Assignments Available" in txt_all:
            continue

        classes = row.get_attribute("class") or ""
        is_missing = "missingAssignment" in classes

        # Pull all <td> text
        tds = [normalize(x) for x in row.locator("td").all_inner_texts()]

        def get_col(key: str) -> str:
            if key not in hmap:
                return ""
            idx = hmap[key]
            if idx < len(tds):
                return tds[idx]
            return ""

        due = get_col("due")
        assigned = get_col("assigned")
        title = get_col("title")  # assignment name
        pts = get_col("pts")
        score = get_col("score")
        pct = get_col("pct")
        status = get_col("status")
        comments = get_col("comments")

        if is_missing:
            status = "MISSING"

        # Normalize numbers where helpful (leave as strings for the sheet)
        pts = re.sub(r"[^\d.]", "", pts) if pts else pts
        score = re.sub(r"[^\d.]", "", score) if score else score
        pct = pct  # keep like '100%'

        rows.append({
            "ImportedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Student": student,
            "Period": period_text,
            "Course": title,        # keep assignment title in your 'Course' column to match your sheet
            "Teacher": teacher,
            "DueDate": due,
            "AssignedDate": assigned,
            "Assignment": "",       # left open if you later want different semantics
            "PtsPossible": pts,
            "Score": score,
            "Pct": pct,
            "Status": status,
            "Comments": comments,
            "SourceURL": source_url,
        })

    return rows

def extract_assignments_for_student(page, student: str) -> Tuple[List[Dict], Dict]:
    collected: List[Dict] = []

    if not ensure_assignments_ready(page):
        print("DEBUG — Assignments area not ready")
        return [], {"wins": 0, "missing_low": 0}

    tables = page.locator("#SP_Assignments table.tblassign")
    tbl_count = tables.count()
    print(f"DEBUG — class tables for {student}: {tbl_count}")

    ids = []
    for i in range(tbl_count):
        tbl = tables.nth(i)
        tid = tbl.get_attribute("id") or ""
        ids.append(tid)
        collected.extend(extract_rows_from_table(student, tbl))
    if ids:
        print(f"DEBUG — found assignment tables (ids): {ids}")

    # compute metrics
    wins = sum(1 for r in collected if (r.get("Status", "").upper() == "WIN"))
    missing_low = sum(1 for r in collected if (r.get("Status", "").upper() in ("MISSING", "LOW")))

    return collected, {"wins": wins, "missing_low": missing_low}

def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[Dict], Dict]:
    all_rows: List[Dict] = []
    agg = {"wins": 0, "missing_low": 0}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()

        login(page, username, password)

        # For each student, open tiles & click the right one
        for s in students:
            switched = switch_to_student(page, s)
            if not switched:
                continue

            rows, metrics = extract_assignments_for_student(page, s)
            all_rows.extend(rows)
            agg["wins"] += metrics.get("wins", 0)
            agg["missing_low"] += metrics.get("missing_low", 0)

        browser.close()

    return all_rows, agg
