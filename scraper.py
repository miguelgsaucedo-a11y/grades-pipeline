from __future__ import annotations

import re
from typing import List, Dict

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


BASE_URL = "https://parentportal.cajonvalley.net/"


# --------------------------
# Helpers / small utilities
# --------------------------

def _visible(page, selector: str) -> bool:
    try:
        return page.is_visible(selector)
    except Exception:
        return False


def _first_visible_selector(page, selectors: List[str]) -> str | None:
    for sel in selectors:
        try:
            if page.is_visible(sel):
                return sel
        except Exception:
            pass
    return None


def _text(el) -> str:
    try:
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def _clean_space(s: str) -> str:
    return " ".join((s or "").split())


# --------------------------
# Login & student switching
# --------------------------

def login(page, username: str, password: str) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    print(f"DEBUG — landed: {page.url}")

    # Try several possible login field selectors (portal UIs sometimes vary).
    user_selectors = [
        'input[name="username"]',
        'input[name="UserName"]',
        "#UserName",
        "#username",
    ]
    pass_selectors = [
        'input[name="password"]',
        'input[name="Password"]',
        "#Password",
        "#password",
    ]
    submit_selectors = [
        'input[type="submit"]',
        'button[type="submit"]',
        'button:has-text("Sign In")',
        'input[value*="Sign In"]',
    ]

    user_sel = _first_visible_selector(page, user_selectors)
    pwd_sel = _first_visible_selector(page, pass_selectors)

    print(f"DEBUG — login fields visible: {bool(user_sel and pwd_sel)}")
    if not (user_sel and pwd_sel):
        # Already signed in
        pass
    else:
        page.fill(user_sel, username)
        page.fill(pwd_sel, password)

        sub_sel = _first_visible_selector(page, submit_selectors)
        if sub_sel:
            page.click(sub_sel)
        else:
            page.keyboard.press("Enter")

    # After login, the main portal content should load
    page.wait_for_load_state("domcontentloaded")
    print("DEBUG — after login: portal loaded")

    # Quick sanity: student banner present?
    page.wait_for_selector("#divStudentBanner", timeout=15000)
    # Often tiles are lazy; we’ll just note whether we can see them when opened later
    print(f"DEBUG — saw student tiles: {_visible(page, '.studentTile')}")


def open_student_picker(page) -> None:
    # Open the student picker panel
    # The "hamburger" in the banner can be clicked via the #openSelect cell
    try:
        page.click("#openSelect", timeout=3000)
    except Exception:
        pass
    # Ensure it's visible
    try:
        page.wait_for_selector("#divSelectStudent", state="visible", timeout=5000)
    except Exception:
        # As a fallback, click the family icon itself.
        try:
            page.click("#imgStudents", timeout=2000)
            page.wait_for_selector("#divSelectStudent", state="visible", timeout=5000)
        except Exception:
            pass


def switch_to_student(page, nickname_or_name: str) -> None:
    open_student_picker(page)

    # Pick a tile containing the nickname or full name.
    # We filter by text so we don't need the numeric IDs.
    tile = page.locator(".studentTile").filter(has_text=nickname_or_name)
    count = tile.count()
    if count == 0:
        # Try case-insensitive substring match by scanning tiles
        all_tiles = page.locator(".studentTile")
        for i in range(all_tiles.count()):
            t = all_tiles.nth(i)
            t_text = _text(t)
            if nickname_or_name.lower() in t_text.lower():
                tile = t
                count = 1
                break

    if count == 0:
        raise RuntimeError(f"Could not find student tile for '{nickname_or_name}'")

    # Click the first match
    tile.first.click()

    # Wait for the main area to settle
    page.wait_for_load_state("domcontentloaded")
    # Make sure the Assignments area is present (open by default in your portal)
    page.wait_for_selector("#SP1_Assignments", timeout=10000)
    print(f"DEBUG — switched to student {nickname_or_name}")


# --------------------------
# Assignments extraction
# --------------------------

def ensure_assignments_ready(page, timeout: int = 10000) -> None:
    """
    Make sure the Assignments area is loaded.

    Strategy:
    1) Wait for the Assignments container to exist.
    2) Prefer the hidden '#tablecount' marker (present in your HTML) — this flips to a value
       like '3' when tables are added.
    3) If that's missing, accept either a visible assignments table OR the text
       'No Assignments Available'.
    """
    # 1) The container exists
    page.wait_for_selector("#SP1_Assignments", timeout=timeout)

    # 2) Try the hidden 'tablecount' marker first
    try:
        page.wait_for_selector("#SP_Assignments >> input#tablecount", timeout=timeout)
        return
    except PWTimeout:
        pass

    # 3) Fallbacks: either a table exists or the "no assignments" text exists
    try:
        page.wait_for_selector("#SP_Assignments table.tblassign", state="visible", timeout=timeout)
        return
    except PWTimeout:
        pass

    try:
        # IMPORTANT: keep text= by itself (no CSS commas)
        page.wait_for_selector('text=No Assignments Available', timeout=timeout)
        return
    except PWTimeout:
        pass

    # If we’re here, nothing became visible in time.
    raise PWTimeout("Assignments area did not become ready")


