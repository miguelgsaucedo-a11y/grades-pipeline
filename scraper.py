import re
from typing import Dict, List, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PORTAL_BASE = "https://parentportal.cajonvalley.net/"
PORTAL_HOME = "https://parentportal.cajonvalley.net/Home/PortalMainPage"

# Utility regexes
RE_PERIOD = re.compile(r"\bperiod[:\s]*([0-9A-Za-z]+)\b", re.I)
RE_LEADING_PERIOD = re.compile(r"^\s*([0-9A-Za-z]+)\s+")  # e.g., "2 CC Math 6 ..."
RE_ASSIGN_AVG = re.compile(r"assignments?\s+average\s*:\s*([A-F][\-\+]?)\s*\(Pts:\s*([0-9.]+)\s*/\s*([0-9.]+)\)", re.I)


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def goto(page, url: str, timeout: int = 15000, wait_until: str = "domcontentloaded"):
    # Some redirects abort the initial navigation; retry with 'commit' if needed.
    try:
        page.goto(url, wait_until=wait_until, timeout=timeout)
    except Exception as e:
        page.goto(url, wait_until="commit", timeout=timeout)


def ensure_logged_in(page, username: str, password: str) -> None:
    goto(page, PORTAL_BASE)
    # Dismiss “session timed out” dialog if present
    try:
        page.get_by_role("button", name=re.compile("OK|Continue", re.I)).click(timeout=2500)
    except Exception:
        pass

    # If login form visible, log in; otherwise we’re already authenticated (hosted runner often stays warm)
    user_in = page.locator("input[name='LoginName'], input#LoginName")
    pass_in = page.locator("input[name='Password'], input#Password")
    login_btn = page.get_by_role("button", name=re.compile("login", re.I))
    if user_in.count() and pass_in.count():
        user_in.first.fill(username, timeout=7000)
        pass_in.first.fill(password, timeout=7000)
        login_btn.click(timeout=7000)

    goto(page, PORTAL_HOME)
    page.wait_for_load_state("domcontentloaded")


def ui_snapshot(page) -> Tuple[str, str]:
    """Helpful log of where we are; returns (url, banner_text)."""
    url = page.url
    # banner that sometimes shows “Terms of Use” etc.
    banner = ""
    try:
        banner = clean_text(page.locator("div[role='banner'], #divBanner, header").first.inner_text(timeout=1500))
    except Exception:
        banner = ""
    return url, banner


def switch_student_by_header_text(page, student: str) -> None:
    """
    On the Show All Assignments view, the student switcher is usually a header or
    dropdown. Strategy: find an element with the student name and click it if it
    looks like a tab/button. If not found, keep going (we’ll still read visible tables).
    """
    switched = False
    try:
        el = page.get_by_text(re.compile(rf"\b{re.escape(student)}\b", re.I)).first
        role = ""
        try:
            role = el.get_attribute("role") or ""
        except Exception:
            pass
        if el and (role.lower() in ("button", "tab") or el.is_visible()):
            el.click(timeout=2000, force=True)
            switched = True
    except Exception:
        pass
    print(f"DEBUG – switched via header text to student '{student}'" if switched else
          f"DEBUG – could not locate a picker for '{student}'; skipping switch")


def nearest_panel_heading_text(table):
    """From a table element, walk up to a panel and read a heading/title."""
    panel = table.locator("xpath=ancestor::div[contains(@class,'panel')][1]")
    heading = ""
    teacher = ""
    try:
        # Common heading containers
        heading_loc = panel.locator(".panel-heading, header, .card-header").first
        heading = clean_text(heading_loc.inner_text(timeout=1500))
    except Exception:
        heading = ""
    # Try to extract a teacher inside heading if it’s specially marked
    try:
        tloc = panel.locator(".panel-heading .teacher, .panel-heading .label:has-text('Teacher') + *, .panel-heading").first
        teacher = clean_text(tloc.inner_text(timeout=1200))
    except Exception:
        teacher = ""

    return heading, teacher


def parse_course_period_from_heading(heading: str, teacher_hint: str) -> Tuple[str, str, str]:
    """
    Returns (course, period, teacher_from_heading).
    - Removes generic labels like “Assignments Show All”
    - Removes teacher name from course string when duplicated
    - Extracts 'Period' using common patterns
    """
    h = heading or ""
    h = re.sub(r"\bAssignments?\b\s*[:\-]?\s*(Show\s*All)?", "", h, flags=re.I)
    h = re.sub(r"\bTerms of Use\b", "", h, flags=re.I)
    h = clean_text(h)

    # Teacher often appears in the heading; pull the last token containing comma
    teacher_from_heading = ""
    m = re.search(r"([A-Z][a-zA-Z\-']+,\s*[A-Z](?:[a-z])?)", h)
    if m:
        teacher_from_heading = m.group(1)

    # Period
    period = ""
    m = RE_PERIOD.search(h)
    if m:
        period = m.group(1)
    else:
        m2 = RE_LEADING_PERIOD.search(h)
        if m2:
            period = m2.group(1)

    course = h
    # remove teacher names from course
    for t in [teacher_hint, teacher_from_heading]:
        if t and t in course:
            course = course.replace(t, "")
    # remove explicit "Period ..." fragments from course
    course = re.sub(r"\bPeriod[:\s]*[0-9A-Za-z]+\b", "", course, flags=re.I)
    course = clean_text(course)

    return course, period, (teacher_from_heading or teacher_hint or "")


