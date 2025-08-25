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
    "/",  # root landing often has the login block
    "/Account/LogOn",
    "/Account/Login",
    "/Account/LogOn?ReturnUrl=%2FHome%2FPortalMainPage",
    "/Account/Login?ReturnUrl=%2FHome%2FPortalMainPage",
]

def dbg(msg: str) -> None:
    print(f"DEBUG — {msg}")

def _trim(s: str, n: int = 260) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")

# -------------------- frame helpers --------------------

def _all_frames(page):
    seen = set()
    out = []
    try:
        mf = page.main_frame
        if mf:
            seen.add(id(mf))
            out.append(mf)
    except Exception:
        pass
    try:
        for fr in page.frames:
            if id(fr) not in seen:
                out.append(fr)
    except Exception:
        pass
    return out

def _body_text_any(page, timeout=1000) -> str:
    parts = []
    for fr in _all_frames(page):
        try:
            t = fr.locator("body").inner_text(timeout=timeout)
            if t:
                parts.append(_trim(t, 180))
        except Exception:
            pass
    return " | ".join(parts)[:600]

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

# -------------------- login flows --------------------

def _dismiss_timeout_any(page) -> None:
    txt = _body_text_any(page)
    if ("Session Has Timed Out" not in txt) and ("OK to Continue" not in txt):
        return
    for sel in [
        'button:has-text("OK")', 'input[type=button][value="OK"]',
        'a:has-text("OK")', 'button:has-text("Continue")', 'a:has-text("Continue")',
    ]:
        if _click_if_visible(page, sel):
            dbg("login DEBUG — dismissed timeout dialog")
            page.wait_for_timeout(350)
            break
    try:
        page.once("dialog", lambda d: d.accept())
    except Exception:
        pass

PIN_SELECTORS = [
    "#PIN", "input[name=PIN]", 'input[id*="PIN"]', 'input[placeholder*="PIN" i]', 'input[aria-label*="PIN" i]',
]
USER_SELECTORS = [
    "#UserName", "input[name=UserName]", 'input[name="username" i]', 'input[id*="user" i]',
] + PIN_SELECTORS
PASS_SELECTORS = [
    "#Password", "input[name=Password]", 'input[id*="pass" i]', 'input[type="password"]',
    'input[placeholder*="password" i]', 'input[aria-label*="password" i]',
]
SUBMIT_SELECTORS = [
    'button[type=submit]', 'input[type=submit]',
    'button:has-text("Log On")', 'button:has-text("Login")', 'button:has-text("Sign In")',
    'input[type=button][value="Log On"]', 'input[type=button][value="Login"]',
    'a:has-text("Log On")', 'a:has-text("Login")', 'a:has-text("Sign In")',
]

def _wait_login_fields_any(page, timeout_ms=8000) -> Tuple[bool, object, str, str]:
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for fr in _all_frames(page):
            u_sel, p_sel = "", ""
            # username / PIN
            for us in USER_SELECTORS:
                try:
                    if fr.locator(us).first.is_visible():
                        u_sel = us; break
                except Exception: pass
            if not u_sel:
                try:
                    if fr.get_by_label(re.compile(r"\bPIN\b", re.I)).is_visible():
                        u_sel = "LABEL::PIN"
                except Exception: pass
            # password
            for ps in PASS_SELECTORS:
                try:
                    if fr.locator(ps).first.is_visible():
                        p_sel = ps; break
                except Exception: pass
            if not p_sel:
                try:
                    if fr.get_by_label(re.compile(r"password", re.I)).is_visible():
                        p_sel = "LABEL::PASSWORD"
                except Exception: pass
            if u_sel and p_sel:
                return True, fr, u_sel, p_sel
        page.wait_for_timeout(120)
    return False, None, "", ""

def _navigate_to_login(page) -> Tuple[bool, object, str, str]:
    for _ in range(4):
        try:
            page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        except Exception:
            pass
        _dismiss_timeout_any(page)

        # any obvious triggers on the landing page
        for sel in [
            'a:has-text("ParentPortal Login")', 'a:has-text("Parent Portal Login")',
            'a:has-text("Log On")', 'a:has-text("Login")', 'a:has-text("Sign In")',
            'button:has-text("ParentPortal Login")', 'button:has-text("Login")', 'button:has-text("Sign In")',
        ]:
            if _click_if_visible(page, sel):
                ok, fr, us, ps = _wait_login_fields_any(page, timeout_ms=6000)
                if ok: return True, fr, us, ps

        for path in LOGON_PATHS:
            url = f"{PORTAL_BASE}{path}" if path.startswith("/") else f"{PORTAL_BASE}/{path}"
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception:
                pass

            if "/Error.htm" in page.url or "An error occurred while processing your request" in _body_text_any(page):
                dbg(f"login DEBUG — got error page when requesting {path}; backing out")
                try: page.goto(f"{PORTAL_BASE}/Account/LogOff", wait_until="domcontentloaded")
                except Exception: pass
                try: page.goto(PORTAL_HOME, wait_until="domcontentloaded")
                except Exception: pass
                _dismiss_timeout_any(page)
                continue

            # collapsed login panel on root/home
            for sel in ['a:has-text("ParentPortal Login")','button:has-text("ParentPortal Login")',
                        'a:has-text("Parent Portal Login")','button:has-text("Parent Portal Login")']:
                _click_if_visible(page, sel)

            ok, fr, us, ps = _wait_login_fields_any(page, timeout_ms=6000)
            if ok: return True, fr, us, ps

        page.wait_for_timeout(400)

    dbg(f"login DEBUG — url now: {page.url}")
    dbg(f"login DEBUG — body: {_trim(_body_text_any(page))}")
    return False, None, "", ""

