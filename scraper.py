from __future__ import annotations
import re, time
from typing import Dict, Iterable, List, Tuple
from playwright.sync_api import Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

PORTAL_BASE  = "https://parentportal.cajonvalley.net"
PORTAL_HOME  = f"{PORTAL_BASE}/Home/PortalMainPage"

def dbg(s: str): print(f"DEBUG — {s}")
def _trim(s: str, n: int = 160) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")

# ---------- frame helpers ----------
def _all_frames(page):
    out = []
    try:
        if page.main_frame: out.append(page.main_frame)
    except Exception: pass
    try:
        for fr in page.frames:
            if fr not in out: out.append(fr)
    except Exception: pass
    return out

def _body_text_any(page, timeout=900) -> str:
    parts = []
    for fr in _all_frames(page):
        try:
            t = fr.locator("body").inner_text(timeout=timeout)
            if t: parts.append(_trim(t, 220))
        except Exception: pass
    return " | ".join(parts)[:1200]

def _first_visible(page, selector: str):
    for fr in _all_frames(page):
        try:
            loc = fr.locator(selector)
            if loc.count() > 0:
                el = loc.first
                if el.is_visible(): return fr, el
        except Exception: pass
    return None, None

def _click_if_visible(page, selector: str) -> bool:
    fr, el = _first_visible(page, selector)
    if fr and el:
        try:
            el.click()
            page.wait_for_timeout(200)
            return True
        except Exception:
            return False
    return False

# ---------- login ----------
LOGON_PATHS = ["/Account/LogOn", "/Account/Login"]
USER_SELECTORS = ["#UserName", "input[name=UserName]", 'input[id*="user" i]', 'input[type="text"]']
PIN_LIKE     = ['input[id*="PIN"]','input[name="PIN"]','input[placeholder*="PIN" i]']
PASS_SELECTORS = ["#Password","input[name=Password]",'input[type="password"]','input[id*="pass" i]']
SUBMIT_SELECTORS = ['button[type=submit]','input[type=submit]','button:has-text("Login")','button:has-text("Log On")','input[type=button][value="Login"]']

def _dismiss_timeout_any(page):
    txt = _body_text_any(page)
    if ("Session Has Timed Out" not in txt) and ("OK to Continue" not in txt):
        return
    for sel in ['button:has-text("OK")','a:has-text("OK")','button:has-text("Continue")']:
        if _click_if_visible(page, sel):
            dbg("login DEBUG — dismissed timeout dialog")
            page.wait_for_timeout(300); break
    try: page.once("dialog", lambda d: d.accept())
    except Exception: pass

def _wait_login_fields_any(page, timeout_ms=9000):
    deadline = time.time() + timeout_ms/1000
    while time.time() < deadline:
        for fr in _all_frames(page):
            u=p=""
            for sel in USER_SELECTORS + PIN_LIKE:
                try:
                    if fr.locator(sel).first.is_visible(): u = sel; break
                except Exception: pass
            for sel in PASS_SELECTORS:
                try:
                    if fr.locator(sel).first.is_visible(): p = sel; break
                except Exception: pass
            if u and p: return True, fr, u, p
        page.wait_for_timeout(120)
    return False, None, "", ""

def _navigate_to_login(page):
    for _ in range(4):
        try: page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        except Exception: pass
        _dismiss_timeout_any(page)
        for path in LOGON_PATHS + ["/"]:
            try:
                page.goto(PORTAL_BASE + path, wait_until="domcontentloaded")
            except Exception: pass
            _dismiss_timeout_any(page)
            ok, fr, us, ps = _wait_login_fields_any(page)
            if ok: return True, fr, us, ps
        # try obvious entrypoints
        for sel in ['a:has-text("ParentPortal Login")','button:has-text("ParentPortal Login")',
                    'a:has-text("Login")','button:has-text("Login")','a:has-text("Sign In")']:
            if _click_if_visible(page, sel):
                ok, fr, us, ps = _wait_login_fields_any(page)
                if ok: return True, fr, us, ps
    dbg(f"login DEBUG — url now: {page.url}")
    dbg(f"login DEBUG — body≈ {_trim(_body_text_any(page))}")
    return False, None, "", ""

def login(page, username: str, password: str):
    ok, fr, us, ps = _navigate_to_login(page)
    dbg(f"login fields visible: {ok}")
    if not ok: raise RuntimeError("Login form not found — cannot proceed.")
    tgt = fr or page

    def _fill(sel, val):
        try: tgt.locator(sel).first.fill(val); return True
        except Exception: return False

    if not _fill(us, username):
        for sel in ['input[type="text"]','input:not([type])','input[type="tel"]']:
            try: tgt.locator(sel).first.fill(username); break
            except Exception: pass
    if not _fill(ps, password):
        try: tgt.locator('input[type="password"]').first.fill(password)
        except Exception: pass

    done = False
    for sel in SUBMIT_SELECTORS:
        if _click_if_visible(tgt, sel): done = True; break
    if not done:
        try: tgt.keyboard.press("Enter")
        except Exception: pass

    # wait until portal/home or banner exists
    deadline = time.time() + 20
    while time.time() < deadline:
        try: page.wait_for_load_state("domcontentloaded", timeout=1000)
        except Exception: pass
        if "/Home/PortalMainPage" in page.url: break
        for bsel in ["#divStudentBanner","#dvStudentBanner",".student-name",".studentBanner"]:
            try:
                if page.locator(bsel).first.is_visible(): break
            except Exception: pass
        _dismiss_timeout_any(page)
        time.sleep(0.2)
    dbg("after login: portal loaded")

