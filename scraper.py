# scraper.py
from __future__ import annotations

import re
import time
from typing import Dict, Iterable, List, Tuple

from playwright.sync_api import Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# ---- URLs & simple markers -------------------------------------------------

PORTAL_HOME = "https://parentportal.cajonvalley.net/Home/PortalMainPage"
LOGON_URL = (
    "https://parentportal.cajonvalley.net/Account/LogOn?ReturnUrl=%2FHome%2FPortalMainPage"
)


# ---- Debug helper -----------------------------------------------------------

def dbg(msg: str):
    print(f"DEBUG — {msg}")


# ---- Login flow -------------------------------------------------------------

def _go_to_login(page):
    # Go straight to the true login endpoint (skips the timeout screen)
    page.goto(LOGON_URL, wait_until="domcontentloaded")


def _wait_for_login_fields(page, timeout_ms=15000) -> bool:
    try:
        page.wait_for_selector("#UserName", timeout=timeout_ms)
        page.wait_for_selector("#Password", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def _dismiss_timeout_if_present(page) -> bool:
    """
    Detects and clears 'Your Session Has Timed Out... Click OK to Continue'.
    Returns True if we found/handled it.
    """
    try:
        body_text = page.locator("body").inner_text(timeout=2500)
    except Exception:
        body_text = ""
    if "Session Has Timed Out" not in body_text:
        return False

    # Try common "OK" buttons or alerts
    for sel in [
        'button:has-text("OK")',
        'text="OK to Continue"',
        'input[type=button][value="OK"]',
        'a:has-text("OK")',
        '[role="button"]:has-text("OK")',
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                loc.click()
                break
        except Exception:
            pass

    # Some portals use JS alert()
    def _once_dialog(d):
        try:
            d.accept()
        except Exception:
            pass
    try:
        page.once("dialog", _once_dialog)
    except Exception:
        pass

    # Force navigation to real login
    _go_to_login(page)
    return True


def login(page, username: str, password: str):
    """
    Robust login routine:
      - Always navigate to /Account/LogOn
      - Dismiss timeout interstitial if needed
      - Fill credentials & submit
      - Wait until we're effectively at the portal home
    """
    try:
        page.context.clear_cookies()
    except Exception:
        pass

    _go_to_login(page)

    login_visible = _wait_for_login_fields(page, timeout_ms=8000)
    if not login_visible:
        handled = _dismiss_timeout_if_present(page)
        if handled:
            login_visible = _wait_for_login_fields(page, timeout_ms=12000)

    dbg(f"login fields visible: {login_visible}")

    if not login_visible:
        # Last try: direct /Account/LogOn without ReturnUrl
        page.goto("https://parentportal.cajonvalley.net/Account/LogOn", wait_until="domcontentloaded")
        if not _wait_for_login_fields(page, timeout_ms=12000):
            raise RuntimeError("Login form not found — cannot proceed.")

    # Fill & submit
    page.fill("#UserName", username)
    page.fill("#Password", password)

    clicked = False
    for sel in [
        'button[type=submit]',
        'input[type=submit]',
        'button:has-text("Log On")',
        'button:has-text("Login")',
        'input[type=button][value="Log On"]',
        'input[type=button][value="Login"]',
    ]:
        try:
            page.locator(sel).first.click()
            clicked = True
            break
        except Exception:
            pass
    if not clicked:
        raise RuntimeError("Could not locate the login submit button.")

    # Wait for home
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass

        cur = page.url
        if (
            cur.startswith(PORTAL_HOME)
            or re.search(r"/Home/PortalMainPage", cur, re.I)
        ):
            break

        # Also accept certain visible markers as success
        for sel in ["#divStudentBanner", "#dvStudentBanner", "#divStudentInfo", "text=Student"]:
            try:
                if page.locator(sel).first.is_visible():
                    return
            except Exception:
                pass
        time.sleep(0.4)

    dbg(f"after login: portal loaded")


# ---- Student switching ------------------------------------------------------

def _casefold(s: str) -> str:
    return (s or "").casefold()


def _banner_text(page) -> str:
    for sel in ["#divStudentBanner", "#dvStudentBanner", "#divStudentInfo", "header", "body"]:
        try:
            if page.locator(sel).first.is_visible():
                return page.locator(sel).first.inner_text(timeout=1000)
        except Exception:
            pass
    return ""


def switch_to_student(page, student: str) -> bool:
    """
    Try several strategies to select the target student:
      1) Click a tile/card with the student's name.
      2) Use a dropdown/combobox (ddlStudent, etc.)
      3) Fallback to any link that matches the name.
    Returns True if we believe we switched successfully.
    """
    name_cf = _casefold(student)

    # Make sure we’re on the home page
    try:
        page.goto(PORTAL_HOME, wait_until="domcontentloaded")
    except Exception:
        pass

    # Strategy 1: tiles/cards
    tile_selectors = [
        ".studentTile a",
        ".studentTile",
        "a.card, .card a, .tile a, .tile",
        "a:has-text('%s')" % student,
        "div:has-text('%s')" % student,
    ]
    for sel in tile_selectors:
        try:
            locs = page.locator(sel)
            count = locs.count()
            if count == 0:
                continue
            for i in range(min(count, 10)):
                el = locs.nth(i)
                try:
                    t = el.inner_text(timeout=800)
                except Exception:
                    t = ""
                if name_cf in _casefold(t):
                    el.click()
                    # Wait for banner/top area to reflect name
                    deadline = time.time() + 12
                    while time.time() < deadline:
                        bt = _banner_text(page)
                        if name_cf and name_cf in _casefold(bt):
                            dbg(f"switched to student {student}")
                            return True
                        time.sleep(0.25)
        except Exception:
            pass

    # Strategy 2: dropdown
    for sel in [
        "select#ddlStudent",
        "select[name*=Student]",
        "select:has(option)",
        "[role=combobox]",
    ]:
        try:
            el = page.locator(sel).first
            if not el or not el.is_visible():
                continue
            # Try selecting by label
            try:
                el.select_option(label=re.compile(student, re.I))
            except Exception:
                # Try by value if labels didn’t match
                try:
                    options = el.locator("option")
                    n = options.count()
                    for i in range(n):
                        txt = options.nth(i).inner_text(timeout=600)
                        val = options.nth(i).get_attribute("value") or ""
                        if name_cf in _casefold(txt) or name_cf in _casefold(val):
                            el.select_option(value=val)
                            break
                except Exception:
                    pass
            # Confirm banner
            deadline = time.time() + 10
            while time.time() < deadline:
                bt = _banner_text(page)
                if name_cf in _casefold(bt):
                    dbg(f"switched to student {student}")
                    return True
                time.sleep(0.2)
        except Exception:
            pass

    # Strategy 3: any obvious link with the name
    try:
        link = page.locator(f"a:has-text('{student}')").first
        if link and link.is_visible():
            link.click()
            deadline = time.time() + 10
            while time.time() < deadline:
                bt = _banner_text(page)
                if name_cf in _casefold(bt):
                    dbg(f"switched to student {student}")
                    return True
                time.sleep(0.2)
    except Exception:
        pass

    return False


# ---- Assignment scraping ----------------------------------------------------

def ensure_assignments_ready(page) -> Tuple[bool, int]:
    """
    Wait until assignment tables appear (or a 'No Assignments' message).
    Returns (ok, table_count)
    """
    # Look for either tables or the 'No Assignments Available' message
    table_count = 0
    try:
        page.wait_for_selector('table[id^="tblAssign_"]', timeout=10000)
        table_count = page.locator('table[id^="tblAssign_"]').count()
    except PlaywrightTimeoutError:
        # Maybe there are no assignments; check message
        try:
            msg = page.locator("body").inner_text(timeout=1000)
        except Exception:
            msg = ""
        if "No Assignments Available" in msg:
            return True, 0
        return False, 0

    return True, table_count


def _read_headers(table) -> List[str]:
    headers = []
    try:
        headers = [h.strip() for h in table.locator("thead tr th").all_text_contents()]
    except Exception:
        pass
    if not headers:
        try:
            headers = [h.strip() for h in table.locator("tr").first.locator("th,td").all_text_contents()]
        except Exception:
            headers = []
    return headers


# Map various possible header spellings to our normalized column names
HEADER_MAP = {
    "period": "Period",
    "course": "Course",
    "section": "Course",
    "teacher": "Teacher",
    "due": "DueDate",
    "due date": "DueDate",
    "assigned": "AssignedDate",
    "assigned date": "AssignedDate",
    "assignment": "Assignment",
    "points possible": "PtsPossible",
    "pts possible": "PtsPossible",
    "points": "PtsPossible",
    "score": "Score",
    "percent": "Pct",
    "%": "Pct",
    "status": "Status",
    "comments": "Comments",
}

OUR_COLS = [
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


def _normalize_headers(raw_headers: Iterable[str]) -> Dict[int, str]:
    """
    Given the raw header texts, return a mapping of column index -> our normalized name.
    """
    mapping: Dict[int, str] = {}
    for idx, h in enumerate(raw_headers):
        key = _casefold(re.sub(r"\s+", " ", h.strip()))
        norm = HEADER_MAP.get(key)
        if not norm:
            # try prefix contains logic
            for k, v in HEADER_MAP.items():
                if k in key:
                    norm = v
                    break
        if norm:
            mapping[idx] = norm
    return mapping


def extract_assignments_for_student(page, student: str) -> Tuple[List[List[str]], int]:
    """
    Returns (rows, table_count) where rows are lists in OUR_COLS order.
    """
    ok, table_count = ensure_assignments_ready(page)
    if not ok:
        return [], 0

    rows: List[List[str]] = []
    tables = page.locator('table[id^="tblAssign_"]')
    ids = []
    try:
        n = tables.count()
        for i in range(n):
            try:
                ids.append(tables.nth(i).get_attribute("id") or "")
            except Exception:
                pass
    except Exception:
        pass

    if ids:
        dbg(f"found assignment tables (ids): {ids}")

    n_tables = tables.count()
    for t_idx in range(n_tables):
        table = tables.nth(t_idx)
        headers = _read_headers(table)
        colmap = _normalize_headers(headers)

        trs = table.locator("tbody tr")
        rcount = trs.count()
        for r in range(rcount):
            tds = trs.nth(r).locator("td")
            d = {col: "" for col in OUR_COLS}
            d["Student"] = student
            d["SourceURL"] = page.url

            # Pull cells
            tcount = tds.count()
            for c in range(tcount):
                text = ""
                try:
                    text = tds.nth(c).inner_text(timeout=1000).strip()
                except Exception:
                    pass
                norm = colmap.get(c)
                if norm:
                    d[norm] = text

            # Compact → list
            rows.append([d[k] for k in OUR_COLS])

    return rows, table_count


# ---- Orchestrator -----------------------------------------------------------

def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[List[str]], Dict[str, int]]:
    """
    High-level orchestration:
      - launches browser
      - logs in
      - loops students, switches, extracts rows
    Returns (rows, metrics)
    """
    all_rows: List[List[str]] = []
    metrics = {"tables_total": 0, "students_processed": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        # Land somewhere “home-ish” first (useful for logs)
        page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        dbg(f"landed: {page.url}")

        # Login
        login(page, username, password)

        # Main loop
        for s in students:
            # Try to ensure we’re at home for each loop
            try:
                page.goto(PORTAL_HOME, wait_until="domcontentloaded")
            except Exception:
                pass

            # Switch student
            ok = switch_to_student(page, s)
            if not ok:
                dbg(f"could not locate a picker for '{s}'; skipping switch")
                dbg(f"skipping extraction for '{s}' (could not switch)")
                continue

            # Extract
            rows, tbl_count = extract_assignments_for_student(page, s)
            dbg(f"class tables for {s}: {tbl_count}")
            all_rows.extend(rows)
            metrics["tables_total"] += tbl_count
            metrics["students_processed"] += 1

        browser.close()

    dbg(f"scraped {len(all_rows)} rows from portal")
    return all_rows, metrics