def login(page, username: str, password: str) -> None:
    found, fr, user_sel, pass_sel = _navigate_to_login(page)
    dbg(f"login fields visible: {found}")
    if not found:
        raise RuntimeError("Login form not found — cannot proceed.")

    target = fr or page

    def _fill(sel: str, val: str):
        try:
            if sel == "LABEL::PIN":
                target.get_by_label(re.compile(r"\bPIN\b", re.I)).fill(val); return True
            if sel == "LABEL::PASSWORD":
                target.get_by_label(re.compile(r"password", re.I)).fill(val); return True
            target.fill(sel, val); return True
        except Exception:
            return False

    if not _fill(user_sel, username):
        for sel in ['input[type="text"]','input[type="tel"]','input[type="number"]','input:not([type])']:
            try:
                loc = target.locator(sel).first
                if loc and loc.is_visible(): loc.fill(username); break
            except Exception: pass

    if not _fill(pass_sel, password):
        try: target.locator('input[type="password"]').first.fill(password)
        except Exception: pass

    # submit
    if not any(_click_if_visible(target, sel) for sel in SUBMIT_SELECTORS):
        try: target.keyboard.press("Enter")
        except Exception: pass

    # wait until portal is usable
    deadline = time.time() + 20
    while time.time() < deadline:
        try: page.wait_for_load_state("domcontentloaded", timeout=1000)
        except Exception: pass
        if page.url.startswith(PORTAL_HOME) or re.search(r"/Home/PortalMainPage", page.url, re.I):
            break
        for sel in ["#divStudentBanner", "#dvStudentBanner", "#divStudentInfo"]:
            try:
                if page.locator(sel).first.is_visible(): return
            except Exception: pass
        _dismiss_timeout_any(page)
        time.sleep(0.25)
    dbg("after login: portal loaded")

# -------------------- student switching & scraping --------------------

def _casefold(s: str) -> str:
    return (s or "").casefold()

def _banner_text(page) -> str:
    for sel in ["#divStudentBanner", "#dvStudentBanner", "#divStudentInfo",
                "header", "h1", "h2", ".studentBanner", ".student-name"]:
        try:
            if page.locator(sel).first.is_visible():
                return page.locator(sel).first.inner_text(timeout=700)
        except Exception:
            pass
    return ""

def _click_any_student_trigger(page) -> None:
    # Open any menu/tile that likely exposes student choices
    triggers = [
        'a:has-text("Students")', 'a:has-text("My Students")', 'a:has-text("Select Student")',
        'button:has-text("Students")', 'button:has-text("My Students")', 'button:has-text("Select Student")',
        '[id*="student"][aria-haspopup="true"]', '[data-toggle=dropdown][id*="student" i]',
        '#studentMenu', '#studentPicker', '.student-picker', '.dropdown:has-text("Student")',
        '.tile:has-text("Student")', '.card:has-text("Student")'
    ]
    for sel in triggers:
        _click_if_visible(page, sel)

def _click_by_text(page, name: str) -> bool:
    """Find a clickable ancestor (a/button) for a node that contains the name."""
    pattern = re.compile(re.escape(name), re.I)

    # role based
    for role in ["link", "button", "menuitem"]:
        try:
            el = page.get_by_role(role, name=pattern).first
            if el and el.is_visible(): el.click(); return True
        except Exception:
            pass

    # generic text -> nearest clickable ancestor
    try:
        el = page.get_by_text(pattern).first
        if el and el.is_visible():
            # try itself
            try:
                tag = (el.evaluate("el => el.tagName") or "").lower()
            except Exception:
                tag = ""
            if tag in ("a", "button"): el.click(); return True
            # bubble up to clickable ancestor
            anc = el.locator("xpath=ancestor::*[self::a or self::button or @role='button'][1]").first
            if anc and anc.is_visible(): anc.click(); return True
    except Exception:
        pass

    # last resort: any element containing text then click it
    try:
        el = page.locator(f'xpath=//*[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{name.lower()}")]').first
        if el and el.is_visible(): el.click(); return True
    except Exception:
        pass

    return False

