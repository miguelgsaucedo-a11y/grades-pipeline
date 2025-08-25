# scraper.py
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

from playwright.sync_api import sync_playwright, Page, Locator

BASE = "https://parentportal.cajonvalley.net"
LOGIN_URL = BASE
PORTAL_HOME = f"{BASE}/Home/PortalMainPage"

ASSIGNMENT_TEXT_MARKERS = [
    "No Assignments Available",
    "No Current Assignments",
    "No Assignments",
]

HEADERS = [
    "ImportedAt","Student","Period","Course","Teacher","DueDate","AssignedDate",
    "Assignment","PtsPossible","Score","Pct","Status","Comments","SourceURL",
]

def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2000)
    except Exception:
        return ""

def _dismiss_timeout_if_present(page: Page) -> None:
    try:
        dlg = page.get_by_role("dialog").first
        if dlg.is_visible(timeout=800):
            ok = dlg.get_by_role("button", name=re.compile(r"ok", re.I)).first
            if ok.is_visible(timeout=400):
                print("DEBUG — login DEBUG — dismissed timeout dialog")
                ok.click()
                page.wait_for_timeout(250)
    except Exception:
        pass

def _goto(page: Page, url: str, timeout: int = 20000, label: str | None = None) -> None:
    tag = label or url
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return
    except Exception as e1:
        print(f"DEBUG — goto({tag}) domcontentloaded aborted: {e1}. Retrying with 'commit'.")
        try:
            page.goto(url, wait_until="commit", timeout=timeout)
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception as e2:
            print(f"DEBUG — goto({tag}) commit fallback also raised: {e2}. Continuing at {page.url}.")

def _first_visible(page: Page, selectors: List[str], timeout: int = 1200) -> Locator | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            pass
    return None

def _by_label(page: Page, labels: List[str], timeout: int = 1200) -> Locator | None:
    for label in labels:
        try:
            loc = page.get_by_label(re.compile(label, re.I)).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            pass
    return None

def ensure_logged_in_and_on_portal(page: Page, pin_value: str, password_value: str) -> None:
    _goto(page, LOGIN_URL, label="LOGIN_URL")
    _dismiss_timeout_if_present(page)

    pin = _by_label(page, [r"\bPIN\b"]) or _first_visible(
        page, ['input[name*="pin" i]','input[id*="pin" i]','input[type="text"]']
    )
    pwd = _by_label(page, [r"password"]) or _first_visible(
        page, ['input[type="password"]','input[name*="pass" i]','input[id*="pass" i]']
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

    visible = bool(pin and pwd and login_btn)
    print(f"DEBUG — login fields visible: {visible}")
    if not visible:
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
    try:
        toggler = page.locator(
            'button.navbar-toggler, button[aria-label*="Toggle"], button[aria-expanded="false"]'
        ).first
        if toggler.is_visible(timeout=700):
            toggler.click()
            page.wait_for_timeout(200)
    except Exception:
        pass

def _click_nav_by_name(page: Page, names: List[str]) -> bool:
    for nm in names:
        for role in ("link","button","tab"):
            try:
                el = page.get_by_role(role, name=re.compile(nm, re.I)).first
                if el.is_visible(timeout=600):
                    el.click()
                    page.wait_for_timeout(350)
                    return True
            except Exception:
                pass
    return False

def _nudge_assignments_ui(page: Page) -> None:
    expand_header_if_collapsed(page)
    # Try to land on Home then Assignments/Classes, if such controls exist.
    _click_nav_by_name(page, [r"^\s*home\s*$"])
    _click_nav_by_name(page, [r"assignments?", r"classes?"])
    # Scroll to trigger lazy loads
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(400)
    page.evaluate("window.scrollTo(0, 0)")

    # Try to expand any collapsed panels that might contain assignments
    try:
        for tog in page.locator('[aria-expanded="false"], [data-toggle="collapse"]').all():
            try:
                if tog.is_visible():
                    tog.click()
                    page.wait_for_timeout(200)
            except Exception:
                pass
    except Exception:
        pass

    # If Assignments anchor exists, scroll it into view
    try:
        page.evaluate("""
            const el = document.querySelector('#SP_Assignments');
            if (el) el.scrollIntoView({behavior:'instant', block:'center'});
        """)
        page.wait_for_timeout(200)
    except Exception:
        pass

def _count_assignment_tables(page: Page, visible_only: bool) -> int:
    # Count tables whether or not they’re visible; visibility is optional.
    sels = ['table[id^="tblAssign_"]', '#SP_Assignments table.tblassign', 'table.tblassign']
    total = 0
    for sel in sels:
        try:
            loc = page.locator(sel)
            if visible_only:
                # Count only those that are visible
                total += sum(1 for _ in loc.filter(":visible").all())
            else:
                total += loc.count()
        except Exception:
            pass
    # Also accept known empty-state texts as a “ready” signal
    if total == 0:
        for txt in ASSIGNMENT_TEXT_MARKERS:
            try:
                if page.locator(f"text={txt}").first.count() > 0:
                    return 0  # ready but no rows
            except Exception:
                pass
    return total

def ensure_assignments_ready(page: Page) -> bool:
    if "PortalMainPage" not in page.url:
        _goto(page, PORTAL_HOME, label="ensure_assignments_ready->home")

    # Try up to ~25s with nudges between checks.
    for attempt in range(1, 6):
        c_any = _count_assignment_tables(page, visible_only=False)
        c_vis = _count_assignment_tables(page, visible_only=True)
        print(f"DEBUG — check {attempt}: tables present={c_any}, visible={c_vis}")
        if c_any > 0 or c_vis > 0:
            return True
        _nudge_assignments_ui(page)
    return False

def switch_to_student(page: Page, student_name: str) -> bool:
    expand_header_if_collapsed(page)
    # Dropdown/picker attempts
    for sel in ["#divStudentBanner","#ddlStudent","#studentPicker",'[id*="Student"][role="button"]']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=800):
                el.click()
                for role in ("menuitem","option"):
                    try:
                        page.get_by_role(role, name=re.compile(student_name, re.I)).first.click(timeout=2500)
                        print(f"DEBUG — switched via {role} to student '{student_name}'")
                        _goto(page, PORTAL_HOME, label="PORTAL_HOME(after switch)")
                        return True
                    except Exception:
                        pass
        except Exception:
            pass
    # Header text fallback
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

