# scraper.py
# Frame-aware Q ParentConnection scraper (Cajon Valley)
# - Login via #Pin / #Password / #LoginButton
# - Click tiles by ID (#stuTile_357342 / #stuTile_357354)
# - Click "Assignments", then scan ALL frames for tables with "Assignment" header
# - Normalize headers (handles NBSP and case) and match by "contains"
# - Force-return to PortalMainPage between students

from playwright.sync_api import sync_playwright
from datetime import datetime
import re
from time import sleep

ASSIGNMENTS_HEADER_RE = re.compile(r'^Per:\s*(\S+)\s+(.*)$', re.I)
NUM_CLEAN = re.compile(r'[^0-9.\-]')

TILE_ID = {
    "adrian": "357342",
    "jacob":  "357354",
}

# ---------------- helpers ----------------

def _norm(s: str) -> str:
    if s is None:
        return ""
    s = str(s).replace("\xa0", " ")
    return " ".join(s.split())

def _to_num(v):
    s = _norm(v)
    if not s:
        return None
    try:
        return float(NUM_CLEAN.sub("", s))
    except:
        return None

def _wait_visible(root, selector, timeout_ms=8000):
    try:
        root.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except:
        return False

def _first_visible(root, selector):
    try:
        loc = root.locator(selector).first
        if loc.is_visible():
            return loc
    except:
        pass
    return None

def _click_if_visible(root, selector):
    el = _first_visible(root, selector)
    if el:
        el.click()
        return True
    return False

def _all_roots(page):
    return [page] + list(page.frames)

def _find_root_with_selector(page, selector, min_count=1, timeout_ms=6000):
    elapsed = 0
    step = 250
    while elapsed < timeout_ms:
        for r in _all_roots(page):
            try:
                locs = r.locator(selector)
                total = locs.count()
                vis = 0
                for i in range(min(total, 25)):
                    if locs.nth(i).is_visible():
                        vis += 1
                if vis >= min_count:
                    return r
            except:
                pass
        sleep(step/1000)
        elapsed += step
    return None

def _log_frames(page, label):
    try:
        print(f"DEBUG — {label}: {len(page.frames)} frames")
        for i, fr in enumerate(page.frames):
            print(f"DEBUG —   frame[{i}] name={fr.name} url={fr.url}")
    except:
        pass

# -------------- scraping bits ----------------

def _extract_sections(root):
    sections = []
    blocks = root.locator("xpath=//div[.//text()[contains(.,'Per:')]]").all()
    for b in blocks:
        try:
            if b.locator("css=table").first.count() > 0:
                sections.append(b)
        except:
            pass
    return sections

def _scrape_table(table, student_name, period, course, teacher):
    rows = table.locator("tr").all()
    if len(rows) < 2:
        return []

    headers_raw = [c.inner_text() for c in rows[0].locator("th,td").all()]
    headers = [_norm(h) for h in headers_raw]

    def col(*names):
        for idx, h in enumerate(headers):
            for n in names:
                if re.search(rf"\b{re.escape(n)}\b", h, re.I):
                    return idx
        return None

    c_due   = col("Date Due", "Due Date")
    c_asgn  = col("Assignment")
    c_asgnd = col("Assigned", "Date Assigned")
    c_poss  = col("Pts Possible", "Possible", "Pos")
    c_score = col("Score")
    c_pct   = col("Pct Score", "Pct")
    c_comm  = col("Comments", "Comment")

    out = []
    imported_at = datetime.utcnow().isoformat()

    for r in rows[1:]:
        tds = [ _norm(c.inner_text()) for c in r.locator("td").all() ]
        if not tds:
            continue
        assignment = tds[c_asgn] if c_asgn is not None and c_asgn < len(tds) else ""
        if not assignment or _norm(assignment).lower() == "assignment":
            continue

        due   = tds[c_due]   if c_due   is not None and c_due   < len(tds) else ""
        asgnd = tds[c_asgnd] if c_asgnd is not None and c_asgnd < len(tds) else ""
        poss  = _to_num(tds[c_poss])  if c_poss  is not None and c_poss  < len(tds) else None
        score = _to_num(tds[c_score]) if c_score is not None and c_score < len(tds) else None
        pct   = _to_num(tds[c_pct])   if c_pct   is not None and c_pct   < len(tds) else None
        comm  = tds[c_comm] if c_comm is not None and c_comm < len(tds) else ""

        flags = []
        if ("missing" in (_norm(comm).lower())) or score == 0 or pct == 0:
            flags.append("Missing")
        if pct is not None and pct < 70:
            flags.append("Low")
        if pct is not None and pct >= 95:
            flags.append("Win")

        out.append({
            "ImportedAt": imported_at,
            "Student": student_name,
            "Period": period,
            "Course": course,
            "Teacher": teacher,
            "DueDate": due,
            "AssignedDate": asgnd,
            "Assignment": assignment,
            "PtsPossible": poss if poss is not None else "",
            "Score": score if score is not None else "",
            "Pct": pct if pct is not None else "",
            "Status": ",".join(flags),
            "Comments": comm,
            "SourceURL": table.page.url if hasattr(table, "page") else ""
        })

    return out