def parse_period_and_course(caption_text: str) -> (str, str):
    """
    The caption looks like: "Per: 2   CC Math 6 (T2201YF1)"
    Return ("2", "CC Math 6 (T2201YF1)")
    """
    t = _clean_space(caption_text)
    m = re.search(r"Per\s*:\s*([A-Za-z0-9]+)\s+(.*)$", t)
    if m:
        return m.group(1), m.group(2)
    # Fallbacks
    if ":" in t:
        left, right = t.split(":", 1)
        return _clean_space(right).split(" ", 1)[0], _clean_space(right)
    return "", t


def extract_assignments_for_student(page, student_name: str) -> List[Dict[str, str]]:
    ensure_assignments_ready(page)

    # tablecount marker (if available) — handy for debugging
    try:
        marker = page.eval_on_selector(
            "#SP_Assignments >> input#tablecount",
            "el => el.value"
        )
        print(f"DEBUG — tablecount marker: {marker}")
    except Exception:
        print("DEBUG — tablecount marker: (not found)")

    # All the period tables
    table_ids = page.eval_on_selector_all(
        '#SP_Assignments table[id^="tblAssign_"]',
        "els => els.map(e => e.id)"
    )
    if isinstance(table_ids, list):
        print(f"DEBUG — found assignment tables (ids): {table_ids}")

    out: List[Dict[str, str]] = []

    for tid in table_ids:
        cap_sel = f"#{tid} caption"
        caption = ""
        try:
            caption = page.inner_text(cap_sel).strip()
        except Exception:
            pass

        period, course_name = parse_period_and_course(caption)

        # Each row in tbody is an assignment row (or a single 'No Assignments Available' row)
        rows = page.query_selector_all(f"#{tid} tbody > tr")
        for r in rows:
            tds = r.query_selector_all("td")
            if not tds:
                continue

            # Single-cell "No Assignments Available" (colspan) row
            if len(tds) == 1:
                txt = _text(tds[0]).lower()
                if "no assignments available" in txt:
                    continue

            # Defensive indexing — the Assignments table has these columns:
            # Detail | Date Due | Assigned | Assignment | Pts Possible | Score | Pct Score | Scored As | Extra Credit | Not Graded | Comments
            def cell(i):
                try:
                    return _clean_space(_text(tds[i]))
                except Exception:
                    return ""

            due_date     = cell(1)
            assigned     = cell(2)
            assignment   = cell(3)   # this is the assignment title
            pts_possible = cell(4)
            score        = cell(5)
            pct          = cell(6)
            comments     = cell(10) if len(tds) > 10 else ""

            # Determine status
            classes = (r.get_attribute("class") or "").split()
            status = "MISSING" if "missingAssignment" in classes else ""
            if not status:
                pct_num = pct.replace("%", "").strip()
                if pct_num.isdigit() and pct_num == "100":
                    status = "WIN"
                elif pts_possible and score and pts_possible == score:
                    status = "WIN"

            out.append({
                # Sheet columns (match HEADERS in main.py)
                "ImportedAt": "",             # filled by main.py
                "Student": student_name,
                "Period": period if course_name == "" else f"{period}  {course_name}",
                "Course": assignment,         # the assignment title goes in 'Course' column in your sheet layout
                "Teacher": "",                # available in header if you want to pull later
                "DueDate": due_date,
                "AssignedDate": assigned,
                "Assignment": "",
                "PtsPossible": pts_possible,
                "Score": score,
                "Pct": pct,
                "Status": status,
                "Comments": comments,
                "SourceURL": "",
            })

    return out


# --------------------------
# Public entry point
# --------------------------

def run_scrape(username: str, password: str, students: List[str]) -> List[Dict[str, str]]:
    """
    Log in, loop over students, collect assignment rows.
    Returns a list of sheet-ready dicts (no metrics tuple).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-gpu"])
        context = browser.new_context()
        page = context.new_page()

        login(page, username, password)

        all_rows: List[Dict[str, str]] = []

        for s in students:
            switch_to_student(page, s)

            rows = extract_assignments_for_student(page, s)
            print(f"DEBUG — class tables for {s}: {len(rows)}")

            all_rows.extend(rows)

        browser.close()

        return all_rows
