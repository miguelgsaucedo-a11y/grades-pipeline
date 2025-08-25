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
    """
    Try each selector in order; return the first that becomes visible.
    """
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
# Login & student switching
# --------------------------

def login(page, username: str, password: str) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    print(f"DEBUG — landed: {page.url}")

    # Likely login field variants
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

    # Let navigation settle and then accept *any* known portal element
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_load_state("networkidle")

    # Any of these indicates the main app is present
    portal_markers = [
        "#SP1_Assignments",
        "#SP_Assignments",
        "#openSelect",
        "#imgStudents",
        ".studentTile",
    ]
    seen = _wait_for_one_of(page, portal_markers, timeout_each=3000)
    print("DEBUG — after login: portal loaded")
    if seen:
        print(f"DEBUG — saw portal marker: {seen}")
    else:
        print("DEBUG — portal markers not immediately visible (continuing anyway)")

    print(f"DEBUG — saw student tiles: {_visible(page, '.studentTile')}")


def open_student_picker(page) -> None:
    # Try to open the student picker (hamburger/chevron next to student)
    for sel in ["#openSelect", "#imgStudents"]:
        try:
            page.click(sel, timeout=2000)
            break
        except Exception:
            pass

    try:
        page.wait_for_selector("#divSelectStudent", state="visible", timeout=5000)
    except Exception:
        # It might already be selected/visible; continue
        pass


def switch_to_student(page, nickname_or_name: str) -> None:
    open_student_picker(page)

    tile = page.locator(".studentTile").filter(has_text=nickname_or_name)
    if tile.count() == 0:
        # Fallback scan for case-insensitive substring match
        all_tiles = page.locator(".studentTile")
        for i in range(all_tiles.count()):
            t = all_tiles.nth(i)
            if nickname_or_name.lower() in _text(t).lower():
                tile = t
                break

    if (getattr(tile, "count", lambda: 1)() == 0):
        raise RuntimeError(f"Could not find student tile for '{nickname_or_name}'")

    tile.first.click()
    page.wait_for_load_state("domcontentloaded")
    _wait_for_one_of(page, ["#SP1_Assignments", "#SP_Assignments"], timeout_each=4000)
    print(f"DEBUG — switched to student {nickname_or_name}")


# --------------------------
# Assignments extraction
# --------------------------

def ensure_assignments_ready(page, timeout: int = 10000) -> None:
    """
    Make sure Assignments area is loaded.
    Strategy:
      - Wait for either #SP1_Assignments or #SP_Assignments to exist
      - Prefer hidden #tablecount marker if present
      - Otherwise accept either a visible assignments table or 'No Assignments Available'
    """
    # Container exists
    try:
        page.wait_for_selector("#SP1_Assignments, #SP_Assignments", timeout=timeout)
    except PWTimeout:
        # As a last resort, keep going — some UIs rename containers
        pass

    # Hidden tablecount marker first
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

    # Or the explicit "No Assignments" text
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
                "Course": assignment,      # aligns with your sheet layout
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
            switch_to_student(page, s)
            rows = extract_assignments_for_student(page, s)
            print(f"DEBUG — class tables for {s}: {len(rows)}")
            all_rows.extend(rows)

        browser.close()
        return all_rows
