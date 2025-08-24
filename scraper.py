# scraper.py
from __future__ import annotations
import re
import time
from typing import List
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout


BASE_URL = "https://parentportal.cajonvalley.net"


def dprint(*args):
    print("DEBUG —", *args, flush=True)


def safe_text(el, default: str = "") -> str:
    try:
        return (el.inner_text() if el else default).strip()
    except Exception:
        return default


def safe_num(txt: str) -> str:
    """Keep as string for Sheets, but normalize whitespace."""
    return (txt or "").strip()


def wait_for_dom(page: Page, sleep_after: float = 0.0):
    page.wait_for_load_state("domcontentloaded")
    if sleep_after:
        time.sleep(sleep_after)


def safe_goto_portal(page: Page):
    """
    Go to PortalMainPage but swallow a navigation abort if the site is already navigating.
    """
    try:
        page.goto(f"{BASE_URL}/Home/PortalMainPage", wait_until="domcontentloaded")
    except Exception as e:
        if "ERR_ABORTED" not in str(e):
            raise
    wait_for_dom(page, 0.4)


def login(page: Page, pin: str, password: str):
    page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")

    # Make sure the two login fields are visible
    pin_ok = False
    pwd_ok = False
    try:
        page.wait_for_selector("#Pin", timeout=15000)
        pin_ok = True
    except PWTimeout:
        pass
    try:
        page.wait_for_selector("#Password", timeout=15000)
        pwd_ok = True
    except PWTimeout:
        pass
    dprint("login fields visible:", pin_ok, pwd_ok)

    page.fill("#Pin", str(pin))
    page.fill("#Password", str(password))
    page.click("#LoginButton")

    # Let the site redirect on its own; avoid issuing our own goto immediately.
    try:
        page.wait_for_url(re.compile(r".*/Home/PortalMainPage.*"), timeout=20000)
    except Exception:
        # If we didn't land there, try once (swallow a possible abort).
        safe_goto_portal(page)

    wait_for_dom(page, 0.5)


def build_student_map(page: Page) -> dict:
    """
    Open the student selector and build a map of Nickname -> stuuniq.
    """
    stu_map = {}
    try:
        # The "family" icon cell opens the student selector
        page.click("#openSelect")
        page.wait_for_selector("#divSelectStudent .studentTile", timeout=8000)

        tiles = page.query_selector_all("#divSelectStudent .studentTile")
        for t in tiles:
            nick = safe_text(t.query_selector(".tileStudentNickname"))
            uniq = t.get_attribute("data-stuuniq")
            if nick and uniq:
                stu_map[nick] = uniq

        # Close the selector (clicking the header image or anywhere works)
        try:
            page.click("#imgStudents")
        except Exception:
            try:
                page.click("body", position={"x": 5, "y": 5})
            except Exception:
                pass

    except Exception as e:
        dprint("could not open student selector:", e)

    dprint("student map:", stu_map)
    return stu_map


def set_student(page: Page, stuuniq: str):
    """
    Switch the current student by calling the banner endpoint, then ensure
    we are back on the portal page.
    """
    page.goto(f"{BASE_URL}/StudentBanner/SetStudentBanner/{stuuniq}", wait_until="domcontentloaded")
    safe_goto_portal(page)


