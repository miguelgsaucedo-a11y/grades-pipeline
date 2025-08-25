# scraper.py
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from typing import List, Dict, Tuple
import re
import time

PORTAL_ROOT = "https://parentportal.cajonvalley.net"
PORTAL_HOME = f"{PORTAL_ROOT}/Home/PortalMainPage"

def _visible_text_sample(page, max_len=160) -> str:
    # small helper for logging – sample visible text on the page
    try:
        txt = page.locator("body").inner_text(timeout=2000)
        return re.sub(r"\s+", " ", txt).strip()[:max_len]
    except Exception:
        return ""

def _goto(page, url: str, wait="domcontentloaded", timeout=15000):
    page.goto(url, wait_until=wait, timeout=timeout)

def _ensure_logged_in(page, username: str, password: str, timeout=20000):
    # Always start from base URL to reset session
    _goto(page, PORTAL_ROOT, wait="domcontentloaded", timeout=timeout)

    # Dismiss any “session timed out” alert dialog if it pops
    try:
        dlg = page.get_by_role("button", name=re.compile(r"OK|Close|Continue", re.I))
        if dlg.is_visible(timeout=1500):
            dlg.click()
    except Exception:
        pass

    # Detect login form
    login_fields = page.locator('input[name="PIN"], input#PIN, input[name="Password"], input#Password')
    has_login = login_fields.count() > 0

    if not has_login:
        # Some gateways redirect straight to main page when cookie is valid.
        # Force-open the login page to be certain.
        try:
            _goto(page, PORTAL_ROOT + "/Account/LogOn", wait="domcontentloaded", timeout=timeout)
        except Exception:
            pass
        login_fields = page.locator('input[name="PIN"], input#PIN, input[name="Password"], input#Password')
        has_login = login_fields.count() > 0

    if has_login:
        # Fill form (common variants)
        try:
            pin = page.locator('input[name="PIN"], input#PIN').first
            pwd = page.locator('input[name="Password"], input#Password').first
            pin.fill(username, timeout=5000)
            pwd.fill(password, timeout=5000)
        except Exception:
            raise RuntimeError("Could not fill login form")

        # Click a “Login” button
        try:
            page.get_by_role("button", name=re.compile(r"Log\s*in|Sign\s*in|Login", re.I)).first.click()
        except Exception:
            # fallback: submit via Enter
            pwd.press("Enter")

        # Navigate to the portal home after login
        try:
            _goto(page, PORTAL_HOME, wait="domcontentloaded", timeout=timeout)
        except Exception:
            # try committing once in case of the net::ERR_ABORTED on quick redirect
            page.wait_for_timeout(600)
            _goto(page, PORTAL_HOME, wait="domcontentloaded", timeout=timeout)
    else:
        # Already logged in – make sure we’re at the portal page
        try:
            _goto(page, PORTAL_HOME, wait="domcontentloaded", timeout=timeout)
        except Exception:
            pass

