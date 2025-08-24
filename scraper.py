from __future__ import annotations

import re
from datetime import date
from typing import List, Dict, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE_URL = "https://parentportal.cajonvalley.net"

# --------- utilities --------- #
def dprint(*args):
    print("DEBUG —", *args, flush=True)


def norm_students(students_csv_or_list) -> list[str]:
    """Accepts 'Adrian,Jacob' or ['Adrian','Jacob'] or ('Adrian','Jacob')."""
    if isinstance(students_csv_or_list, (list, tuple, set)):
        return [str(s).strip() for s in students_csv_or_list if str(s).strip()]
    return [s.strip() for s in str(students_csv_or_list or "").split(",") if s.strip()]


def pct_to_float(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
    return float(m.group(1)) if m else None


def cell_text_safe(locator) -> str:
    try:
        return locator.inner_text().strip()
    except PWTimeoutError:
        return ""
    except Exception:
        return ""


# --------- core page helpers --------- #
def login(page, username: str, password: str) -> None:
    """
    Land on /, fill #Pin and #Password, click #LoginButton and wait for portal shell.
    """
    dprint("landed:", f"{BASE_URL}/")
    page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")

    pin = page.locator("#Pin")
    pwd = page.locator("#Password")
    btn = page.locator("#LoginButton")

    pin.wait_for(state="visible", timeout=20000)
    pwd.wait_for(state="visible", timeout=20000)
    dprint("login fields visible:", True, True)

    pin.fill(username)
    pwd.fill(password)
    btn.click()

    page.wait_for_url(re.compile(r"/Home/PortalMainPage"), timeout=30000)
    page.wait_for_selector("#SP-MainDiv", timeout=30000)
    dprint("after login: portal loaded")


def build_student_map(page) -> Dict[str, Tuple[str, str]]:
    """
    Build a map of names to (stuuniq, sid).
      - stuuniq: value used in SetStudentBanner URL (data-stuuniq)
      - sid: the numeric student id reflected in #hStudentID and in tile id suffix
    We map nickname, full name, and a convenient first name token.
    """
    page.wait_for_selector("#divStudentBanner", timeout=20000)

    tiles = page.locator("#divSelectStudent .studentTile")
    count = tiles.count()

    m: Dict[str, Tuple[str, str]] = {}
    for i in range(count):
        t = tiles.nth(i)
        stuuniq = t.get_attribute("data-stuuniq") or ""
        tile_id = t.get_attribute("id") or ""
        # tile id is like "stuTile_357342" -> capture the numeric piece
        sid_match = re.search(r"stuTile_(\d+)", tile_id)
        sid = sid_match.group(1) if sid_match else ""

        nick = cell_text_safe(t.locator(".tileStudentNickname"))
        full_name = cell_text_safe(t.locator(".tileStudentName"))

        if not (stuuniq and sid):
            continue

        if nick:
            m[nick] = (stuuniq, sid)
        if full_name:
            m[full_name] = (stuuniq, sid)
            # also map a handy first token if it helps
            first = full_name.split(",")[-1].strip() if "," in full_name else full_name.split()[0]
            if first:
                m.setdefault(first, (stuuniq, sid))

    dprint("saw student tiles:", bool(m))
    return m


def set_student(page, stuuniq: str, expect_sid: str) -> None:
    """
    Switch the active student using stuuniq in the URL, then wait for #hStudentID to show sid.
    """
    page.goto(f"{BASE_URL}/StudentBanner/SetStudentBanner/{stuuniq}", wait_until="domcontentloaded")
    page.wait_for_selector(f'#hStudentID[value="{expect_sid}"]', timeout=15000)


def ensure_assignments_loaded(page) -> None:
    """
    The 'Assignments' section content is placed in #SP_Assignments when loaded.
    """
    page.wait_for_selector("#SP-MainDiv", timeout=15000)
    page.wait_for_selector("#SP_Assignments", timeout=20000)
    dprint("nav_root for Assignments exists:", True)
    page.wait_for_timeout(250)


def parse_assignments_for_student(page, student_name: str) -> List[List[str]]:
    """
    Scrape per-class tables inside #SP_Assignments.
    Returns: [date, student, course, due_date, assignment, score_pct, category]
    """
    ensure_assignments_loaded(page)

    root = page.locator("#SP_Assignments")
    tables = root.locator('table[id^="tblAssign_"]')
    tcount = tables.count()

    out_rows: List[List[str]] = []
    today = date.today().isoformat()

    for ti in range(tcount):
        tbl = tables.nth(ti)

        course = cell_text_safe(tbl.locator("caption")).strip()
        course = re.sub(r"^\s*Per\s*:\s*\S+\s*", "", course)

        body_rows = tbl.locator("tbody > tr")
        for ri in range(body_rows.count()):
            r = body_rows.nth(ri)

            if "No Assignments" in r.inner_text():
                continue

            cls = r.get_attribute("class") or ""
            is_missing = "missingAssignment" in cls

            tds = r.locator("td")

            due = ""
            assign = ""
            pct_txt = ""

            ddate_el = r.locator('td[id^="ddate"]')
            desc_el = r.locator('td[id^="descript"]')

            if ddate_el.count() > 0:
                due = cell_text_safe(ddate_el)
            elif tds.count() >= 2:
                due = cell_text_safe(tds.nth(1))

            if desc_el.count() > 0:
                assign = cell_text_safe(desc_el)
            elif tds.count() >= 4:
                assign = cell_text_safe(tds.nth(3))

            if tds.count() >= 7:
                pct_txt = cell_text_safe(tds.nth(6))

            pct_val = pct_to_float(pct_txt)
            category = "OK"
            if is_missing:
                category = "MISSING"
            elif pct_val is None or pct_val == 0:
                category = "LOW"
            elif pct_val < 70:
                category = "LOW"
            elif pct_val >= 90:
                category = "WIN"

            out_rows.append([today, student_name, course, due, assign, (pct_txt or "").strip(), category])

    return out_rows


# --------- public entrypoint --------- #
def run_scrape(username: str, password: str, students_csv_or_list) -> List[List[str]]:
    """
    - Logs in
    - Resolves requested student names to (stuuniq, sid)
    - For each, switches active student and scrapes Assignments
    """
    students = norm_students(students_csv_or_list)
    all_rows: List[List[str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.set_default_timeout(20000)

        try:
            login(page, username, password)
        except PWTimeoutError as e:
            dprint("login failed (timeout):", str(e))
            browser.close()
            return all_rows

        stu_map = build_student_map(page)

        for student in students:
            # case-insensitive key match
            entry = next((v for k, v in stu_map.items() if k.lower() == student.lower()), None)
            if not entry:
                dprint("tile not found for", student)
                continue

            stuuniq, sid = entry
            try:
                set_student(page, stuuniq, sid)
            except PWTimeoutError:
                dprint("failed to switch to student", student)
                continue

            student_rows = parse_assignments_for_student(page, student)
            dprint("scraped rows for", f"{student}:", len(student_rows))
            all_rows.extend(student_rows)

        browser.close()

    return all_rows