def build_header_map(table) -> Dict[str, int]:
    """Map normalized column name → index."""
    header_map: Dict[str, int] = {}
    try:
        ths = table.locator("thead tr th")
        count = ths.count()
        for i in range(count):
            name = clean_text(ths.nth(i).inner_text())
            key = name.lower()
            header_map[key] = i
    except Exception:
        pass
    return header_map


def get_cell_text(cells, idx: int) -> str:
    try:
        return clean_text(cells.nth(idx).inner_text())
    except Exception:
        return ""


def is_summary_or_blank(assign_text: str) -> bool:
    if not assign_text:
        return True
    t = assign_text.lower()
    if "assignments average" in t:
        return True
    if "exempt from task" in t:
        return True
    return False


def extract_assignments_from_student(page, student: str) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """
    Returns (assignment_rows, grade_rows)
    grade_rows holds simple "Assignments Average" snapshots per panel if present.
    """
    rows: List[Dict[str, str]] = []
    grade_rows: List[Dict[str, str]] = []

    switch_student_by_header_text(page, student)

    # show-all has many tables with ids like tblAssign_#####
    tables = page.locator("table[id^='tblAssign_']")
    present = tables.count()
    # sometimes they're present but not visible until expanded; we still read their HTML
    visible = sum(1 for i in range(present) if tables.nth(i).is_visible())
    print(f"DEBUG – check 1: tables present={present}, visible={visible}")

    # Try again with a broader query (in case of dynamic fragments)
    if present == 0:
        tables = page.locator("table.tblassign, table.table:has(thead th:has-text('Assignment'))")
        present = tables.count()
        visible = sum(1 for i in range(present) if tables.nth(i).is_visible())
    print(f"DEBUG – check 2: tables present={present}, visible={visible}")

    for i in range(present):
        table = tables.nth(i)

        heading_text, teacher_hint = nearest_panel_heading_text(table)
        course, period, teacher_from_heading = parse_course_period_from_heading(heading_text, teacher_hint)
        teacher = teacher_from_heading or teacher_hint

        header_map = build_header_map(table)
        if not header_map:
            continue

        # normalize keys we care about by fuzzy matching
        def find_col(*candidates: str) -> int:
            for want in candidates:
                for k, idx in header_map.items():
                    if want in k:
                        return idx
            return -1

        idx_assigned = find_col("assigned", "date assigned")
        idx_due = find_col("due", "due date")
        idx_assign = find_col("assignment")
        idx_poss = find_col("possible", "pts possible", "points possible")
        idx_score = find_col("score")
        idx_pct = find_col("percent", "pct")
        idx_status = find_col("status", "missing", "win")
        idx_comments = find_col("comment", "notes")

        body_rows = table.locator("tbody tr")
        for r in range(body_rows.count()):
            tds = body_rows.nth(r).locator("td")
            # Protect against misaligned rows
            try:
                assign_txt = get_cell_text(tds, idx_assign) if idx_assign >= 0 else ""
            except Exception:
                assign_txt = ""

            # Skip summaries or blanks
            if is_summary_or_blank(assign_txt):
                # Try to capture an “Assignments Average: … (Pts: x / y)” line as a grade snapshot
                line = clean_text(body_rows.nth(r).inner_text())
                m = RE_ASSIGN_AVG.search(line)
                if m:
                    grade_rows.append({
                        "Student": student,
                        "Course": course,
                        "Teacher": teacher,
                        "GradeLetter": m.group(1),
                        "PointsEarned": m.group(2),
                        "PointsPossible": m.group(3),
                        "SourceURL": PORTAL_HOME
                    })
                continue

            rec: Dict[str, str] = {
                "Student": student,
                "Period": period,
                "Course": course,
                "Teacher": teacher,
                "DueDate": get_cell_text(tds, idx_due) if idx_due >= 0 else "",
                "AssignedDate": get_cell_text(tds, idx_assigned) if idx_assigned >= 0 else "",
                "Assignment": assign_txt,
                "PtsPossible": get_cell_text(tds, idx_poss) if idx_poss >= 0 else "",
                "Score": get_cell_text(tds, idx_score) if idx_score >= 0 else "",
                "Pct": get_cell_text(tds, idx_pct) if idx_pct >= 0 else "",
                "Status": get_cell_text(tds, idx_status) if idx_status >= 0 else "",
                "Comments": get_cell_text(tds, idx_comments) if idx_comments >= 0 else "",
                "SourceURL": PORTAL_HOME
            }
            rows.append(rec)

    return rows, grade_rows


def run_scrape(username: str, password: str, students: List[str]):
    """Return (assignment_rows, grade_rows)."""
    all_rows: List[Dict[str, str]] = []
    all_grade_rows: List[Dict[str, str]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        ensure_logged_in(page, username, password)
        url, banner = ui_snapshot(page)
        print(f"DEBUG – UI SNAPSHOT – url: {url}")
        print(f"DEBUG – UI SNAPSHOT – sample: {banner or 'Terms of Use'}")

        for s in students:
            rs, gs = extract_assignments_from_student(page, s)
            print(f"DEBUG – class tables for {s}: {len(rs)}")
            all_rows.extend(rs)
            all_grade_rows.extend(gs)

        browser.close()
    return all_rows, all_grade_rows
