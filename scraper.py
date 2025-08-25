from __future__ import annotations

import re
from typing import List, Dict, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


BASE_URL = "https://parentportal.cajonvalley.net/"


# --------------------------
# Small helpers
# --------------------------

def _visible(page, selector: str) -> bool:
    try:
        return page.is_visible(selector)
    except Exception:
        return False


def _first_visible_selector(page, selectors: List[str]) -> Optional[str]:
    for sel in selectors:
        try:
            if page.is_visible(sel):
                return sel
        except Exception:
            pass
    return None


def _wait_for_one_of(page, selectors: List[str], timeout_each: int = 3000) -> Optional[str]:
    """Try each selector in order; return the first that becomes visible."""
    for sel in selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=timeout_each)
            return sel
        except Exception:
            continue
    return None


def _text(el) -> str:
    try:
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def _clean_space(s: str) -> str:
    return " ".join((s or "").split())


# --------------------------
# Navigation helpers
# --------------------------

def goto_home(page) -> None:
    """
    Force navigation to the main portal page and wait for any known marker.
    This handles cases where the landing page after login varies.
    """
    page.goto(BASE_URL + "Home/PortalMainPage", wait_until="domcontentloaded")

    page.wait_for_load_state("domcontentloaded")
    page.wait_for_load_state("networkidle")

    portal_markers = [
        "#SP1_Assignments",
        "#SP_Assignments",
        "#openSelect",
        "#imgStudents",
        ".studentTile",
    ]
    seen = _wait_for_one_of(page, portal_markers, timeout_each=3000)
    print("DEBUG — after login: portal loaded")
    print(f"DEBUG — saw portal marker: {seen or 'None'}")
    print(f"DEBUG — saw student tiles: {_visible(page, '.studentTile')}")


def login(page, username: str, password: str) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    print(f"DEBUG — landed: {page.url}")

    # Login variants
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
    pwd_sel  = _first_visible_selector(page, pass_selectors)
    print(f"DEBUG — login fields visible: {bool(user_sel and pwd_sel)}")

    if user_sel and pwd_sel:
        page.fill(user_sel, username)
        page.fill(pwd_sel, password)
        sub_sel = _first_visible_selector(page, submit_selectors)
        if sub_sel:
            page.click(sub_sel)
        else:
            page.keyboard.press("Enter")

    goto_home(page)


# --------------------------
# Student switching
# --------------------------

def open_student_picker(page) -> None:
    # Try to open the picker (button/avatar)
    for sel in ["#openSelect", "#imgStudents", "a:has-text('Students')", "button:has-text('Students')"]:
        try:
            if _visible(page, sel):
                page.click(sel, timeout=2000)
                break
        except Exception:
            pass

    # Wait for the tile panel if that UI exists
    try:
        page.wait_for_selector("#divSelectStudent", state="visible", timeout=3000)
    except Exception:
        pass  # not all layouts use a tile panel


def try_select_from_dropdown(page, target_name: str) -> bool:
    """
    Fallback: some layouts provide a <select> for students instead of tiles.
    Try each select and choose the option whose label contains the target_name.
    """
    try:
        selects = page.query_selector_all("select")
    except Exception:
        selects = []

    for sel in selects or []:
        try:
            options = sel.query_selector_all("option")
            labels = [(_text(o), o.get_attribute("value") or "") for o in options]
            match = None
            for lbl, val in labels:
                if target_name.lower() in (lbl or "").lower():
                    match = (lbl, val)
                    break
            if match:
                label, value = match
                # Prefer selecting by value; if empty, use label
                if value:
                    page.select_option(sel, value=value)
                else:
                    page.select_option(sel, label=label)
                page.wait_for_load_state("networkidle")
                _wait_for_one_of(page, ["#SP1_Assignments", "#SP_Assignments"], timeout_each=4000)
                print(f"DEBUG — switched via dropdown to student {target_name}")
                return True
        except Exception:
            continue
    return False


