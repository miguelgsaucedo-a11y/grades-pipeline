from __future__ import annotations

import re
from typing import List, Dict, Optional
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL = "https://parentportal.cajonvalley.net/"


# --------------------------
# Tiny helpers
# --------------------------

def _visible(page, selector: str) -> bool:
    try:
        return page.is_visible(selector)
    except Exception:
        return False


def _first_visible(page, sels: List[str]) -> Optional[str]:
    for s in sels:
        if _visible(page, s):
            return s
    return None


def _wait_one_of(page, sels: List[str], timeout_each: int = 3000) -> Optional[str]:
    for s in sels:
        try:
            page.wait_for_selector(s, state="visible", timeout=timeout_each)
            return s
        except Exception:
            pass
    return None


def _txt(el) -> str:
    try:
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def _clean(s: str) -> str:
    return " ".join((s or "").split())


def _log_dom_probe(page, label: str) -> None:
    try:
        url = page.url
    except Exception:
        url = "(no url)"
    try:
        title = page.title()
    except Exception:
        title = "(no title)"
    print(f"DEBUG — {label}: url={url} | title={title}")
    # First ~300 chars of visible body text — helpful to see what page we’re on.
    try:
        sample = page.locator("body").inner_text(timeout=1000)
        sample = _clean(sample)[:300]
        print(f"DEBUG — {label}: body≈ \"{sample}\"")
    except Exception:
        pass


# --------------------------
# Navigation / login
# --------------------------