# ---------- UI snapshot & switching ----------
def _banner_text(page) -> str:
    for sel in ["#divStudentBanner","#dvStudentBanner","#divStudentInfo",".studentBanner",".student-name"]:
        try:
            if page.locator(sel).first.is_visible():
                return page.locator(sel).first.inner_text(timeout=600)
        except Exception: pass
    return ""

def _visible_texts(page, selectors: List[str], limit=60) -> List[str]:
    out=[]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = min(loc.count(), limit-len(out))
            for i in range(n):
                el = loc.nth(i)
                if el.is_visible():
                    txt = (el.inner_text(timeout=500) or "").strip()
                    lab = el.get_attribute("aria-label") or ""
                    tit = el.get_attribute("title") or ""
                    if txt or lab or tit:
                        out.append(_trim(" | ".join([v for v in [txt, lab, tit] if v]), 120))
                if len(out) >= limit: break
        except Exception: pass
        if len(out) >= limit: break
    return out

def debug_student_ui_snapshot(page):
    try:
        dbg(f"UI SNAPSHOT — url: {page.url}")
        banner = _trim(_banner_text(page), 160) or "(none)"
        dbg(f"UI SNAPSHOT — banner: {banner}")
        clicks = _visible_texts(page, [
            "a","button","[role=button]","[role=menuitem]",".dropdown a",".navbar a",".card a",".tile a"
        ], limit=30)
        if clicks:
            dbg("UI SNAPSHOT — clickables(sample): " + " || ".join(clicks[:18]))
        # options inside any selects
        options=[]
        try:
            sels = page.locator("select")
            for i in range(min(4, sels.count())):
                opts = sels.nth(i).locator("option")
                for j in range(min(12, opts.count())):
                    t = (opts.nth(j).inner_text(timeout=400) or "").strip()
                    if t: options.append(_trim(t, 80))
        except Exception: pass
        if options:
            dbg("UI SNAPSHOT — select options(sample): " + " | ".join(options[:18]))
    except Exception: pass

def _open_menus(page):
    for sel in [
        ".navbar-toggler",'button.navbar-toggler','[aria-label*=menu i]','[class*=hamburger i]',
        ".dropdown-toggle",'[data-toggle=dropdown]','[aria-haspopup=true]',
        ".navbar .dropdown > a",".navbar .dropdown > button",
        "#studentMenu","#studentPicker",".student-picker",
    ]:
        _click_if_visible(page, sel)

def _click_by_text_like(page, needle: str) -> bool:
    pat = re.compile(re.escape(needle), re.I)
    for role in ["link","button","menuitem","option"]:
        try:
            el = page.get_by_role(role, name=pat).first
            if el and el.is_visible(): el.click(); return True
        except Exception: pass
    try:
        el = page.get_by_text(pat, exact=False).first
        if el and el.is_visible():
            # click nearest actionable ancestor
            try:
                tag = (el.evaluate("el=>el.tagName") or "").lower()
            except Exception: tag = ""
            if tag in ("a","button","option"): el.click(); return True
            anc = el.locator("xpath=ancestor::*[self::a or self::button or self::option or @role='button'][1]").first
            if anc and anc.is_visible(): anc.click(); return True
    except Exception: pass
    # alt/title/aria on images/icons
    try:
        el = page.locator(
            f'[alt*="{needle}" i], [title*="{needle}" i], [aria-label*="{needle}" i]'
        ).first
        if el and el.is_visible(): el.click(); return True
    except Exception: pass
    return False

def switch_to_student(page, student: str) -> bool:
    target = (student or "").strip()
    if not target: return False
    name_cf = target.casefold()

    try: page.goto(PORTAL_HOME, wait_until="domcontentloaded")
    except Exception: pass

    debug_student_ui_snapshot(page)
    # already there?
    if name_cf in _banner_text(page).casefold() or name_cf in _body_text_any(page).casefold():
        dbg(f"switched to student {target} (already active)")
        return True

    # 1) open menus, click obvious links
    _open_menus(page)
    for key in ["My Students","Students","Select Student","Change Student","Switch Student",
                "Assignments","Grades","Gradebook"]:
        if _click_by_text_like(page, key): break
    if _click_by_text_like(page, target):
        deadline = time.time()+10
        while time.time()<deadline:
            if name_cf in _banner_text(page).casefold() or name_cf in _body_text_any(page).casefold():
                dbg(f"switched to student {target}"); return True
            time.sleep(0.2)

    # 2) expand any dropdowns and try again
    _open_menus(page)
    if _click_by_text_like(page, target):
        deadline = time.time()+10
        while time.time()<deadline:
            if name_cf in _banner_text(page).casefold() or name_cf in _body_text_any(page).casefold():
                dbg(f"switched to student {target}"); return True
            time.sleep(0.2)

    # 3) try selects directly
    for sel in ["select#ddlStudent","select[name*=Student i]","select:has(option)","[role=combobox]"]:
        try:
            drop = page.locator(sel).first
            if drop and drop.is_visible():
                try:
                    drop.select_option(label=re.compile(target, re.I))
                except Exception:
                    opts = drop.locator("option")
                    for i in range(min(30, opts.count())):
                        txt = (opts.nth(i).inner_text(timeout=400) or "")
                        val = opts.nth(i).get_attribute("value") or ""
                        if name_cf in txt.casefold() or name_cf in val.casefold():
                            drop.select_option(value=val); break
                time.sleep(0.4)
                if name_cf in _banner_text(page).casefold() or name_cf in _body_text_any(page).casefold():
                    dbg(f"switched to student {target}"); return True
        except Exception: pass

    debug_student_ui_snapshot(page)
    return False

