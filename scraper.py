import re
import time
from typing import List, Tuple
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PORTAL_URL = "https://parentportal.cajonvalley.net/Home/PortalMainPage"

# ---------- helpers

def _log_ui_snapshot(page, label="UI SNAPSHOT"):
    url = page.url
    # tiny sample of visible link/button texts to help diagnose
    try:
        link_texts = page.eval_on_selector_all(
            "a, button, [role='menuitem'], [role='button'], li",
            "els => els.map(e => (e.innerText||'').trim()).filter(t => t.length>0).slice(0,15)"
        )
    except Exception:
        link_texts = []
    sample = ", ".join([t[:40] for t in link_texts])
    print(f"DEBUG — {label} — url: {url}")
    print(f"DEBUG — {label} — sample: [{sample}]")

def _dismiss_timeout_dialog_if_present(page):
    # Their portal sometimes throws a “Your Session Has Timed Out… OK to Continue”.
    try:
        # Click any visible button with OK/Continue/Dismiss wording.
        for text in ["OK", "Ok", "Continue", "Close"]:
            loc = page.get_by_role("button", name=re.compile(text, re.I))
            if loc.count():
                loc.first.click(timeout=1500)
                print("DEBUG — login DEBUG — dismissed timeout dialog")
                break
    except Exception:
        pass

def _login_if_needed(page, username: str, password: str) -> bool:
    # Heuristics for the login form
    try:
        # If already at main page and logged in, skip
        if "PortalMainPage" in page.url and "LogOn" not in page.url and "Error" not in page.url:
            # Still check if a login panel is floating
            user_box = page.locator("input[type='text'], input[type='email']").first
            pass_box = page.locator("input[type='password']").first
            if user_box.count()==0 and pass_box.count()==0:
                return True
    except Exception:
        pass

    # Try common input names/ids
    user_sel = "input[name='UserName'], input[name='LoginName'], input[id*='User'], input[type='email']"
    pass_sel = "input[name='Password'], input[id*='Password'], input[type='password']"

    try:
        user = page.locator(user_sel).first
        pwd = page.locator(pass_sel).first

        if user.count() and pwd.count():
            print("DEBUG — login fields visible: True")
            user.fill(username, timeout=8000)
            pwd.fill(password, timeout=8000)

            # Submit through a likely button
            try:
                page.get_by_role("button", name=re.compile("(Log.?in|Sign.?in)", re.I)).first.click(timeout=3000)
            except Exception:
                # fallback: press Enter
                pwd.press("Enter")
            page.wait_for_load_state("networkidle", timeout=15000)
            _dismiss_timeout_dialog_if_present(page)
            return True
        else:
            print("DEBUG — login fields visible: False")
            return True  # sometimes already logged in
    except Exception:
        return True  # fail open; downstream guards handle it

def _open_possible_student_menu(page):
    # Click anything that looks like a student menu trigger.
    triggers = [
        "#studentPicker", "#selectStudent", "#divStudentBanner",
        "[id*='Student'][id*='Picker']",
        "[class*='student'][class*='picker']",
        "a:has-text('Student')", "button:has-text('Student')",
        "a:has-text('Students')", "button:has-text('Students')",
    ]
    for sel in triggers:
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=1200)
                time.sleep(0.3)  # open dropdown
                return True
        except Exception:
            pass
    return False

