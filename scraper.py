# scraper.py
from __future__ import annotations

import re
import time
from typing import Dict, Iterable, List, Tuple

from playwright.sync_api import Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


PORTAL_BASE = "https://parentportal.cajonvalley.net"
PORTAL_HOME = f"{PORTAL_BASE}/Home/PortalMainPage"
LOGON_URL = f"{PORTAL_BASE}/Account/LogOn?ReturnUrl=%2FHome%2FPortalMainPage"
LOGOFF_URL = f"{PORTAL_BASE}/Account/LogOff"


def dbg(msg: str) -> None:
    print(f"DEBUG — {msg}")


# ---------- login helpers ----------------------------------------------------

def _body_text(page, timeout=1500) -> str:
    try:
        return page.locator("body").inner_text(timeout=timeout)
    except Exception:
        return ""


def _go_to_login(page) -> None:
    page.goto(LOGON_URL, wait_until="domcontentloaded")


def _wait_for_any_login_fields(page, timeout_ms=12000) -> bool:
    """Be flexible: accept several possible selectors for the two fields."""
    candidates = [
        "#UserName",
        "input[name=UserName]",
        "input[id*=User][type=text]",
        "input[type=text][name*=user i]",
        "input[type=text][id*=user i]",
    ]
    pw_candidates = [
        "#Password",
        "input[name=Password]",
        "input[type=password]",
        "input[id*=pass i]",
        "input[name*=pass i]",
    ]
    try:
        page.wait_for_selector(" , ".join(candidates), timeout=timeout_ms)
        page.wait_for_selector(" , ".join(pw_candidates), timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def _dismiss_timeout_if_present(page) -> bool:
    """Clear 'Your Session Has Timed Out… Click OK to Continue' if it shows up."""
    body = _body_text(page)
    if "Session Has Timed Out" not in body and "OK to Continue" not in body:
        return False

    for sel in [
        'button:has-text("OK")',
        'text="OK to Continue"',
        'input[type=button][value="OK"]',
        'a:has-text("OK")',
        '[role="button"]:has-text("OK")',
    ]:
        try:
            el = page.locator(sel).first
            if el and el.is_visible():
                el.click()
                break
        except Exception:
            pass

    # Some versions use JS alerts
    try:
        page.once("dialog", lambda d: d.accept())
    except Exception:
        pass

    # Ensure we go to the actual logon form
    _go_to_login(page)
    return True


def login(page, username: str, password: str) -> None:
    """
    Very defensive login:
      • Force a logoff
      • Go to /Account/LogOn
      • Dismiss timeout interstitials
      • Fall back to generic selectors if specific IDs aren't found
    """
    # Hard reset server-side session
    try:
        page.goto(LOGOFF_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(300)
    except Exception:
        pass

    _go_to_login(page)

    # If we landed on a timeout screen, clear it
    _dismiss_timeout_if_present(page)

    # Try a couple of times to find the fields
    login_visible = _wait_for_any_login_fields(page, timeout_ms=9000)
    if not login_visible:
        # Sometimes the site dumps us on /Home with message; try to clear and return again
        try:
            page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        except Exception:
            pass
        _dismiss_timeout_if_present(page)
        _go_to_login(page)
        login_visible = _wait_for_any_login_fields(page, timeout_ms=9000)

    dbg(f"login fields visible: {login_visible}")
    if not login_visible:
        raise RuntimeError("Login form not found — cannot proceed.")

    # Fill username
    for sel in ["#UserName", "input[name=UserName]", "input[type=text][name*=user i]", "input[id*=User i]"]:
        try:
            if page.locator(sel).first.is_visible():
                page.fill(sel, username)
                break
        except Exception:
            pass

    # Fill password
    for sel in ["#Password", "input[name=Password]", "input[type=password]", "input[id*=pass i]"]:
        try:
            if page.locator(sel).first.is_visible():
                page.fill(sel, password)
                break
        except Exception:
            pass

    # Submit
    clicked = False
    for sel in [
        'button[type=submit]',
        'input[type=submit]',
        'button:has-text("Log On")',
        'button:has-text("Login")',
        'input[type=button][value="Log On"]',
        'input[type=button][value="Login"]',
        'a:has-text("Log On")',
        'a:has-text("Login")',
    ]:
        try:
            page.locator(sel).first.click()
            clicked = True
            break
        except Exception:
            pass
    if not clicked:
        # As a last resort, press Enter in the password field
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    # Wait until the portal home (or obvious banner) shows up
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=1500)
        except Exception:
            pass
        cur = page.url
        if cur.startswith(PORTAL_HOME) or re.search(r"/Home/PortalMainPage", cur, re.I):
            break
        # Also accept visible markers
        for sel in ["#divStudentBanner", "#dvStudentBanner", "#divStudentInfo"]:
            try:
                if page.locator(sel).first.is_visible():
                    return
            except Exception:
                pass
        time.sleep(0.4)

    dbg("after login: portal loaded")


# ---------- student switching ------------------------------------------------

def _casefold(s: str) -> str:
    return (s or "").casefold()


def _banner_text(page) -> str:
    for sel in ["#divStudentBanner", "#dvStudentBanner", "#divStudentInfo", "header", "body"]:
        try:
            if page.locator(sel).first.is_visible():
                return page.locator(sel).first.inner_text(timeout=800)
        except Exception:
            pass
    return ""


def switch_to_student(page, student: str) -> bool:
    """Try tiles, then dropdown/combobox, then any link with the student's name."""
    name_cf = _casefold(student)
    try:
        page.goto(PORTAL_HOME, wait_until="domcontentloaded")
    except Exception:
        pass

    # 1) Tiles/cards
    for sel in [
        ".studentTile a",
        ".studentTile",
        "a.card, .card a, .tile a, .tile",
        f"a:has-text('{student}')",
        f"div:has-text('{student}')",
    ]:
        try:
            locs = page.locator(sel)
            count = locs.count()
            for i in range(min(count, 12)):
                el = locs.nth(i)
                try:
                    t = el.inner_text(timeout=600)
                except Exception:
                    t = ""
                if name_cf in _casefold(t):
                    el.click()
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        if name_cf in _casefold(_banner_text(page)):
                            dbg(f"switched to student {student}")
                            return True
                        time.sleep(0.2)
        except Exception:
            pass

    # 2) Dropdown
    for sel in ["select#ddlStudent", "select[name*=Student]", "select:has(option)", "[role=combobox]"]:
        try:
            el = page.locator(sel).first
            if el and el.is_visible():
                try:
                    el.select_option(label=re.compile(student, re.I))
                except Exception:
                    # fallback: iterate options
                    options = el.locator("option")
                    for i in range(options.count()):
                        txt = options.nth(i).inner_text(timeout=500)
                        val = options.nth(i).get_attribute("value") or ""
                        if name_cf in _casefold(txt) or name_cf in _casefold(val):
                            el.select_option(value=val)
                            break
                deadline = time.time() + 10
                while time.time() < deadline:
                    if name_cf in _casefold(_banner_text(page)):
                        dbg(f"switched to student {student}")
                        return True
                    time.sleep(0.2)
        except Exception:
            pass

    # 3) Any link with the name
    try:
        link = page.locator(f"a:has-text('{student}')").first
        if link and link.is_visible():
            link.click()
            deadline = time.time() + 10
            while time.time() < deadline:
                if name_cf in _casefold(_banner_text(page)):
                    dbg(f"switched to student {student}")
                    return True
                time.sleep(0.2)
    except Exception:
        pass

    return False


# ---------- assignment scraping ---------------------------------------------

def ensure_assignments_ready(page) -> Tuple[bool, int]:
    """Wait until we either see assignment tables or a 'No Assignments Available' message."""
    try:
        page.wait_for_selector('table[id^="tblAssign_"]', timeout=10000)
        return True, page.locator('table[id^="tblAssign_"]').count()
    except PlaywrightTimeoutError:
        txt = _body_text(page, timeout=800)
        if "No Assignments Available" in txt:
            return True, 0
        return False, 0


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
    mapping: Dict[int, str] = {}
    for idx, h in enumerate(raw_headers):
        key = _casefold(re.sub(r"\s+", " ", h.strip()))
        norm = HEADER_MAP.get(key)
        if not norm:
            for k, v in HEADER_MAP.items():
                if k in key:
                    norm = v
                    break
        if norm:
            mapping[idx] = norm
    return mapping


def extract_assignments_for_student(page, student: str) -> Tuple[List[List[str]], int]:
    ok, table_count = ensure_assignments_ready(page)
    if not ok:
        return [], 0

    rows: List[List[str]] = []
    tables = page.locator('table[id^="tblAssign_"]')

    # Log table IDs for visibility
    try:
        ids = []
        for i in range(tables.count()):
            try:
                ids.append(tables.nth(i).get_attribute("id") or "")
            except Exception:
                pass
        if ids:
            dbg(f"found assignment tables (ids): {ids}")
    except Exception:
        pass

    for ti in range(tables.count()):
        table = tables.nth(ti)
        headers = _read_headers(table)
        colmap = _normalize_headers(headers)

        trs = table.locator("tbody tr")
        for r in range(trs.count()):
            tds = trs.nth(r).locator("td")
            d = {c: "" for c in OUR_COLS}
            d["Student"] = student
            d["SourceURL"] = page.url

            for c in range(tds.count()):
                txt = ""
                try:
                    txt = tds.nth(c).inner_text(timeout=900).strip()
                except Exception:
                    pass
                key = colmap.get(c)
                if key:
                    d[key] = txt

            rows.append([d[k] for k in OUR_COLS])

    return rows, table_count


# ---------- Orchestrator -----------------------------------------------------

def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[List[str]], Dict[str, int]]:
    all_rows: List[List[str]] = []
    metrics = {"tables_total": 0, "students_processed": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        dbg(f"landed: {page.url}")

        login(page, username, password)

        for s in students:
            try:
                page.goto(PORTAL_HOME, wait_until="domcontentloaded")
            except Exception:
                pass

            if not switch_to_student(page, s):
                dbg(f"could not locate a picker for '{s}'; skipping switch")
                dbg(f"skipping extraction for '{s}' (could not switch)")
                continue

            rows, tbl_count = extract_assignments_for_student(page, s)
            dbg(f"class tables for {s}: {tbl_count}")
            all_rows.extend(rows)
            metrics["tables_total"] += tbl_count
            metrics["students_processed"] += 1

        browser.close()

    dbg(f"scraped {len(all_rows)} rows from portal")
    return all_rows, metrics
