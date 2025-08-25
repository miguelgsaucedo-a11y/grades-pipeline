# scraper.py
from __future__ import annotations

import re
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

from playwright.sync_api import sync_playwright, Page, Locator

# ------------------------------
# Portal URLs & simple helpers
# ------------------------------
BASE = "https://parentportal.cajonvalley.net"
LOGIN_URL = BASE  # the root shows the actual login form (PIN / Password / Login)
PORTAL_HOME = f"{BASE}/Home/PortalMainPage"

PUBLIC_HOME_MARKERS = [
    "District Website",
    "Forget Your PIN?",
    "Reset Your Password",
    "How to Re-enroll Online",
    "How to make online payments",
    "Terms of Use",
]
ERROR_MARKERS = [
    "An error occurred while processing your request",
    "Error.htm?aspxerrorpath=",
]

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


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def _is_public_home(page: Page) -> bool:
    txt = _body_text(page).lower()
    return any(m.lower() in txt for m in PUBLIC_HOME_MARKERS)


def _is_error_page(page: Page) -> bool:
    return ("Error.htm" in page.url) or any(
        m.lower() in _body_text(page).lower() for m in ERROR_MARKERS
    )


def _dismiss_timeout_if_present(page: Page) -> None:
    """Dismiss 'Your Session Has Timed Out' dialog if visible."""
    try:
        dlg = page.get_by_role("dialog")
        if dlg.is_visible():
            ok = dlg.get_by_role("button", name=re.compile(r"ok", re.I))
            if ok.is_visible():
                print("DEBUG — login DEBUG — dismissed timeout dialog")
                ok.click()
                page.wait_for_timeout(400)
    except Exception:
        pass


def _goto(page: Page, url: str, timeout: int = 20000, label: str | None = None) -> None:
    """
    Robust navigation that tolerates client-side redirects that cause
    net::ERR_ABORTED. We retry with wait_until='commit' and keep going.
    """
    tag = label or url
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return
    except Exception as e1:
        print(f"DEBUG — goto({tag}) domcontentloaded aborted: {e1}. Retrying with 'commit'.")
        try:
            page.goto(url, wait_until="commit", timeout=timeout)
            # Let the next document begin; don't force another navigation
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
            return
        except Exception as e2:
            # As a last resort, proceed with whatever the browser has
            print(f"DEBUG — goto({tag}) commit fallback also raised: {e2}. Continuing at {page.url}.")