# ---------------- Parsing ----------------
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

    out: List[List[str]] = []
    for tr in table.locator("tbody tr").all():
        tds = [c.strip() for c in tr.locator("td").all_inner_texts()]
        if not tds:
            continue
        if any("No Assignments" in x for x in tds):
            continue

        def pick(i: int | None) -> str:
            return tds[i] if (i is not None and i < len(tds)) else ""

        out.append([
            now_stamp(),          # ImportedAt
            student,              # Student
            ctx.period,           # Period
            ctx.course,           # Course
            ctx.teacher,          # Teacher
            pick(i_due),          # DueDate
            pick(i_assigned),     # AssignedDate
            pick(i_assgn),        # Assignment
            pick(i_pts),          # PtsPossible
            pick(i_score),        # Score
            pick(i_pct),          # Pct
            pick(i_status),       # Status
            pick(i_comments),     # Comments
            page.url,             # SourceURL
        ])
    return out

def extract_assignments_for_student(page: Page, student: str) -> Tuple[List[List[str]], int]:
    if not switch_to_student(page, student):
        return [], 0

    # Multi-phase readiness attempts
    if not ensure_assignments_ready(page):
        print("DEBUG — assignments view not ready; continuing")
        return [], 0

    # Count before parsing (present vs visible)
    present = _count_assignment_tables(page, visible_only=False)
    visible = _count_assignment_tables(page, visible_only=True)
    print(f"DEBUG — assignments tables found (present={present}, visible={visible})")

    tables = page.locator('table[id^="tblAssign_"], #SP_Assignments table.tblassign, table.tblassign')
    rows: List[List[str]] = []
    for tbl in tables.all():
        rows.extend(_parse_table_rows(page, tbl, student))

    return rows, present

def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[List[str]], Dict]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-gpu","--no-sandbox"])
        context = browser.new_context()
        context.set_default_timeout(20000)
        page = context.new_page()

        _goto(page, LOGIN_URL, label="LOGIN_URL(start)")
        print(f"DEBUG — landed: {page.url}")

        ensure_logged_in_and_on_portal(page, username, password)

        print(f"DEBUG — UI SNAPSHOT — url: {page.url}")
        sample = []
        bt = " ".join(_body_text(page).split())
        for token in ["Home","FAQs","District Website","Terms of Use","Assignments","Classes"]:
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
        context.close(); browser.close()
        metrics = {"tables_seen": table_total, "rows": len(all_rows)}
        return all_rows, metrics
