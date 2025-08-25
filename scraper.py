# scraper.py
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

from playwright.sync_api import sync_playwright, Page, Locator

# ------------------------------
# URLs & constants
# ------------------------------
BASE = "https://parentportal.cajonvalley.net"
LOGIN_URL = BASE  # the root shows the PIN / Password / Login
PORTAL_HOME = f"{BASE}/Home/PortalMainPage"

PUBLIC_HOME_MARKERS = [
    "District Website",
    "Forget Your PIN?",
    "Reset Your Password",
    "How to Re-enroll Online",
    "How to make online payments",
    "Terms of Use",
]

ASSIGNMENT_TEXT_MARKERS = [
    "No Assignments Available",
    "No Current Assignments",
    "No Assignments",
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

# ------------------------------
# Small helpers
# ------------------------------
def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2500)
    except Exception:
        return ""


def _dismiss_timeout_if_present(page: Page) -> None:
    try:
        dlg = page.get_by_role("dialog")
        if dlg.is_visible():
            ok = dlg.get_by_role("button", name=re.compile(r"ok", re.I))
            if ok.is_visible():
                print("DEBUG — login DEBUG — dismissed timeout dialog")
                ok.click()
                page.wait_for_timeout(300)
    except Exception:
        pass


def _goto(page: Page, url: str, timeout: int = 20000, label: str | None = None) -> None:
    """Resilient navigation that tolerates client-side redirects (ERR_ABORTED)."""
    tag = label or url
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return
    except Exception as e1:
        print(
            f"DEBUG — goto({tag}) domcontentloaded aborted: {e1}. "
            f"Retrying with 'commit'."
        )
        try:
            page.goto(url, wait_until="commit", timeout=timeout)
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
            return
        except Exception as e2:
            print(f"DEBUG — goto({tag}) commit fallback also raised: {e2}. Continuing at {page.url}.")


def _first_visible(page: Page, selectors: List[str], timeout: int = 1500) -> Locator | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            pass
    return None


def _first_by_label(page: Page, labels: List[str], timeout: int = 1500) -> Locator | None:
    for label in labels:
        try:
            loc = page.get_by_label(re.compile(label, re.I)).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            pass
    return None


def ensure_logged_in_and_on_portal(page: Page, pin_value: str, password_value: str) -> None:
    """Open root login, submit, then land on the canonical portal home."""
    _goto(page, LOGIN_URL, label="LOGIN_URL")
    _dismiss_timeout_if_present(page)

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
            if cand.is_visible(timeout=900):
                login_btn = cand
                break
        except Exception:
            pass

    login_visible = bool(pin and pwd and login_btn)
    print(f"DEBUG — login fields visible: {login_visible}")
    if not login_visible:
        print("ERROR: Login form not found — cannot proceed.")
        return

    pin.fill(pin_value)
    pwd.fill(password_value)
    login_btn.click()
    page.wait_for_load_state("domcontentloaded", timeout=20000)
    _dismiss_timeout_if_present(page)

    _goto(page, PORTAL_HOME, label="PORTAL_HOME")
    _dismiss_timeout_if_present(page)
    print("DEBUG — after login: portal loaded")


def expand_header_if_collapsed(page: Page) -> None:
    """Open hamburger if needed so header controls are visible."""
    try:
        toggler = page.locator(
            'button.navbar-toggler, button[aria-label*="Toggle"], button[aria-expanded="false"]'
        ).first
        if toggler.is_visible(timeout=900):
            toggler.click()
            page.wait_for_timeout(200)
    except Exception:
        pass


def _click_home_or_assignments(page: Page) -> None:
    """Nudge UI to the Home/Assignments area in case we’re on a different tab."""
    expand_header_if_collapsed(page)

    # Try an explicit "Home" nav first
    for role in ("link", "button"):
        try:
            el = page.get_by_role(role, name=re.compile(r"^\s*home\s*$", re.I)).first
            if el.is_visible(timeout=700):
                el.click()
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                break
        except Exception:
            pass

    # If there’s an Assignments tab/link, try that too
    for role in ("tab", "link", "button"):
        try:
            el = page.get_by_role(role, name=re.compile(r"assignments?", re.I)).first
            if el.is_visible(timeout=700):
                el.click()
                page.wait_for_timeout(400)
                break
        except Exception:
            pass


def switch_to_student(page: Page, student_name: str) -> bool:
    """
    Switch to a student via picker or header link; then ensure we’re on Home.
    """
    expand_header_if_collapsed(page)

    picker_candidates = [
        "#divStudentBanner",
        "#ddlStudent",
        "#studentPicker",
        '[id*="Student"][role="button"]',
    ]

    # Try a dropdown/picker
    for sel in picker_candidates:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=800):
                el.click()
                # menu option or listbox option
                try:
                    page.get_by_role("menuitem", name=re.compile(student_name, re.I)).first.click(timeout=2500)
                    print(f"DEBUG — switched via menu to student '{student_name}'")
                    _goto(page, PORTAL_HOME, label="PORTAL_HOME(after switch)")
                    return True
                except Exception:
                    pass
                try:
                    page.get_by_role("option", name=re.compile(student_name, re.I)).first.click(timeout=2500)
                    print(f"DEBUG — switched via option to student '{student_name}'")
                    _goto(page, PORTAL_HOME, label="PORTAL_HOME(after switch)")
                    return True
                except Exception:
                    pass
        except Exception:
            pass

    # Fallback: directly click the student’s name if it’s a header link
    try:
        link = page.get_by_text(re.compile(rf"\b{re.escape(student_name)}\b", re.I)).first
        if link.is_visible(timeout=900):
            link.click()
            print(f"DEBUG — switched via header text to student '{student_name}'")
            _goto(page, PORTAL_HOME, label="PORTAL_HOME(after switch)")
            return True
    except Exception:
        pass

    print(f"DEBUG — could not locate a picker for '{student_name}'; skipping switch")
    return False


