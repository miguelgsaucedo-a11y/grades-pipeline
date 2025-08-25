# scraper.py
from __future__ import annotations
import re
import time
from typing import Dict, Iterable, List, Tuple
from playwright.sync_api import Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

PORTAL_BASE = "https://parentportal.cajonvalley.net"
PORTAL_HOME = f"{PORTAL_BASE}/Home/PortalMainPage"

LOGON_PATHS = [
    "/Account/LogOn",
    "/Account/Login",
    "/Account/LogOn?ReturnUrl=%2FHome%2FPortalMainPage",
    "/Account/Login?ReturnUrl=%2FHome%2FPortalMainPage",
]

def dbg(msg: str) -> None:
    print(f"DEBUG — {msg}")

def _trim(s: str, n: int = 280) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")

# ---------- helpers that work across all frames ----------

def _all_frames(page):
    # main frame first for speed/consistency
    seen = set()
    result = []
    try:
        mf = page.main_frame
        if mf:
            result.append(mf)
            seen.add(id(mf))
    except Exception:
        pass
    try:
        for fr in page.frames:
            if id(fr) not in seen:
                result.append(fr)
    except Exception:
        pass
    return result

def _body_text_any(page, timeout=1200) -> str:
    # collect a bit of text from any visible frame/body for diagnostics
    chunks: List[str] = []
    for fr in _all_frames(page):
        try:
            t = fr.locator("body").inner_text(timeout=timeout)
            if t:
                chunks.append(_trim(t, 160))
        except Exception:
            pass
    return " | ".join(chunks)[:600]

def _first_visible(page, selector: str):
    for fr in _all_frames(page):
        try:
            loc = fr.locator(selector)
            if loc.count() > 0:
                el = loc.first
                if el.is_visible():
                    return fr, el
        except Exception:
            pass
    return None, None

def _click_if_visible(page, selector: str) -> bool:
    fr, el = _first_visible(page, selector)
    if fr and el:
        try:
            el.click()
            page.wait_for_timeout(250)
            return True
        except Exception:
            return False
    return False

# ---------- login-related utilities ----------

def _dismiss_timeout_any(page) -> None:
    """
    Handles the 'Your Session Has Timed Out... Click OK to Continue' modal/overlay
    even if it is inside an iframe.
    """
    body = _body_text_any(page)
    if ("Session Has Timed Out" not in body) and ("OK to Continue" not in body):
        return

    ok_selectors = [
        'button:has-text("OK")',
        'input[type=button][value="OK"]',
        'a:has-text("OK")',
        'button:has-text("Continue")',
        'a:has-text("Continue")',
    ]
    for sel in ok_selectors:
        if _click_if_visible(page, sel):
            dbg("login DEBUG — dismissed timeout dialog")
            page.wait_for_timeout(400)
            break
    try:
        page.once("dialog", lambda d: d.accept())
    except Exception:
        pass

def _wait_login_fields_any(page, timeout_ms=8000) -> Tuple[bool, object]:
    """
    Wait (poll) for login fields in any frame, return (found, frame_with_fields).
    """
    deadline = time.time() + timeout_ms / 1000.0
    user_sels = ["#UserName", "input[name=UserName]", 'input[type="text"][name*=User]', 'input[id*=User]']
    pass_sels = ["#Password", "input[name=Password]", 'input[type="password"]', 'input[id*=Pass]']

    while time.time() < deadline:
        for fr in _all_frames(page):
            u_ok = False
            p_ok = False
            for us in user_sels:
                try:
                    if fr.locator(us).first.is_visible():
                        u_ok = True
                        break
                except Exception:
                    pass
            for ps in pass_sels:
                try:
                    if fr.locator(ps).first.is_visible():
                        p_ok = True
                        break
                except Exception:
                    pass
            if u_ok and p_ok:
                return True, fr
        page.wait_for_timeout(120)
    return False, None