def switch_to_student(page, nickname_or_name: str) -> bool:
    """
    Returns True if we believe we're now on the requested student.
    Falls back gracefully if we can't find a picker.
    """
    open_student_picker(page)

    # Tile-based UI
    tiles = page.locator(".studentTile")
    try:
        count = tiles.count()
    except Exception:
        count = 0

    if count and count > 0:
        # Exact text match tile first
        tile = tiles.filter(has_text=nickname_or_name)
        if tile.count() == 0:
            # Fuzzy match
            for i in range(count):
                t = tiles.nth(i)
                if nickname_or_name.lower() in _text(t).lower():
                    tile = t
                    break

        # Click if we found one
        try:
            (tile if hasattr(tile, "click") else tile.first).click()
            page.wait_for_load_state("domcontentloaded")
            _wait_for_one_of(page, ["#SP1_Assignments", "#SP_Assignments"], timeout_each=4000)
            print(f"DEBUG — switched to student {nickname_or_name}")
            return True
        except Exception:
            pass

    # Dropdown fallback
    if try_select_from_dropdown(page, nickname_or_name):
        return True

    print(f"DEBUG — could not locate a picker for '{nickname_or_name}'; skipping this student")
    return False


# --------------------------
# Assignments extraction
# --------------------------

def ensure_assignments_ready(page, timeout: int = 10000) -> None:
    # Container exists
    try:
        page.wait_for_selector("#SP1_Assignments, #SP_Assignments", timeout=timeout)
    except PWTimeout:
        pass

    # Hidden marker first (fast path)
    try:
        page.wait_for_selector("#SP_Assignments >> input#tablecount", timeout=timeout)
        return
    except PWTimeout:
        pass

    # A visible table is fine
    try:
        page.wait_for_selector("#SP_Assignments table.tblassign", state="visible", timeout=timeout)
        return
    except PWTimeout:
        pass

    # Or explicit "No Assignments Available"
    try:
        page.wait_for_selector('text=No Assignments Available', timeout=timeout)
        return
    except PWTimeout:
        pass

    raise PWTimeout("Assignments area did not become ready")


def parse_period_and_course(caption_text: str) -> (str, str):
    t = _clean_space(caption_text)
    m = re.search(r"Per\s*:\s*([A-Za-z0-9]+)\s+(.*)$", t)
    if m:
        return m.group(1), m.group(2)
    if ":" in t:
        _, right = t.split(":", 1)
        right = _clean_space(right)
        parts = right.split(" ", 1)
        return (parts[0] if parts else ""), right
    return "", t


def extract_assignments_for_student(page, student_name: str) -> List[Dict[str, str]]:
    ensure_assignments_ready(page)

    try:
        marker = page.eval_on_selector("#SP_Assignments >> input#tablecount", "el => el.value")
        print(f"DEBUG — tablecount marker: {marker}")
    except Exception:
        print("DEBUG — tablecount marker: (not found)")

    table_ids = page.eval_on_selector_all(
        '#SP_Assignments table[id^="tblAssign_"]',
        "els => els.map(e => e.id)"
    ) or []
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

        for r in page.query_selector_all(f"#{tid} tbody > tr"):
            tds = r.query_selector_all("td")
            if not tds:
                continue
            if len(tds) == 1 and "no assignments available" in _text(tds[0]).lower():
                continue

            def cell(i):
                try:
                    return _clean_space(_text(tds[i]))
                except Exception:
                    return ""

            due_date     = cell(1)
            assigned     = cell(2)
            assignment   = cell(3)
            pts_possible = cell(4)
            score        = cell(5)
            pct          = cell(6)
            comments     = cell(10) if len(tds) > 10 else ""

            classes = (r.get_attribute("class") or "").split()
            status = "MISSING" if "missingAssignment" in classes else ""
            if not status:
                pct_num = pct.replace("%", "").strip()
                if pct_num.isdigit() and pct_num == "100":
                    status = "WIN"
                elif pts_possible and score and pts_possible == score:
                    status = "WIN"

            out.append({
                "ImportedAt": "",
                "Student": student_name,
                "Period": period if not course_name else f"{period}  {course_name}",
                "Course": assignment,
                "Teacher": "",
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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-gpu"])
        context = browser.new_context()
        page = context.new_page()

        login(page, username, password)

        all_rows: List[Dict[str, str]] = []
        for s in students:
            ok = switch_to_student(page, s)
            if not ok:
                print(f"DEBUG — skipping extraction for '{s}' (could not switch)")
                continue
            rows = extract_assignments_for_student(page, s)
            print(f"DEBUG — class tables for {s}: {len(rows)}")
            all_rows.extend(rows)

        browser.close()
        return all_rows