def _click_anything_with_text(page, text: str) -> bool:
    # Try increasingly broad patterns; keep order to prefer obvious clickables.
    selectors = [
        f"a:has-text('{text}')",
        f"button:has-text('{text}')",
        f"[role='menuitem']:has-text('{text}')",
        f"[role='link']:has-text('{text}')",
        f"[role='button']:has-text('{text}')",
        f"li:has-text('{text}')",
        f"div:has-text('{text}') span",
        f"div:has-text('{text}')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.scroll_into_view_if_needed(timeout=1500)
                loc.click(timeout=1500)
                return True
        except Exception:
            continue
    # Playwright text engine (case-insensitive partial)
    try:
        loc = page.get_by_text(text, exact=False).first
        if loc.count():
            loc.click(timeout=1500)
            return True
    except Exception:
        pass
    return False

def _switch_to_student(page, student_name: str) -> bool:
    """
    Try a few strategies:
      1) If a student menu exists, open it, then click the student's name.
      2) Without a menu, click any visible element that already shows the student's name.
    """
    # Strategy 1: open student menu and pick
    opened = _open_possible_student_menu(page)
    if opened:
        if _click_anything_with_text(page, student_name):
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            return True

    # Strategy 2: try to click the name directly on the page
    if _click_anything_with_text(page, student_name):
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        return True

    return False

def _ensure_assignments_ready(page) -> bool:
    # Wait for at least one assignment table to show up (ids tend to be tblAssign_*)
    try:
        page.wait_for_selector("table[id^='tblAssign_'], #SP_Assignments table", timeout=10000, state="visible")
        return True
    except PWTimeout:
        return False

def _extract_from_visible_tables(page, student: str) -> Tuple[List[List[str]], int]:
    rows: List[List[str]] = []
    table_ids = []
    # Collect all assignment-like tables
    for loc in page.locator("table[id^='tblAssign_'], #SP_Assignments table").all():
        try:
            tid = loc.get_attribute("id") or "<no-id>"
            table_ids.append(tid)
            # Parse each row
            for tr in loc.locator("tbody tr").all():
                tds = [t.inner_text().strip() for t in tr.locator("td").all()]
                if not tds or len(tds) < 8:
                    continue
                # Map columns best-effort; adapt to your actual layout
                # Example expectation: [Course, Assignment, AssignedDate, DueDate, PtsPossible, Score, Pct, Status, Teacher? ...]
                course = tds[0]
                assignment = tds[1]
                assigned = tds[2] if len(tds) > 2 else ""
                due = tds[3] if len(tds) > 3 else ""
                pts_possible = tds[4] if len(tds) > 4 else ""
                score = tds[5] if len(tds) > 5 else ""
                pct = tds[6] if len(tds) > 6 else ""
                status = tds[7] if len(tds) > 7 else ""
                teacher = ""  # often shown above table; unknown here
                period = ""   # unknown; can be parsed from course if needed
                comments = ""
                url = page.url

                rows.append([
                    student, period, course, teacher, due, assigned,
                    assignment, pts_possible, score, pct, status, comments, url
                ])
        except Exception:
            continue
    print(f"DEBUG — found assignment tables (ids): {table_ids}")
    return rows, len(table_ids)

# ---------- main entry

def run_scrape(username: str, password: str, students: List[str]) -> Tuple[List[List[str]], dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context()
        page = context.new_page()

        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
        print(f"DEBUG — landed: {page.url}")

        _dismiss_timeout_dialog_if_present(page)
        _login_if_needed(page, username, password)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        print("DEBUG — after login: portal loaded")
        all_rows: List[List[str]] = []
        metrics = {"students": {}}

        # Two small UI snapshots around the time we try to switch
        _log_ui_snapshot(page)

        for s in students:
            ok = _switch_to_student(page, s)
            if not ok:
                _log_ui_snapshot(page)  # one more sample after attempts
                print(f"DEBUG — could not locate a picker for '{s}'; skipping switch")
                print(f"DEBUG — skipping extraction for '{s}' (could not switch)")
                metrics["students"][s] = {"switched": False, "tables": 0, "rows": 0}
                continue

            # If we did switch, wait for assignments to be present
            ready = _ensure_assignments_ready(page)
            if not ready:
                print("DEBUG — assignments section not found/ready; skipping.")
                metrics["students"][s] = {"switched": True, "tables": 0, "rows": 0}
                continue

            rows, tablecount = _extract_from_visible_tables(page, s)
            metrics["students"][s] = {"switched": True, "tables": tablecount, "rows": len(rows)}
            all_rows.extend(rows)

        print(f"DEBUG — scraped {len(all_rows)} rows from portal")
        context.close()
        browser.close()
        return all_rows, metrics