def _navigate_to_login(page) -> Tuple[bool, object]:
    """
    Multi-strategy approach:
      1) Go to root/PortalMainPage, clear any timeout dialog.
      2) Click any 'Log On / Login / Sign In' link in any frame.
      3) Try each known login path (incl. ReturnUrl) with a referer in place.
      4) If we hit Error.htm, call /Account/LogOff, back to root, and retry.
    Returns (found_fields, frame).
    """
    for attempt in range(4):
        try:
            page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        except Exception:
            pass

        _dismiss_timeout_any(page)

        # Strategy A: find a login link in any frame
        for sel in [
            'a[href*="/Account/LogOn"]',
            'a[href*="/Account/Login"]',
            'a:has-text("Log On")',
            'a:has-text("Log In")',
            'a:has-text("Sign In")',
            'button:has-text("Log On")',
            'button:has-text("Login")',
            'button:has-text("Sign In")',
        ]:
            if _click_if_visible(page, sel):
                found, fr = _wait_login_fields_any(page, timeout_ms=6000)
                if found:
                    return True, fr

        # Strategy B: try direct login URLs (we already have a referer now)
        for path in LOGON_PATHS:
            try:
                page.goto(f"{PORTAL_BASE}{path}&_={int(time.time()*1000)}" if "?" in path
                          else f"{PORTAL_BASE}{path}?_={int(time.time()*1000)}",
                          wait_until="domcontentloaded")
            except Exception:
                pass

            # If server throws ASP.NET Error page, back off this path
            if "/Error.htm" in page.url or "An error occurred while processing your request" in _body_text_any(page):
                dbg(f"login DEBUG — got error page when requesting {path}; backing out")
                # Try to reset server-side auth state
                try:
                    page.goto(f"{PORTAL_BASE}/Account/LogOff", wait_until="domcontentloaded")
                except Exception:
                    pass
                try:
                    page.goto(PORTAL_HOME, wait_until="domcontentloaded")
                except Exception:
                    pass
                _dismiss_timeout_any(page)
                continue

            found, fr = _wait_login_fields_any(page, timeout_ms=6000)
            if found:
                return True, fr

        # Small wait before next attempt
        page.wait_for_timeout(500)

    dbg(f"login DEBUG — url now: {page.url}")
    dbg(f"login DEBUG — body: {_trim(_body_text_any(page), 240)}")
    return False, None

def login(page, username: str, password: str) -> None:
    found, fr = _navigate_to_login(page)
    dbg(f"login fields visible: {found}")
    if not found:
        raise RuntimeError("Login form not found — cannot proceed.")

    target = fr or page  # page and frame share same fill/click API

    # Fill username
    for sel in ["#UserName", "input[name=UserName]", 'input[type="text"][name*=User]', 'input[id*=User]']:
        try:
            if target.locator(sel).first.is_visible():
                target.fill(sel, username)
                break
        except Exception:
            pass

    # Fill password
    for sel in ["#Password", "input[name=Password]", 'input[type="password"]', 'input[id*=Pass]']:
        try:
            if target.locator(sel).first.is_visible():
                target.fill(sel, password)
                break
        except Exception:
            pass

    # Submit
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
        if _click_if_visible(target, sel):
            break
    try:
        target.keyboard.press("Enter")
    except Exception:
        pass

    # Wait until we appear to be inside the portal
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=1200)
        except Exception:
            pass
        cur = page.url
        if cur.startswith(PORTAL_HOME) or re.search(r"/Home/PortalMainPage", cur, re.I):
            break
        for sel in ["#divStudentBanner", "#dvStudentBanner", "#divStudentInfo"]:
            try:
                if page.locator(sel).first.is_visible():
                    return
            except Exception:
                pass
        _dismiss_timeout_any(page)
        time.sleep(0.3)
    dbg("after login: portal loaded")

# ---------- student switching & scraping ----------

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
    name_cf = _casefold(student)
    try:
        page.goto(PORTAL_HOME, wait_until="domcontentloaded")
    except Exception:
        pass

    # tiles/cards
    for sel in [
        ".studentTile a", ".studentTile",
        "a.card, .card a, .tile a, .tile",
        f"a:has-text('{student}')", f"div:has-text('{student}')",
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

    # dropdown
    for sel in ["select#ddlStudent", "select[name*=Student]", "select:has(option)", "[role=combobox]"]:
        try:
            el = page.locator(sel).first
            if el and el.is_visible():
                try:
                    el.select_option(label=re.compile(student, re.I))
                except Exception:
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

    # generic link
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

def ensure_assignments_ready(page) -> Tuple[bool, int]:
    try:
        page.wait_for_selector('table[id^="tblAssign_"]', timeout=10000)
        return True, page.locator('table[id^="tblAssign_"]').count()
    except PlaywrightTimeoutError:
        txt = _body_text_any(page, timeout=900)
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
    "Student", "Period", "Course", "Teacher",
    "DueDate", "AssignedDate", "Assignment",
    "PtsPossible", "Score", "Pct", "Status",
    "Comments", "SourceURL",
]

def _normalize_headers(raw_headers: Iterable[str]) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    for idx, h in enumerate(raw_headers):
        key = (re.sub(r"\s+", " ", h.strip()) or "").casefold()
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

def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[List[str]], Dict[str, int]]:
    all_rows: List[List[str]] = []
    metrics = {"tables_total": 0, "students_processed": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--lang=en-US",
            ],
        )
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            locale="en-US",
            timezone_id="America/Los_Angeles",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-CH-UA-Platform": "Windows",
                "Sec-CH-UA": '"Chromium";v="126", "Not.A/Brand";v="24", "Google Chrome";v="126"',
                "Sec-CH-UA-Mobile": "?0",
            },
        )
        page = ctx.new_page()

        try:
            page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        except Exception:
            pass
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