def _try_open_student_menu(page):
    # Open any UI that reveals student names
    # Common variants seen across PowerSchool-like portals
    candidates = [
        '#divStudentBanner',          # banner area w/ student name
        'button[aria-controls*="Student"]',
        'button:has-text("Student")',
        'a:has-text("Student")',
        'a:has-text("Change Student")',
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                page.wait_for_timeout(150)
                return True
        except Exception:
            continue
    return False

def _switch_to_student(page, name: str) -> bool:
    # First: click directly on the name if visible
    name_xpath = f'//a[contains(normalize-space(.), "{name}") or contains(@title, "{name}")]'
    try:
        links = page.locator(name_xpath)
        if links.count() > 0 and links.first.is_visible():
            links.first.click()
            page.wait_for_load_state("domcontentloaded")
            return True
    except Exception:
        pass

    # Second: open a student menu, then click the name
    _try_open_student_menu(page)
    try:
        links = page.locator(name_xpath)
        if links.count() > 0 and links.first.is_visible():
            links.first.click()
            page.wait_for_load_state("domcontentloaded")
            return True
    except Exception:
        pass

    # Third: sometimes names are buttons, not links
    btn_xpath = f'//button[contains(normalize-space(.), "{name}")]'
    try:
        btns = page.locator(btn_xpath)
        if btns.count() > 0 and btns.first.is_visible():
            btns.first.click()
            page.wait_for_load_state("domcontentloaded")
            return True
    except Exception:
        pass

    return False

def _extract_tables_for_current_student(page) -> List[Dict]:
    """
    Scrape all assignment tables visible on the PortalMainPage.
    We look for tables with id that starts with 'tblAssign_' which is how this portal renders lists.
    """
    rows: List[Dict] = []

    # In practice these tables appear after the section header which hints the course.
    tables = page.locator('table.tblassign, table[id^="tblAssign_"]')
    tcount = tables.count()

    # If the DOM is present but not yet visible, give it a moment
    if tcount == 0:
        # small staged waits
        for _ in range(5):
            page.wait_for_timeout(250)
            tcount = page.locator('table.tblassign, table[id^="tblAssign_"]').count()
            if tcount:
                break

    for i in range(tcount):
        table = tables.nth(i)

        # Derive a course label from the nearest header above this table
        course = ""
        try:
            hdr = table.locator("xpath=preceding::*[self::h1 or self::h2 or self::h3 or self::h4][1]")
            if hdr.count():
                course = re.sub(r"\s+", " ", hdr.first.inner_text().strip())
                if re.search(r"Assignments?\s+Show\s+All", course, re.I):
                    # not actionable – generic “Assignments Show All” header
                    course = ""
        except Exception:
            pass

        # Now read table rows
        try:
            body_rows = table.locator("tbody tr")
            for r in range(body_rows.count()):
                tr = body_rows.nth(r)
                tds = tr.locator("td")
                c = tds.count()
                if c == 0:
                    continue

                text_cells = [re.sub(r"\s+", " ", (tds.nth(k).inner_text() or "").strip()) for k in range(c)]

                # Heuristics for the common column layout on this portal:
                #  Assignment | Due Date | Assigned Date | Points Possible | Score | % | Status | Comments
                # Some classes omit Assigned Date – so we guard with indexes.
                def cell(idx, default=""):
                    return text_cells[idx] if idx < len(text_cells) else default

                assignment = cell(0)
                due_date = cell(1)
                assigned_date = cell(2)
                # If the second cell looks like a number, the table might be: Assignment | Pos | Score | %
                if re.fullmatch(r"\d+(\.\d+)?", due_date):
                    # shift fields right
                    assigned_date = ""
                    pts_possible = cell(1)
                    score = cell(2)
                    pct = cell(3)
                else:
                    pts_possible = cell(3)
                    score = cell(4)
                    pct = cell(5)

                status = ""
                comments = ""
                # Try to find status/comments if present
                if len(text_cells) >= 7:
                    status = cell(6)
                if len(text_cells) >= 8:
                    comments = cell(7)

                # Derive status if blank
                if not status:
                    if re.search(r"missing", assignment, re.I):
                        status = "MISSING"
                    elif pct and re.fullmatch(r"100%?", pct):
                        status = "OK"

                rows.append({
                    "Period": "",                             # not reliably on this page
                    "Course": course,
                    "Teacher": "",                            # set by header if available below
                    "DueDate": due_date,
                    "AssignedDate": assigned_date,
                    "Assignment": assignment,
                    "PtsPossible": pts_possible,
                    "Score": score,
                    "Pct": pct,
                    "Status": status,
                    "Comments": comments,
                    "SourceURL": page.url
                })
        except Exception:
            continue

    # Try to capture teacher name from a nearby header on the page (appears next to the course block)
    try:
        teacher_hdr = page.locator("xpath=//h1|//h2|//h3|//h4")
        if teacher_hdr.count():
            header_text = " ".join([
                re.sub(r"\s+", " ", teacher_hdr.nth(i).inner_text().strip())
                for i in range(min(teacher_hdr.count(), 4))
            ])
            # crude teacher extraction like "Scarbrough, P"
            m = re.search(r"([A-Z][a-z]+,\s*[A-Z](?:\.)?)", header_text)
            teacher = m.group(1) if m else ""
            for r in rows:
                if not r["Teacher"]:
                    r["Teacher"] = teacher
    except Exception:
        pass

    return rows

def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[Dict], Dict]:
    scraped_rows: List[Dict] = []
    metrics = {
        "students": students,
        "per_student_table_counts": {},
        "ui_url": "",
        "ui_sample": ""
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()

        _ensure_logged_in(page, username, password, timeout=25000)

        metrics["ui_url"] = page.url
        metrics["ui_sample"] = _visible_text_sample(page)

        # Iterate students
        for s in students:
            switched = _switch_to_student(page, s)
            if not switched:
                metrics["per_student_table_counts"][s] = 0
                continue

            tables_rows = _extract_tables_for_current_student(page)
            metrics["per_student_table_counts"][s] = len(tables_rows)
            # Attach student name on each row
            for r in tables_rows:
                r["Student"] = s
            scraped_rows.extend(tables_rows)

        browser.close()

    return scraped_rows, metrics
