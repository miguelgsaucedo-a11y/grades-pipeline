# scraper.py
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from typing import List, Dict, Tuple
import re

PORTAL_ROOT = "https://parentportal.cajonvalley.net"
PORTAL_HOME = f"{PORTAL_ROOT}/Home/PortalMainPage"

# ---------------------------
# small logging helpers
# ---------------------------
def _txt_sample(page, max_len=220) -> str:
    try:
        body = page.locator("body")
        if body.count():
            t = body.inner_text(timeout=2000)
            return re.sub(r"\s+", " ", t).strip()[:max_len]
    except Exception:
        pass
    return ""

def _log(page, msg: str):
    print(f"DEBUG — {msg}")

# ---------------------------
# navigation
# ---------------------------
def _goto(page, url: str, wait="domcontentloaded", timeout=20000):
    page.goto(url, wait_until=wait, timeout=timeout)

def _dismiss_any_timeout_dialog(page):
    # Seen on this portal from time to time
    try:
        btn = page.get_by_role("button", name=re.compile(r"(OK|Continue|Close)", re.I))
        if btn.count() and btn.first.is_visible():
            btn.first.click()
    except Exception:
        pass

# ---------------------------
# login helpers
# ---------------------------
PIN_CANDIDATES = [
    'input#PIN',
    'input[name="PIN"]',
    'input[name*="PIN" i]',
    'input[placeholder*="PIN" i]',
    '//label[normalize-space()="PIN"]/following::input[1]',
]
PWD_CANDIDATES = [
    'input#Password',
    'input[name="Password"]',
    'input[name*="Password" i]',
    'input[type="password"]',
    '//label[normalize-space()="Password"]/following::input[1]',
]