def goto_home(page) -> None:
    # Force main portal page
    page.goto(BASE_URL + "Home/PortalMainPage", wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    _log_dom_probe(page, "after login nav")

    # Accept a broader set of “I’m on the portal” markers
    portal_markers = [
        "#SP_Assignments", "#SP1_Assignments",
        "[id*='_Assignments']", "[id*='Assignments']",
        "#openSelect", "#imgStudents", ".studentTile",
        "#divSelectStudent", "#divStudentBanner"
    ]
    seen = _wait_one_of(page, portal_markers, timeout_each=2000)
    print(f"DEBUG — portal marker seen: {seen or 'None'}")


def login(page, username: str, password: str) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    print(f"DEBUG — landed: {page.url}")

    user_sels = ['input[name="username"]', 'input[name="UserName"]', "#UserName", "#username"]
    pass_sels = ['input[name="password"]', 'input[name="Password"]', "#Password", "#password"]
    submit_sels = ['input[type="submit"]', 'button[type="submit"]',
                   'button:has-text("Sign In")', 'input[value*="Sign In"]']

    user_sel = _first_visible(page, user_sels)
    pwd_sel  = _first_visible(page, pass_sels)
    print(f"DEBUG — login fields visible: {bool(user_sel and pwd_sel)}")

    if user_sel and pwd_sel:
        page.fill(user_sel, username)
        page.fill(pwd_sel, password)
        sub_sel = _first_visible(page, submit_sels)
        if sub_sel:
            page.click(sub_sel)
        else:
            page.keyboard.press("Enter")

    goto_home(page)


# --------------------------
# Student helpers
# --------------------------

def _open_student_picker_if_any(page) -> None:
    for s in ["#openSelect", "#imgStudents", "a:has-text('Students')", "button:has-text('Students')"]:
        if _visible(page, s):
            try:
                page.click(s, timeout=1500)
                break
            except Exception:
                pass
    try:
        page.wait_for_selector("#divSelectStudent, .studentTile", timeout=2000)
    except Exception:
        pass


def _pick_from_dropdown(page, target: str) -> bool:
    try:
        selects = page.query_selector_all("select")
    except Exception:
        selects = []
    for sel in selects or []:
        try:
            opts = sel.query_selector_all("option")
            for o in opts:
                label = _txt(o)
                if target.lower() in (label or "").lower():
                    value = o.get_attribute("value") or ""
                    if value:
                        page.select_option(sel, value=value)
                    else:
                        page.select_option(sel, label=label)
                    page.wait_for_load_state("networkidle")
                    print(f"DEBUG — switched via dropdown to {target}")
                    return True
        except Exception:
            continue
    return False


def _current_student_name(page) -> Optional[str]:
    probes = [
        "#divStudentBanner",
        "#studentBanner",
        "header:has-text('Student')",
        "a#openSelect",
        ".studentTile.selected",
    ]
    for p in probes:
        try:
            if page.is_visible(p):
                txt = page.inner_text(p).strip()
                # Heuristic: shortest “Name (ID…)” / “Name - …”
                txt = _clean(txt)
                if txt:
                    # Keep just the first two words to avoid long banners
                    parts = txt.split()
                    if len(parts) >= 1:
                        return " ".join(parts[:3])
        except Exception:
            pass
    return None


def switch_to_student(page, target: str) -> bool:
    _open_student_picker_if_any(page)

    tiles = page.locator(".studentTile")
    try:
        count = tiles.count()
    except Exception:
        count = 0

    if count and count > 0:
        # Prefer exact-ish match, otherwise fuzzy contains
        cand = tiles.filter(has_text=target)
        if cand.count() == 0:
            for i in range(min(count, 12)):  # don’t iterate endlessly
                t = tiles.nth(i)
                if target.lower() in _txt(t).lower():
                    cand = t
                    break
        try:
            (cand if hasattr(cand, "click") else cand.first).click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_load_state("networkidle")
            print(f"DEBUG — switched to student {target} (tiles)")
            return True
        except Exception:
            pass

    if _pick_from_dropdown(page, target):
        return True

    # If we can't switch, we’ll continue with whatever student is active.
    print(f"DEBUG — could not locate a picker for '{target}'; skipping switch")
    return False


# --------------------------
# Assignments extraction
# --------------------------

def _find_assignments_root(page) -> Optional[str]:
    # Return the id of any Assignments container
    try:
        rid = page.eval_on_selector(
            "#SP_Assignments, #SP1_Assignments, [id*='_Assignments'], [id*='Assignments']",
            "el => el && el.id ? el.id : null"
        )
        return rid
    except Exception:
        return None


def ensure_assignments_ready(page, timeout: int = 10000) -> None:
    rid = _find_assignments_root(page)
    if not rid:
        # Sometimes needs a small scroll bump or a short wait
        try:
            page.mouse.wheel(0, 400)
        except Exception:
            pass
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        rid = _find_assignments_root(page)

    if not rid:
        # No container at all; maybe a blank dashboard or maintenance page.
        raise PWTimeout("Assignments container not found")

    # “ready” if one of: hidden #tablecount, a visible table, or the no-assignments text
    sel_hidden_marker = f"#{rid} >> input#tablecount"
    sel_tbl_visible   = f"#{rid} table.tblassign, #{rid} table[id^='tblAssign_']"

    for s in [sel_hidden_marker, sel_tbl_visible]:
        try:
            page.wait_for_selector(s, timeout=timeout)
            return
        except PWTimeout:
            pass

    try:
        page.get_by_text("No Assignments Available", exact=False).wait_for(state="visible", timeout=timeout)
        return
    except Exception:
        pass

    raise PWTimeout("Assignments area did not become ready")


def _parse_period_and_course(caption: str) -> (str, str):
    t = _clean(caption)
    m = re.search(r"Per\s*:\s*([A-Za-z0-9]+)\s+(.*)$", t)
    if m:
        return m.group(1), m.group(2)
    if ":" in t:
        _, right = t.split(":", 1)
        right = _clean(right)
        parts = right.split(" ", 1)
        return (parts[0] if parts else ""), right
    return "", t


def extract_assignments_for_student(page, student_name: str) -> List[Dict[str, str]]:
    ensure_assignments_ready(page)

    rid = _find_assignments_root(page) or "SP_Assignments"
    try:
        marker = page.eval_on_selector(f"#{rid} >> input#tablecount", "el => el && el.value ? el.value : ''")
        print(f"DEBUG — tablecount marker: {marker or '(n/a)'}")
    except Exception:
        print("DEBUG — tablecount marker: (n/a)")

    table_ids = page.eval_on_selector_all(
        f"#{rid} table[id^='tblAssign_']",
        "els => els.map(e => e.id)"
    ) or []
    print(f"DEBUG — found assignment tables (ids): {table_ids}")

    rows: List[Dict[str, str]] = []

    for tid in table_ids:
        cap_sel = f"#{tid} caption"
        caption = ""
        try:
            caption = page.inner_text(cap_sel).strip()
        except Exception:
            pass

        period, course_name = _parse_period_and_course(caption)

        for r in page.query_selector_all(f"#{tid} tbody > tr"):
            tds = r.query_selector_all("td")
            if not tds:
                continue
            if len(tds) == 1 and "no assignments available" in _txt(tds[0]).lower():
                continue

            def cell(i):
                try:
                    return _clean(_txt(tds[i]))
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

            rows.append({
                "ImportedAt": "",  # filled in main
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

    return rows


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
        extracted_students: set[str] = set()

        for s in students:
            switched = switch_to_student(page, s)

            # Try to learn who is currently selected (even if switch failed)
            curr = _current_student_name(page) or s
            if switched:
                extracted_students.add(curr)

            # Attempt extraction anyway—if we truly aren’t on a student,
            # ensure_assignments_ready will raise and we just skip.
            try:
                rows = extract_assignments_for_student(page, curr)
                print(f"DEBUG — class tables for {curr}: {len(rows)}")
                if rows:
                    all_rows.extend(rows)
            except Exception as e:
                print(f"DEBUG — skipping extraction for '{s}' ({'could not switch' if not switched else str(e)})")

        browser.close()
        return all_rows