def _first_visible(page: Page, selectors: List[str], timeout: int = 2000) -> Locator | None:
    """Return first visible locator among CSS selectors or None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            pass
    return None


def _first_by_label(page: Page, labels: List[str], timeout: int = 2000) -> Locator | None:
    """Return first visible locator found by label text or None."""
    for label in labels:
        try:
            loc = page.get_by_label(re.compile(label, re.I)).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            pass
    return None


def ensure_logged_in_and_on_portal(page: Page, pin_value: str, password_value: str) -> None:
    """
    Always start at the root (PIN/Password/Login), submit the form, then
    force-navigate to /Home/PortalMainPage (using robust _goto).
    """
    _goto(page, LOGIN_URL, label="LOGIN_URL")
    _dismiss_timeout_if_present(page)

    # Sometimes we arrive already on the portal main page
    if "PortalMainPage" in page.url:
        return

    # If error page or public marketing home, try again
    if _is_error_page(page) or _is_public_home(page):
        _goto(page, LOGIN_URL, label="LOGIN_URL(retry)")
        _dismiss_timeout_if_present(page)

    # Find PIN / Password / Login on the root page
    pin = _first_by_label(page, [r"\bPIN\b"]) or _first_visible(
        page, ['input[name*="pin" i]', 'input[id*="pin" i]', 'input[type="text"]']
    )
    pwd = _first_by_label(page, [r"password"]) or _first_visible(
        page, ['input[type="password"]', 'input[name*="pass" i]', 'input[id*="pass" i]']
    )

    login_btn = None
    for name in [r"log\s*in", r"log\s*on", r"sign\s*in"]:
        try:
            cand = page.get_by_role("button", name=re.compile(name, re.I)).first
            if cand.is_visible(timeout=1000):
                login_btn = cand
                break
        except Exception:
            pass

    login_visible = bool(pin and pwd and login_btn)
    print(f"DEBUG — login fields visible: {login_visible}")

    if not login_visible:
        print("ERROR: Login form not found — cannot proceed.")
        return

    # Submit credentials
    pin.fill(pin_value)
    pwd.fill(password_value)
    login_btn.click()
    page.wait_for_load_state("domcontentloaded", timeout=20000)
    _dismiss_timeout_if_present(page)

    # Canonical destination after login
    _goto(page, PORTAL_HOME, label="PORTAL_HOME")
    _dismiss_timeout_if_present(page)
    print("DEBUG — after login: portal loaded")


def expand_header_if_collapsed(page: Page) -> None:
    """Open hamburger menu if the navbar is collapsed so header links/picker appear."""
    try:
        toggler = page.locator(
            'button.navbar-toggler, button[aria-label*="Toggle"], button[aria-expanded="false"]'
        ).first
        if toggler.is_visible(timeout=1200):
            toggler.click()
            page.wait_for_timeout(300)
    except Exception:
        pass


def ui_snapshot(page: Page) -> str:
    """Collect a small sample of visible nav text for debugging."""
    txt = " ".join(_body_text(page).split())
    sample = []
    for token in [
        "Home",
        "FAQs",
        "District Website",
        "Forget Your PIN?",
        "Reset Your Password",
        "online payments",
        "Terms of Use",
    ]:
        if token in txt:
            sample.append(token)
    return ", ".join(sample) if sample else "(none)"


def switch_to_student(page: Page, student_name: str) -> bool:
    """
    Switch to a student either via a picker/dropdown (preferred) or by clicking
    their name in the header/menu as a fallback.
    """
    expand_header_if_collapsed(page)

    picker_candidates = [
        "#divStudentBanner",          # container that often wraps the picker
        "#ddlStudent",
        "#studentPicker",
        '[id*="Student"][role="button"]',
    ]

    for sel in picker_candidates:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=800):
                el.click()
                # try menu items first
                try:
                    page.get_by_role("menuitem", name=re.compile(student_name, re.I)).first.click(timeout=3000)
                    return True
                except Exception:
                    pass
                # or a listbox option
                try:
                    page.get_by_role("option", name=re.compile(student_name, re.I)).first.click(timeout=3000)
                    return True
                except Exception:
                    pass
        except Exception:
            pass

    # Fallback: click the student's name text if it is a link/button in the header
    try:
        expand_header_if_collapsed(page)
        link = page.get_by_text(re.compile(rf"\b{re.escape(student_name)}\b", re.I)).first
        if link.is_visible(timeout=1500):
            link.click()
            return True
    except Exception:
        pass

    return False


def ensure_assignments_ready(page: Page) -> bool:
    """Wait for any assignments table or an explicit 'No Assignments Available' text."""
    try:
        page.wait_for_selector('table[id^="tblAssign_"], text="No Assignments Available"', timeout=12000)
        return True
    except Exception:
        return False


@dataclass
class ClassContext:
    period: str
    course: str
    teacher: str


def _extract_class_context_for_table(page: Page, table: Locator) -> ClassContext:
    """
    Heuristics to capture course/period/teacher around an assignment table.
    We look in the nearest 'panel' ancestor and its header; fall back to
    scanning preceding headings.
    """
    header_text = ""
    try:
        # typical bootstrap/asp.net panel containers
        header = table.locator(
            'xpath=ancestor::div[contains(@class,"panel") or contains(@class,"card")][1]'
            '//h3 | ancestor::div[contains(@class,"panel") or contains(@class,"card")][1]'
            '//h4 | ancestor::div[contains(@class,"panel") or contains(@class,"card")][1]'
            '//*[contains(@class,"title") or contains(@class,"header")][1]'
        ).first
        if header.is_visible():
            header_text = header.inner_text().strip()
    except Exception:
        pass

    if not header_text:
        # last resort: the nearest preceding heading
        try:
            header = table.locator("xpath=preceding::h3[1] | preceding::h4[1]").first
            if header.is_visible():
                header_text = header.inner_text().strip()
        except Exception:
            pass

    # Parse period/course/teacher with gentle regexes
    period = ""
    course = header_text.strip()
    teacher = ""

    # Period like "Period 2" or "3A"
    m = re.search(r"Period\s*([0-9A-Za-z]+)", header_text, re.I)
    if m:
        period = m.group(1)
    else:
        # Sometimes the first token is the period (e.g., "3A English 8 Honors (...)")
        m2 = re.match(r"^\s*([0-9A-Za-z]{1,3})\s+", header_text)
        if m2:
            period = m2.group(1)

    # Teacher in parens like "(Smith)" or "Teacher: Smith"
    m = re.search(r"Teacher[:\s]*([A-Za-z .,'-]+)", header_text, re.I)
    if m:
        teacher = m.group(1).strip()
    else:
        m2 = re.search(r"\(([A-Za-z .,'-]{2,})\)\s*$", header_text)
        if m2:
            teacher = m2.group(1).strip()

    return ClassContext(period=period, course=course, teacher=teacher)


def _parse_table_rows(page: Page, table: Locator, student: str) -> List[List[str]]:
    """Parse one assignment table into sheet rows (list of lists)."""
    ctx = _extract_class_context_for_table(page, table)

    # Identify header columns dynamically
    headers = []
    try:
        headers = [h.strip() for h in table.locator("thead tr th").all_inner_texts()]
    except Exception:
        pass
    if not headers:
        try:
            headers = [h.strip() for h in table.locator("tr").first.locator("th,td").all_inner_texts()]
        except Exception:
            headers = []

    def idx(name: str) -> int | None:
        for i, h in enumerate(headers):
            if re.search(rf"\b{name}\b", h, re.I):
                return i
        return None

    i_assgn = idx("Assignment") or idx("Title")
    i_due = idx("Due") or idx("Due Date")
    i_assigned = idx("Assigned")
    i_pts = idx("Pts") or idx("Points")
    i_score = idx("Score")
    i_pct = idx("Pct") or idx("%")
    i_status = idx("Status")
    i_comments = idx("Comments") or idx("Notes")

    rows_out: List[List[str]] = []

    for tr in table.locator("tbody tr").all():
        tds = [c.strip() for c in tr.locator("td").all_inner_texts()]
        if not tds:
            continue
        if any("No Assignments Available" in x for x in tds):
            continue

        def pick(i: int | None) -> str:
            return tds[i] if (i is not None and i < len(tds)) else ""

        row = [
            now_stamp(),                      # ImportedAt
            student,                          # Student
            ctx.period,                       # Period
            ctx.course,                       # Course
            ctx.teacher,                      # Teacher
            pick(i_due),                      # DueDate
            pick(i_assigned),                 # AssignedDate
            pick(i_assgn),                    # Assignment
            pick(i_pts),                      # PtsPossible
            pick(i_score),                    # Score
            pick(i_pct),                      # Pct
            pick(i_status),                   # Status
            pick(i_comments),                 # Comments
            page.url,                         # SourceURL
        ]
        rows_out.append(row)

    return rows_out


def extract_assignments_for_student(page: Page, student: str) -> Tuple[List[List[str]], int]:
    """
    Switch to the student, wait for assignments, then parse all tables.
    Returns (rows, table_count_seen).
    """
    if not switch_to_student(page, student):
        print(f"DEBUG — could not locate a picker for '{student}'; skipping switch")
        return [], 0

    if not ensure_assignments_ready(page):
        print("DEBUG — assignments view not ready; continuing")
        return [], 0

    # Find all assignment tables
    tables = page.locator('table[id^="tblAssign_"]')
    ids = []
    try:
        for el in tables.all():
            ids.append(el.get_attribute("id") or "")
    except Exception:
        pass
    if ids:
        print(f"DEBUG — found assignment tables (ids): {ids}")
    else:
        print("DEBUG — no assignment tables found (may be 'No Assignments Available')")

    all_rows: List[List[str]] = []
    for tbl in tables.all():
        all_rows.extend(_parse_table_rows(page, tbl, student))

    return all_rows, len(ids)


# ------------------------------
# Top-level runner used by main.py
# ------------------------------
def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[List[str]], Dict]:
    """
    Orchestrates browser, login and scraping for all students.
    Returns (rows, metrics).
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()

        # Always start at root login page
        _goto(page, LOGIN_URL, label="LOGIN_URL(start)")
        print(f"DEBUG — landed: {page.url}")

        ensure_logged_in_and_on_portal(page, username, password)
        if "PortalMainPage" not in page.url:
            # Nudge again (robust goto)
            _goto(page, PORTAL_HOME, label="PORTAL_HOME(nudge)")

        # Debug UI snapshot for triage
        print(f"DEBUG — UI SNAPSHOT — url: {page.url}")
        print(f"DEBUG — UI SNAPSHOT — sample: {ui_snapshot(page)}")

        all_rows: List[List[str]] = []
        total_tables = 0

        for s in students:
            stu_rows, tbl_count = extract_assignments_for_student(page, s)
            total_tables += tbl_count
            if stu_rows:
                print(f"DEBUG — class tables for {s}: {tbl_count}")
                all_rows.extend(stu_rows)
            else:
                print(f"DEBUG — class tables for {s}: 0")

        print(f"DEBUG — scraped {len(all_rows)} rows from portal")

        context.close()
        browser.close()

        metrics = {"tables_seen": total_tables, "rows": len(all_rows)}
        return all_rows, metrics