def _first_visible(page, selectors: List[str], wait_each=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                try:
                    loc.first.wait_for(state="visible", timeout=wait_each)
                except Exception:
                    # still try to use it if attached
                    pass
                if loc.first.is_visible():
                    return loc.first
        except Exception:
            continue
    return None

def _get_login_fields(page) -> Tuple[object, object]:
    """
    Returns (pin_field, password_field) locators or (None, None).
    Also supports a heuristic: find a password field, then pick the nearest
    previous text input as the PIN field if direct selectors fail.
    """
    pwd = _first_visible(page, PWD_CANDIDATES, wait_each=2000)
    pin = _first_visible(page, PIN_CANDIDATES, wait_each=2000)

    if not pin and pwd:
        # Heuristic: PIN is often the text input just before the password
        try:
            text_inputs = page.locator('input[type="text"], input:not([type])')
            if text_inputs.count():
                # take the first visible one on the page
                for i in range(text_inputs.count()):
                    cand = text_inputs.nth(i)
                    if cand.is_visible():
                        pin = cand
                        break
        except Exception:
            pass

    return (pin, pwd) if (pin and pwd) else (None, None)

def _open_login_form(page):
    # Always reset to root
    _goto(page, PORTAL_ROOT, wait="domcontentloaded", timeout=20000)
    _dismiss_any_timeout_dialog(page)

    # Try explicit login paths (both variants that appear in the logs)
    for path in ("/Account/LogOn", "/Account/Login"):
        try:
            _goto(page, PORTAL_ROOT + path, wait="domcontentloaded", timeout=20000)
            pin, pwd = _get_login_fields(page)
            if pin and pwd:
                return (pin, pwd)
        except Exception:
            pass

    # Try clicking any visible “Login / Log On / Sign In” link or button
    try:
        link = page.get_by_role("link", name=re.compile(r"log\s*(on|in)|sign\s*in", re.I))
        if link.count():
            link.first.click()
            page.wait_for_load_state("domcontentloaded")
            pin, pwd = _get_login_fields(page)
            if pin and pwd:
                return (pin, pwd)
    except Exception:
        pass

    # Last resort – hunt in-place
    pin, pwd = _get_login_fields(page)
    return (pin, pwd)

def _ensure_logged_in(page, username: str, password: str, timeout=25000):
    pin, pwd = _open_login_form(page)

    _log(page, f"LOGIN DEBUG — url now: {page.url}")
    _log(page, f"LOGIN DEBUG — body sample: {_txt_sample(page)}")

    if not (pin and pwd):
        raise RuntimeError("Could not find login form")

    try:
        pin.fill(username, timeout=12000)
        pwd.fill(password, timeout=12000)
    except Exception:
        raise RuntimeError("Could not fill login form")

    # Try to submit
    try:
        page.get_by_role("button", name=re.compile(r"log\s*(in|on)|sign\s*in", re.I)).first.click()
    except Exception:
        try:
            pwd.press("Enter")
        except Exception:
            pass

    # Head to the main page (and handle quick abort by retrying once)
    try:
        _goto(page, PORTAL_HOME, wait="domcontentloaded", timeout=timeout)
    except Exception:
        page.wait_for_timeout(600)
        _goto(page, PORTAL_HOME, wait="domcontentloaded", timeout=timeout)

def _try_open_student_menu(page):
    for sel in [
        '#divStudentBanner',
        'button[aria-controls*="Student"]',
        'button:has-text("Student")',
        'a:has-text("Student")',
        'a:has-text("Change Student")',
    ]:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                page.wait_for_timeout(100)
                return True
        except Exception:
            pass
    return False

def _switch_to_student(page, name: str) -> bool:
    # direct click on link
    name_xpath = f'//a[contains(normalize-space(.), "{name}") or contains(@title, "{name}")]'
    try:
        links = page.locator(name_xpath)
        if links.count() and links.first.is_visible():
            links.first.click()
            page.wait_for_load_state("domcontentloaded")
            return True
    except Exception:
        pass

    # menu then click
    _try_open_student_menu(page)
    try:
        links = page.locator(name_xpath)
        if links.count() and links.first.is_visible():
            links.first.click()
            page.wait_for_load_state("domcontentloaded")
            return True
    except Exception:
        pass

    # some portals render as buttons
    btn_xpath = f'//button[contains(normalize-space(.), "{name}")]'
    try:
        btns = page.locator(btn_xpath)
        if btns.count() and btns.first.is_visible():
            btns.first.click()
            page.wait_for_load_state("domcontentloaded")
            return True
    except Exception:
        pass

    return False

def _extract_tables_for_current_student(page) -> List[Dict]:
    rows: List[Dict] = []

    tables = page.locator('table.tblassign, table[id^="tblAssign_"]')
    count = tables.count()
    if count == 0:
        # staged waits to allow late rendering
        for _ in range(6):
            page.wait_for_timeout(250)
            count = page.locator('table.tblassign, table[id^="tblAssign_"]').count()
            if count:
                break

    for i in range(count):
        table = tables.nth(i)

        # derive course from nearest header above table
        course = ""
        try:
            hdr = table.locator("xpath=preceding::*[self::h1 or self::h2 or self::h3 or self::h4][1]")
            if hdr.count():
                course = re.sub(r"\s+", " ", hdr.first.inner_text().strip())
                if re.search(r"Assignments?\s+Show\s+All", course, re.I):
                    course = ""
        except Exception:
            pass

        try:
            body_rows = table.locator("tbody tr")
            for r in range(body_rows.count()):
                tr = body_rows.nth(r)
                tds = tr.locator("td")
                c = tds.count()
                if c == 0:
                    continue
                cells = [re.sub(r"\s+", " ", (tds.nth(k).inner_text() or "").strip()) for k in range(c)]

                def cell(idx, default=""):
                    return cells[idx] if idx < len(cells) else default

                assignment = cell(0)
                due_date = cell(1)
                assigned_date = cell(2)

                # layout variant: when cell(1) is numeric, it's Pos
                if re.fullmatch(r"\d+(\.\d+)?", due_date):
                    assigned_date = ""
                    pts_possible = cell(1)
                    score = cell(2)
                    pct = cell(3)
                else:
                    pts_possible = cell(3)
                    score = cell(4)
                    pct = cell(5)

                status = cell(6, "")
                comments = cell(7, "")

                if not status:
                    if re.search(r"missing", assignment, re.I):
                        status = "MISSING"
                    elif pct and re.fullmatch(r"100%?", pct):
                        status = "OK"

                rows.append({
                    "Period": "",
                    "Course": course,
                    "Teacher": "",
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

    # try to populate Teacher from headers on the page
    try:
        heads = page.locator("xpath=//h1|//h2|//h3|//h4")
        if heads.count():
            joined = " ".join([re.sub(r"\s+", " ", heads.nth(i).inner_text().strip())
                               for i in range(min(4, heads.count()))])
            m = re.search(r"([A-Z][a-z]+,\s*[A-Z](?:\.)?)", joined)
            teacher = m.group(1) if m else ""
            for r in rows:
                if not r["Teacher"]:
                    r["Teacher"] = teacher
    except Exception:
        pass

    return rows

def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[Dict], Dict]:
    scraped: List[Dict] = []
    metrics = {"students": students, "per_student_table_counts": {}, "ui_url": "", "ui_sample": ""}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()

        _ensure_logged_in(page, username, password, timeout=30000)
        metrics["ui_url"] = page.url
        metrics["ui_sample"] = _txt_sample(page)

        for s in students:
            ok = _switch_to_student(page, s)
            if not ok:
                _log(page, f"could not locate a picker for '{s}'; skipping switch")
                metrics["per_student_table_counts"][s] = 0
                continue

            tables_rows = _extract_tables_for_current_student(page)
            metrics["per_student_table_counts"][s] = len(tables_rows)
            for r in tables_rows:
                r["Student"] = s
            scraped.extend(tables_rows)

        browser.close()

    return scraped, metrics