def switch_to_student(page, student: str) -> bool:
    name_cf = _casefold(student)
    try:
        page.goto(PORTAL_HOME, wait_until="domcontentloaded")
    except Exception:
        pass

    # If we already show the student's name in banner/body, accept it
    if name_cf and (name_cf in _casefold(_banner_text(page)) or name_cf in _casefold(_body_text_any(page))):
        dbg(f"switched to student {student} (already active)")
        return True

    # open student switchers/menus
    _click_any_student_trigger(page)

    # attempt direct clicks/roles/ancestor heuristics
    if _click_by_text(page, student):
        deadline = time.time() + 10
        while time.time() < deadline:
            if name_cf in _casefold(_banner_text(page)) or name_cf in _casefold(_body_text_any(page)):
                dbg(f"switched to student {student}")
                return True
            time.sleep(0.2)

    # try common “tiles/cards” and dropdowns explicitly
    for sel in [".studentTile a", ".studentTile", "a.card, .card a, .tile a, .tile"]:
        try:
            locs = page.locator(sel)
            count = min(locs.count(), 16)
            for i in range(count):
                el = locs.nth(i)
                text = ""
                try: text = el.inner_text(timeout=600)
                except Exception: pass
                if name_cf in _casefold(text):
                    el.click()
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        if name_cf in _casefold(_banner_text(page)) or name_cf in _casefold(_body_text_any(page)):
                            dbg(f"switched to student {student}")
                            return True
                        time.sleep(0.2)
        except Exception:
            pass

    for sel in ["select#ddlStudent", "select[name*=Student]", "select:has(option)", "[role=combobox]"]:
        try:
            drop = page.locator(sel).first
            if drop and drop.is_visible():
                try:
                    drop.select_option(label=re.compile(student, re.I))
                except Exception:
                    options = drop.locator("option")
                    for i in range(options.count()):
                        txt = options.nth(i).inner_text(timeout=500)
                        val = options.nth(i).get_attribute("value") or ""
                        if name_cf in _casefold(txt) or name_cf in _casefold(val):
                            drop.select_option(value=val); break
                deadline = time.time() + 10
                while time.time() < deadline:
                    if name_cf in _casefold(_banner_text(page)) or name_cf in _casefold(_body_text_any(page)):
                        dbg(f"switched to student {student}")
                        return True
                    time.sleep(0.2)
        except Exception:
            pass

    return False

def ensure_assignments_ready(page) -> Tuple[bool, int]:
    try:
        page.wait_for_selector('table[id^="tblAssign_"]', timeout=6000)
        return True, page.locator('table[id^="tblAssign_"]').count()
    except PlaywrightTimeoutError:
        # try to navigate via any Assignments tile/link
        for sel in [
            'a:has-text("Assignments")', 'button:has-text("Assignments")',
            '.tile:has-text("Assignments")', '.card:has-text("Assignments")',
            'a[href*="Assignment" i]', 'a[href*="assign" i]'
        ]:
            if _click_if_visible(page, sel):
                try:
                    page.wait_for_selector('table[id^="tblAssign_"]', timeout=6000)
                    return True, page.locator('table[id^="tblAssign_"]').count()
                except PlaywrightTimeoutError:
                    pass
        txt = _body_text_any(page, timeout=800)
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
    "period": "Period", "course": "Course", "section": "Course", "teacher": "Teacher",
    "due": "DueDate", "due date": "DueDate", "assigned": "AssignedDate", "assigned date": "AssignedDate",
    "assignment": "Assignment", "points possible": "PtsPossible", "pts possible": "PtsPossible",
    "points": "PtsPossible", "score": "Score", "percent": "Pct", "%": "Pct",
    "status": "Status", "comments": "Comments",
}
OUR_COLS = ["Student","Period","Course","Teacher","DueDate","AssignedDate","Assignment","PtsPossible","Score","Pct","Status","Comments","SourceURL"]

def _normalize_headers(raw_headers: Iterable[str]) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    for idx, h in enumerate(raw_headers):
        key = (re.sub(r"\s+", " ", h.strip()) or "").casefold()
        norm = HEADER_MAP.get(key)
        if not norm:
            for k, v in HEADER_MAP.items():
                if k in key: norm = v; break
        if norm: mapping[idx] = norm
    return mapping

def extract_assignments_for_student(page, student: str) -> Tuple[List[List[str]], int]:
    ok, table_count = ensure_assignments_ready(page)
    if not ok:
        return [], 0

    out_rows: List[List[str]] = []
    tables = page.locator('table[id^="tblAssign_"]')

    try:
        ids = []
        for i in range(tables.count()):
            try: ids.append(tables.nth(i).get_attribute("id") or "")
            except Exception: pass
        if ids: dbg(f"found assignment tables (ids): {ids}")
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
                try: txt = tds.nth(c).inner_text(timeout=800).strip()
                except Exception: pass
                key = colmap.get(c)
                if key: d[key] = txt

            out_rows.append([d[k] for k in OUR_COLS])

    return out_rows, table_count

def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[List[str]], Dict[str, int]]:
    all_rows: List[List[str]] = []
    metrics = {"tables_total": 0, "students_processed": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--lang=en-US"],
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

        try: page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        except Exception: pass
        dbg(f"landed: {page.url}")

        login(page, username, password)

        for s in students:
            try: page.goto(PORTAL_HOME, wait_until="domcontentloaded")
            except Exception: pass

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