# ---------- assignments ----------
def ensure_assignments_ready(page) -> Tuple[bool,int]:
    try:
        page.wait_for_selector('table[id^="tblAssign_"]', timeout=6000)
        return True, page.locator('table[id^="tblAssign_"]').count()
    except PlaywrightTimeoutError:
        for sel in ['a:has-text("Assignments")','button:has-text("Assignments")',
                    '.tile:has-text("Assignments")','.card:has-text("Assignments")',
                    'a[href*="Assign" i]','a[href*="Gradebook" i]']:
            if _click_if_visible(page, sel):
                try:
                    page.wait_for_selector('table[id^="tblAssign_"]', timeout=6000)
                    return True, page.locator('table[id^="tblAssign_"]').count()
                except PlaywrightTimeoutError:
                    pass
        if "No Assignments Available" in _body_text_any(page): return True, 0
        return False, 0

def _read_headers(table) -> List[str]:
    heads=[]
    try: heads = [h.strip() for h in table.locator("thead tr th").all_text_contents()]
    except Exception: pass
    if not heads:
        try: heads = [h.strip() for h in table.locator("tr").first.locator("th,td").all_text_contents()]
        except Exception: heads=[]
    return heads

HEADER_MAP = {
    "period":"Period","course":"Course","section":"Course","teacher":"Teacher",
    "due":"DueDate","due date":"DueDate","assigned":"AssignedDate","assigned date":"AssignedDate",
    "assignment":"Assignment","points possible":"PtsPossible","pts possible":"PtsPossible",
    "points":"PtsPossible","score":"Score","percent":"Pct","%":"Pct",
    "status":"Status","comments":"Comments",
}
OUR_COLS = ["Student","Period","Course","Teacher","DueDate","AssignedDate","Assignment","PtsPossible","Score","Pct","Status","Comments","SourceURL"]

def _normalize_headers(raw: Iterable[str]) -> Dict[int,str]:
    mapping={}
    for i,h in enumerate(raw):
        key = (re.sub(r"\s+"," ",h.strip()) or "").casefold()
        norm = HEADER_MAP.get(key)
        if not norm:
            for k,v in HEADER_MAP.items():
                if k in key: norm=v; break
        if norm: mapping[i]=norm
    return mapping

def extract_assignments_for_student(page, student: str) -> Tuple[List[List[str]],int]:
    ok, table_count = ensure_assignments_ready(page)
    if not ok: return [], 0

    out=[]
    tables = page.locator('table[id^="tblAssign_"]')
    try:
        ids=[]
        for i in range(tables.count()):
            try: ids.append(tables.nth(i).get_attribute("id") or "")
            except Exception: pass
        if ids: dbg(f"found assignment tables (ids): {ids}")
    except Exception: pass

    for ti in range(tables.count()):
        table = tables.nth(ti)
        headers = _read_headers(table)
        colmap  = _normalize_headers(headers)
        trs = table.locator("tbody tr")
        for r in range(trs.count()):
            tds = trs.nth(r).locator("td")
            d = {c:"" for c in OUR_COLS}
            d["Student"]=student
            d["SourceURL"]=page.url
            for c in range(tds.count()):
                try: txt = tds.nth(c).inner_text(timeout=700).strip()
                except Exception: txt=""
                key = colmap.get(c)
                if key: d[key]=txt
            out.append([d[k] for k in OUR_COLS])
    return out, table_count

# ---------- runner ----------
def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[List[str]], Dict[str,int]]:
    rows=[]
    metrics={"tables_total":0,"students_processed":0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled","--lang=en-US"],
        )
        ctx = browser.new_context(
            viewport={"width":1366,"height":900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            locale="en-US",
            timezone_id="America/Los_Angeles",
            extra_http_headers={"Accept-Language":"en-US,en;q=0.9"},
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

            r, cnt = extract_assignments_for_student(page, s)
            dbg(f"class tables for {s}: {cnt}")
            rows.extend(r)
            metrics["tables_total"] += cnt
            metrics["students_processed"] += 1

        browser.close()

    dbg(f"scraped {len(rows)} rows from portal")
    return rows, metrics