def extract_assignment_rows_from_table(page: Page, table_css: str, student: str, course_name: str) -> List[List[str]]:
    """
    Given a tblassign table, parse all rows into lists for Sheets.
    Columns returned:
      [Student, Course, Due Date, Assigned Date, Assignment, Points Possible, Score, Percent, Missing?, Not Graded?, Comments]
    """
    rows_out: List[List[str]] = []
    # Skip the "No Assignments Available" table quickly
    if page.query_selector(f"{table_css} tbody tr td:has-text('No Assignments Available')"):
        return rows_out

    trs = page.query_selector_all(f"{table_css} tbody tr")
    for tr in trs:
        # Skip group headers or weird rows that don't have enough cells
        tds = tr.query_selector_all("td")
        if len(tds) < 5:
            continue

        # Pull columns by position; guard each one.
        due = safe_text(tds[1]) if len(tds) > 1 else ""
        assigned = safe_text(tds[2]) if len(tds) > 2 else ""
        assignment = safe_text(tds[3]) if len(tds) > 3 else ""
        pts_possible = safe_num(safe_text(tds[4]) if len(tds) > 4 else "")
        score = safe_num(safe_text(tds[5]) if len(tds) > 5 else "")
        pct = safe_num(safe_text(tds[6]) if len(tds) > 6 else "")
        # not strictly needed but included for completeness
        not_graded = safe_text(tds[9]) if len(tds) > 9 else ""
        comments = safe_text(tds[10]) if len(tds) > 10 else ""

        # Determine "missing" from row class or empty score
        tr_class = (tr.get_attribute("class") or "").lower()
        missing = ("missingassignment" in tr_class) or (score == "" and pct == "")

        # Ignore obviously blank rows (like spacing rows)
        if assignment == "" and due == "" and assigned == "":
            continue

        rows_out.append([
            student,
            course_name,
            due,
            assigned,
            assignment,
            pts_possible,
            score,
            pct,
            "Y" if missing else "",
            not_graded,
            comments,
        ])

    return rows_out


def parse_assignments_for_student(page: Page, student: str) -> List[List[str]]:
    """
    Ensure the Assignments area is present, then extract rows from all class tables.
    """
    rows: List[List[str]] = []

    # Try to ensure Assignments panel is visible/loaded
    try:
        # If the left menu exists, click the Assignments area item to focus/scroll it.
        if page.query_selector("tr#Assignments"):
            page.click("tr#Assignments")
    except Exception:
        pass

    try:
        page.wait_for_selector("#SP_Assignments", timeout=10000)
    except PWTimeout:
        dprint("assign_root not found for", student)
        return rows

    # All class tables for assignments live under #SP_Assignments with class .tblassign
    tables = page.query_selector_all("#SP_Assignments table.tblassign")
    dprint("class tables for", f"{student}:", len(tables))

    for tbl in tables:
        # Build a CSS to reference this table again (via its id if present)
        tbl_id = tbl.get_attribute("id")
        tbl_css = f"#{tbl_id}" if tbl_id else "#SP_Assignments table.tblassign"

        # Course name is in the <caption> element of the table
        course = ""
        try:
            cap = tbl.query_selector("caption")
            course = safe_text(cap)
            # Clean the "Per: X   " prefix if present
            course = re.sub(r"^\s*Per\s*:\s*\S+\s*", "", course)
        except Exception:
            pass

        rows.extend(extract_assignment_rows_from_table(page, tbl_css, student, course))

    return rows


def run_scrape(username: str, password: str, students_csv: str) -> List[List[str]]:
    """
    Entrypoint called by main.py. Returns rows suitable for gspread append_rows().
    `students_csv` is a comma-separated string of student nicknames that appear in the tiles.
    """
    all_rows: List[List[str]] = []
    students = [s.strip() for s in students_csv.split(",") if s.strip()]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.set_default_timeout(20000)

        # ---- Login and land on Portal ----
        dprint("landed:", f"{BASE_URL}/")
        login(page, username, password)

        # ---- Map student nickname -> stuuniq ----
        stu_map = build_student_map(page)

        # ---- Iterate students ----
        for student in students:
            stuuniq = stu_map.get(student)
            if not stuuniq:
                dprint("tile not found for", student)
                continue

            # Switch student and be sure we are on PortalMainPage again
            set_student(page, stuuniq)

            # Extract assignments rows for this student
            student_rows = parse_assignments_for_student(page, student)
            dprint("scraped rows for", f"{student}:", len(student_rows))
            all_rows.extend(student_rows)

        browser.close()

    return all_rows
