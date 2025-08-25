from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import re
from datetime import datetime

PORTAL_URL = "https://parentportal.cajonvalley.net/"

def _text(el):
    if not el:
        return ""
    try:
        return el.inner_text().strip()
    except Exception:
        return ""

def _first(el, selector):
    try:
        return el.query_selector(selector)
    except Exception:
        return None

def login_and_wait(page, username, password):
    page.goto(PORTAL_URL, wait_until="domcontentloaded")
    pin = page.locator("#Pin")
    pw  = page.locator("#Password")

    pin_visible = pin.is_visible(timeout=10000)
    pw_visible  = pw.is_visible(timeout=10000)
    print(f"DEBUG – login fields visible: {pin_visible} {pw_visible}")

    pin.fill(username)
    pw.fill(password)
    page.click("#LoginButton")

    page.wait_for_selector("#divStudentBanner", timeout=30000)
    print("DEBUG – after login: portal loaded")

def show_student_tiles(page):
    tiles_container = page.locator("#divSelectStudent")
    if tiles_container.is_visible():
        return True
    page.locator("#openSelect").click()
    try:
        page.wait_for_selector(".studentTile", timeout=10000)
        return True
    except PWTimeout:
        return False

def build_student_tile_map(page):
    ok = show_student_tiles(page)
    print(f"DEBUG – saw student tiles: {ok}")
    mapping = {}
    if not ok:
        return mapping

    tiles = page.query_selector_all(".studentTile")
    for t in tiles:
        tile_id_full = t.get_attribute("id") or ""   # e.g. stuTile_357342
        tile_id_num  = tile_id_full.split("_")[-1]
        nick_el = t.query_selector(".tileStudentNickname")
        nickname = (_text(nick_el)).lower()
        if nickname:
            mapping[nickname] = {"tile_id": tile_id_num, "selector": f"#{tile_id_full}"}
    return mapping

def current_student_banner_id(page):
    try:
        return page.eval_on_selector("#hStudentID", "el => el.value")
    except Exception:
        return None

def switch_to_student(page, nickname, tile_map):
    target = tile_map.get(nickname.lower())
    if not target:
        print(f"DEBUG – could not find tile for {nickname}")
        return False

    target_id = target["tile_id"]
    cur_id = current_student_banner_id(page)

    if cur_id == target_id:
        print(f"DEBUG – already on student {nickname} (id {target_id})")
        if page.locator("#divSelectStudent").is_visible():
            page.locator("#openSelect").click()
        return True

    show_student_tiles(page)
    page.click(target["selector"])

    try:
        page.wait_for_function(
            """(id) => document.querySelector('#hStudentID') && document.querySelector('#hStudentID').value === id""",
            arg=target_id,
            timeout=15000
        )
        if page.locator("#divSelectStudent").is_visible():
            page.locator("#openSelect").click()
        print(f"DEBUG – switched to student {nickname}")
        return True
    except PWTimeout:
        print(f"DEBUG – failed to switch to student {nickname}")
        return False

def ensure_assignments_ready(page):
    """
    Open the Assignments area and wait for either a tablecount marker
    or any assignment table to appear.
    """
    # Ensure the menu row exists; click to focus/open
    menu_row = page.locator("tr#Assignments")
    if menu_row.count():
        try:
            menu_row.click()
        except Exception:
            pass

    # Make sure the content area exists
    try:
        page.wait_for_selector("#trSP1_Assignments, #SP_Assignments", timeout=10000)
    except PWTimeout:
        return False

    # IMPORTANT: do NOT mix `text=` engine with CSS in a single selector.
    # Wait for either the hidden count or any table within Assignments.
    try:
        page.locator("input#tablecount, #SP_Assignments table.tblassign, table.tblassign").first.wait_for(timeout=10000)
    except PWTimeout:
        # Small grace period; some pages are just slow to render the rows.
        page.wait_for_timeout(750)

    # Log tablecount if present
    tablecount_val = "?"
    try:
        tablecount_val = page.eval_on_selector("input#tablecount", "el => el.value")
    except Exception:
        pass
    print(f"DEBUG – tablecount marker: {tablecount_val}")

    # Give layout a beat
    page.wait_for_timeout(250)
    return True

def collect_assignment_tables(page):
    """
    Return a de-duplicated list of assignment table elements using multiple selectors.
    """
    selectors = [
        "#SP_Assignments table.tblassign",
        "table.tblassign",
        "table[id^='tblAssign_']",
    ]
    seen = set()
    tables = []
    for sel in selectors:
        try:
            for el in page.query_selector_all(sel):
                rid = el.get_attribute("id") or f"ptr-{id(el)}"
                if rid in seen:
                    continue
                seen.add(rid)
                tables.append(el)
        except Exception:
            pass

    ids = [el.get_attribute("id") or "(no id)" for el in tables]
    print(f"DEBUG – found assignment tables (ids): {ids}")
    return tables

PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")

def extract_assignments_for_student(page, student_name):
    """
    Returns (rows, counts)
      rows: [timestamp, student, course, assignment, due_date, pct, status]
      counts: {'flags': missing+low, 'wins': high}
    """
    ok = ensure_assignments_ready(page)
    if not ok:
        print(f"DEBUG – Assignments panel not ready for {student_name}")
        return [], {"flags": 0, "wins": 0}

    tables = collect_assignment_tables(page)
    print(f"DEBUG – class tables for {student_name}: {len(tables)}")

    out_rows = []
    missing_or_low = 0
    wins = 0
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for tbl in tables:
        # Course name from caption
        cap = _first(tbl, "caption")
        course_text = _text(cap)
        if course_text and "Per" in course_text and ":" in course_text:
            course = course_text.split(":", 1)[1].strip()
        else:
            course = course_text or ""

        # Body rows
        rows = tbl.query_selector_all("tbody > tr")
        for tr in rows:
            tr_text = _text(tr)
            if "No Assignments Available" in tr_text:
                continue

            tr_class = (tr.get_attribute("class") or "").lower()
            is_missing = "missingassignment" in tr_class

            due_el = _first(tr, 'td[id^="ddate"]')
            desc_el = _first(tr, 'td[id^="descript"]')
            due_date = _text(due_el)
            assignment = _text(desc_el)

            # Find a % anywhere in the row
            pct = ""
            for td in tr.query_selector_all("td"):
                m = PCT_RE.search(_text(td))
                if m:
                    pct = m.group(1)
                    break

            status = ""
            if is_missing:
                status = "MISSING"
                missing_or_low += 1
            elif pct:
                try:
                    p = float(pct)
                    if p >= 90.0:
                        status = "WIN"
                        wins += 1
                    elif p < 70.0:
                        status = "LOW"
                        missing_or_low += 1
                except Exception:
                    pass

            if status:
                out_rows.append([ts, student_name, course, assignment, due_date, pct, status])

    return out_rows, {"flags": missing_or_low, "wins": wins}

def run_scrape(username, password, students):
    """
    Orchestrates login, per-student switching, and scraping.
    Returns (rows, metrics)
    """
    all_rows = []
    total_flags = 0
    total_wins = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ])
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(15000)

        login_and_wait(page, username, password)
        tile_map = build_student_tile_map(page)

        for s in students:
            switched = switch_to_student(page, s, tile_map)
            if not switched:
                print(f"DEBUG – failed to switch to student {s}")
                continue

            rows, counts = extract_assignments_for_student(page, s)
            all_rows.extend(rows)
            total_flags += counts.get("flags", 0)
            total_wins += counts.get("wins", 0)

        context.close()
        browser.close()

    return all_rows, {"flags": total_flags, "wins": total_wins}