def _any_assignment_table_visible(page: Page, timeout: int) -> bool:
    """Check a handful of patterns for the assignment tables or empty-state text."""
    # Known tables
    selectors = [
        'table[id^="tblAssign_"]',
        "#SP_Assignments table.tblassign",
        "table.tblassign",
    ]
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=timeout):
                return True
        except Exception:
            pass

    # Empty-state texts
    for txt in ASSIGNMENT_TEXT_MARKERS:
        try:
            if page.locator(f'text="{txt}"').first.is_visible(timeout=timeout):
                return True
        except Exception:
            pass

    return False


def ensure_assignments_ready(page: Page) -> bool:
    """
    Make sure the assignments panel is on-screen and loaded.
    We nudge the UI, scroll to trigger lazy-loads, and poll.
    """
    if "PortalMainPage" not in page.url:
        _goto(page, PORTAL_HOME, label="ensure_assignments_ready->home")

    # Up to ~20s total, with nudges in between
    for attempt in range(1, 5):
        # Try fast path first
        if _any_assignment_table_visible(page, timeout=2000):
            return True

        # Nudge the UI (Home / Assignments) and scroll to trigger lazy loads
        _click_home_or_assignments(page)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(400)
        page.evaluate("window.scrollTo(0, 0)")

        # After the nudge, give it a longer wait
        if _any_assignment_table_visible(page, timeout=5000):
            return True

    return False


# ------------------------------
# Parsing
# ------------------------------
@dataclass
class ClassContext:
    period: str
    course: str
    teacher: str


def _extract_class_context_for_table(page: Page, table: Locator) -> ClassContext:
    header_text = ""
    try:
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
        try:
            header = table.locator("xpath=preceding::h3[1] | preceding::h4[1]").first
            if header.is_visible():
                header_text = header.inner_text().strip()
        except Exception:
            pass

    period = ""
    course = header_text.strip()
    teacher = ""

    m = re.search(r"Period\s*([0-9A-Za-z]+)", header_text, re.I)
    if m:
        period = m.group(1)
    else:
        m2 = re.match(r"^\s*([0-9A-Za-z]{1,3})\s+", header_text)
        if m2:
            period = m2.group(1)

    m = re.search(r"Teacher[:\s]*([A-Za-z .,'-]+)", header_text, re.I)
    if m:
        teacher = m.group(1).strip()
    else:
        m2 = re.search(r"\(([A-Za-z .,'-]{2,})\)\s*$", header_text)
        if m2:
            teacher = m2.group(1).strip()

    return ClassContext(period=period, course=course, teacher=teacher)


def _parse_table_rows(page: Page, table: Locator, student: str) -> List[List[str]]:
    ctx = _extract_class_context_for_table(page, table)

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
        if any("No Assignments" in x for x in tds):
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
    if not switch_to_student(page, student):
        return [], 0

    if not ensure_assignments_ready(page):
        print("DEBUG — assignments view not ready; continuing")
        return [], 0

    tables = page.locator('table[id^="tblAssign_"]')
    ids = []
    try:
        for el in tables.all():
            ids.append(el.get_attribute("id") or "")
    except Exception:
        pass
    if ids:
        print(f"DEBUG — found assignment tables (ids): {ids}")

    rows: List[List[str]] = []
    for tbl in tables.all():
        rows.extend(_parse_table_rows(page, tbl, student))

    return rows, len(ids)


# ------------------------------
# Top-level runner used by main.py
# ------------------------------
def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[List[str]], Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
        context = browser.new_context()
        context.set_default_timeout(20000)
        page = context.new_page()

        _goto(page, LOGIN_URL, label="LOGIN_URL(start)")
        print(f"DEBUG — landed: {page.url}")

        ensure_logged_in_and_on_portal(page, username, password)

        print(f"DEBUG — UI SNAPSHOT — url: {page.url}")
        sample = []
        bt = " ".join(_body_text(page).split())
        for token in ["Home", "FAQs", "District Website", "Terms of Use"]:
            if token in bt:
                sample.append(token)
        print(f"DEBUG — UI SNAPSHOT — sample: {', '.join(sample) if sample else '(none)'}")

        all_rows: List[List[str]] = []
        table_total = 0

        for s in students:
            stu_rows, tbl_count = extract_assignments_for_student(page, s)
            print(f"DEBUG — class tables for {s}: {tbl_count}")
            table_total += tbl_count
            all_rows.extend(stu_rows)

        print(f"DEBUG — scraped {len(all_rows)} rows from portal")

        context.close()
        browser.close()

        metrics = {"tables_seen": table_total, "rows": len(all_rows)}
        return all_rows, metrics