def _scrape_from_container(container, student_name):
    results = []
    sections = _extract_sections(container)
    print(f"DEBUG — sections for {student_name}: {len(sections)}")

    if not sections:
        return results

    for sec in sections:
        try:
            header = _norm(sec.text_content())
        except:
            continue
        if "Per:" not in header:
            continue

        lines = [ln for ln in header.splitlines() if ln.strip()]
        period = course = ""
        teacher = ""
        # parse "Per: X Course"
        for ln in lines[:3]:
            m = ASSIGNMENTS_HEADER_RE.search(ln)
            if m:
                period, course = _norm(m.group(1)), _norm(m.group(2))
                break
        for ln in lines:
            if ln.lower().startswith("teacher:"):
                teacher = _norm(ln.split(":",1)[1])
                break

        table = sec.locator("css=table").first
        if not table or not table.is_visible():
            continue
        results.extend(_scrape_table(table, student_name, period, course, teacher))
    return results

def _collect_tables_anywhere(page, student_name):
    """After clicking Assignments, look for tables in ANY frame; parse even if header blocks aren't found."""
    # First try structured sections per frame
    for root in _all_roots(page):
        try:
            sec_cnt = root.locator("xpath=//div[.//text()[contains(.,'Per:')]]").count()
            if sec_cnt > 0:
                rows = _scrape_from_container(root, student_name)
                if rows:
                    return rows
        except:
            pass

    # If no structured sections found, parse any table with 'Assignment' header anywhere
    all_rows = []
    total_tables = 0
    for root in _all_roots(page):
        try:
            tables = root.locator("xpath=//table[.//th[contains(.,'Assignment')]]").all()
            total_tables += len(tables)
            for t in tables:
                # try to infer period/course/teacher from nearby text
                try:
                    container = t.locator("xpath=ancestor::div[1]").first
                    header = _norm(container.text_content())
                except:
                    header = ""
                period = course = teacher = ""
                if header:
                    lines = [ln for ln in header.splitlines() if ln.strip()]
                    for ln in lines[:3]:
                        m = ASSIGNMENTS_HEADER_RE.search(ln)
                        if m:
                            period, course = _norm(m.group(1)), _norm(m.group(2))
                            break
                    for ln in lines:
                        if ln.lower().startswith("teacher:"):
                            teacher = _norm(ln.split(":",1)[1]); break
                all_rows.extend(_scrape_table(t, student_name, period, course, teacher))
        except:
            pass
    print(f"DEBUG — tables(anywhere) for {student_name}: {total_tables}")
    return all_rows

# ---------------- main flow ----------------

def run_scrape(pin, password, students=("Adrian","Jacob")):
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # Login
        page.goto("https://parentportal.cajonvalley.net/", wait_until="domcontentloaded")
        print("DEBUG — landed:", page.url)

        pin_ok = _wait_visible(page, "#Pin", 12000)
        pwd_ok = _wait_visible(page, "#Password", 12000)
        print("DEBUG — login fields visible:", pin_ok, pwd_ok)
        if pin_ok: page.fill("#Pin", str(pin))
        if pwd_ok: page.fill("#Password", str(password))
        if not _click_if_visible(page, "#LoginButton"):
            try: page.locator("#Password").press("Enter")
            except: pass

        page.wait_for_load_state("domcontentloaded"); sleep(1.0)
        _log_frames(page, "after login")

        for s in students:
            sid = TILE_ID.get(s.lower())
            if not sid:
                print(f"DEBUG — no tile id mapping for {s}, skipping"); continue

            # Always force back to the picker before switching student
            page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage", wait_until="domcontentloaded")
            sleep(0.6)

            tiles_root = _find_root_with_selector(page, f"#stuTile_{sid}", 1, 6000)
            print("DEBUG — tiles_root for", s, "exists:", bool(tiles_root))
            if not tiles_root:
                print(f"DEBUG — tile not found for {s}")
                continue

            _click_if_visible(tiles_root, f"#stuTile_{sid}")
            page.wait_for_load_state("domcontentloaded"); sleep(0.8)

            # Click Assignments in any frame (left nav uses td.td2_action)
            nav_root = _find_root_with_selector(page, "td.td2_action:has-text('Assignments')", 1, 5000)
            print("DEBUG — nav_root for Assignments exists:", bool(nav_root))
            if nav_root:
                _click_if_visible(nav_root, "td.td2_action:has-text('Assignments')")
                page.wait_for_load_state("domcontentloaded"); sleep(0.5)

            # Try to show-all where present (any frame)
            for r in _all_roots(page):
                try:
                    sa = r.get_by_text(re.compile(r"\bShow All\b", re.I)).first
                    if sa.is_visible():
                        sa.click(); break
                except:
                    pass

            # Collect tables across ALL frames
            rows = _collect_tables_anywhere(page, s)
            print(f"DEBUG — scraped rows for {s}: {len(rows)}")
            all_rows.extend(rows)

        browser.close()
    return all_rows
