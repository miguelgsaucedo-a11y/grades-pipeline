# scraper.py
# Frame-aware Q ParentConnection scraper (Cajon Valley)
# - Login via #Pin / #Password / #LoginButton
# - Click tiles by ID (#stuTile_357342 / #stuTile_357354)
# - Find the frame that holds "Assignments" and the frame with the tables
# - Click "Show All" and scrape all "Per:" sections

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

# -------------------- small helpers --------------------

def _to_num(v):
    if v is None: return None
    s = str(v).strip()
    if not s: return None
    try: return float(NUM_CLEAN.sub('', s))
    except: return None

def _all_roots(page):
    # main page + all frames (nested frames are included in page.frames)
    return [page] + list(page.frames)

def _wait_visible(root, selector, timeout_ms=8000):
    try:
        root.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except:
        return False

def _first_visible(root, selector):
    try:
        loc = root.locator(selector).first
        if loc.is_visible(): return loc
    except: pass
    return None

def _click_if_visible(root, selector):
    el = _first_visible(root, selector)
    if el:
        el.click(); return True
    return False

def _find_root_with_selector(page, selector, min_count=1, timeout_ms=6000):
    """Return the first page/frame with >=min_count visible matches for selector."""
    elapsed = 0
    step = 250
    while elapsed < timeout_ms:
        for r in _all_roots(page):
            try:
                locs = r.locator(selector)
                total = locs.count()
                vis = 0
                for i in range(min(total, 25)):
                    if locs.nth(i).is_visible(): vis += 1
                if vis >= min_count:
                    return r
            except: pass
        sleep(step/1000); elapsed += step
    return None

def _find_root_with_text(page, pattern, timeout_ms=6000):
    """Return first page/frame where regex 'pattern' is visible."""
    pat = re.compile(pattern, re.I)
    elapsed = 0; step = 250
    while elapsed < timeout_ms:
        for r in _all_roots(page):
            try:
                loc = r.get_by_text(pat, exact=False).first
                if loc.is_visible(): return r
            except: pass
        sleep(step/1000); elapsed += step
    return None

def _log_frames(page, label):
    try:
        print(f"DEBUG — {label}: {len(page.frames)} frames")
        for i, fr in enumerate(page.frames):
            print(f"DEBUG —   frame[{i}] name={fr.name} url={fr.url}")
    except: pass

# -------------------- scraping helpers --------------------

def _extract_sections(root):
    """divs that contain 'Per:' and at least one table underneath"""
    sections = []
    blocks = root.locator("xpath=//div[.//text()[contains(.,'Per:')]]").all()
    for b in blocks:
        try:
            if b.locator("css=table").first.count() > 0:
                sections.append(b)
        except: pass
    return sections

def _scrape_table(table, student_name, period, course, teacher):
    rows = table.locator("tr").all()
    if len(rows) < 2: return []
    headers = [c.inner_text().strip() for c in rows[0].locator("th,td").all()]

    def col(*names):
        for n in names:
            if n in headers: return headers.index(n)
        return None

    c_due   = col("Date Due","Due Date")
    c_asgn  = col("Assignment")
    c_asgnd = col("Assigned","Date Assigned")
    c_poss  = col("Pts Possible","Possible","Pos")
    c_score = col("Score")
    c_pct   = col("Pct Score","Pct")
    c_comm  = col("Comments","Comment")

    out = []
    imported_at = datetime.utcnow().isoformat()
    for r in rows[1:]:
        tds = [c.inner_text().strip() for c in r.locator("td").all()]
        if not tds: continue
        assignment = tds[c_asgn] if c_asgn is not None and c_asgn < len(tds) else ""
        if not assignment or assignment.lower() == "assignment": continue

        due   = tds[c_due]   if c_due   is not None and c_due   < len(tds) else ""
        asgnd = tds[c_asgnd] if c_asgnd is not None and c_asgnd < len(tds) else ""
        poss  = _to_num(tds[c_poss])  if c_poss  is not None and c_poss  < len(tds) else None
        score = _to_num(tds[c_score]) if c_score is not None and c_score < len(tds) else None
        pct   = _to_num(tds[c_pct])   if c_pct   is not None and c_pct   < len(tds) else None
        comm  = tds[c_comm] if c_comm is not None and c_comm < len(tds) else ""

        flags = []
        if ("missing" in (comm or "").lower()) or score == 0 or pct == 0: flags.append("Missing")
        if pct is not None and pct < 70: flags.append("Low")
        if pct is not None and pct >= 95: flags.append("Win")

        out.append({
            "ImportedAt": imported_at,
            "Student": student_name,
            "Period": period, "Course": course, "Teacher": teacher,
            "DueDate": due, "AssignedDate": asgnd, "Assignment": assignment,
            "PtsPossible": poss if poss is not None else "",
            "Score": score if score is not None else "",
            "Pct": pct if pct is not None else "",
            "Status": ",".join(flags), "Comments": comm,
            "SourceURL": table.page.url if hasattr(table, "page") else ""
        })
    return out

def _scrape_from_root(root, student_name):
    # Try to show everything if there is a "Show All"
    try:
        sa = root.get_by_text(re.compile(r"\bShow All\b", re.I)).first
        if sa.is_visible(): sa.click(); sleep(0.3)
    except: pass

    sections = _extract_sections(root)
    print(f"DEBUG — sections for {student_name}: {len(sections)}")

    # Fallback: any table that has an "Assignment" header
    if not sections:
        tables = root.locator("xpath=//table[.//th[contains(.,'Assignment')]]").all()
        print(f"DEBUG — fallback tables for {student_name}: {len(tables)}")
        results = []
        for t in tables:
            try:
                container = t.locator("xpath=ancestor::div[.//text()[contains(.,'Per:')]][1]").first
                header = container.text_content() or ""
            except:
                header = ""
            period = course = teacher = ""
            if header:
                lines = [ln for ln in header.splitlines() if ln.strip()]
                for ln in lines[:3]:
                    m = ASSIGNMENTS_HEADER_RE.search(ln)
                    if m:
                        period, course = m.group(1).strip(), m.group(2).strip()
                        break
                for ln in lines:
                    if ln.strip().lower().startswith("teacher:"):
                        teacher = ln.split(":",1)[1].strip(); break
            results.extend(_scrape_table(t, student_name, period, course, teacher))
        return results

    results = []
    for sec in sections:
        try: header = sec.text_content() or ""
        except: continue
        if "Per:" not in header: continue

        lines = [ln for ln in header.splitlines() if ln.strip()]
        m = None
        for ln in lines[:3]:
            m = ASSIGNMENTS_HEADER_RE.search(ln)
            if m: break
        if not m: continue
        period, course = m.group(1).strip(), m.group(2).strip()

        teacher = ""
        for ln in lines:
            if ln.lower().startswith("teacher:"):
                teacher = ln.split(":",1)[1].strip(); break

        table = sec.locator("css=table").first
        if not table or not table.is_visible(): continue
        results.extend(_scrape_table(table, student_name, period, course, teacher))
    return results

# -------------------- main flow --------------------

def run_scrape(pin, password, students=("Adrian","Jacob")):
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # 1) Login on the root URL
        page.goto("https://parentportal.cajonvalley.net/", wait_until="domcontentloaded")
        print("DEBUG — landed:", page.url)

        pin_ok = _wait_visible(page, "#Pin", 12000)
        pwd_ok = _wait_visible(page, "#Password", 12000)
        print("DEBUG — login fields visible:", pin_ok, pwd_ok)
        if pin_ok: page.fill("#Pin", str(pin))
        if pwd_ok: page.fill("#Password", str(password))
        clicked_login = _click_if_visible(page, "#LoginButton")
        if not clicked_login:
            try: page.locator("#Password").press("Enter")
            except: pass

        page.wait_for_load_state("domcontentloaded"); sleep(1.0)
        _log_frames(page, "after login")

        # 2) Ensure we're on the student picker; if not, go directly
        if not _find_root_with_selector(page, f"#stuTile_{TILE_ID['adrian']}", 1, 4000):
            page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage", wait_until="domcontentloaded")
            sleep(0.8)
            _log_frames(page, "after PortalMainPage")

        for s in students:
            sid = TILE_ID.get(s.lower())
            if not sid:
                print(f"DEBUG — no tile id mapping for {s}, skipping"); continue

            # 3) Click the student tile by ID across any frame
            tiles_root = _find_root_with_selector(page, f"#stuTile_{sid}", 1, 6000)
            print("DEBUG — tiles_root for", s, "exists:", bool(tiles_root))
            if tiles_root:
                _click_if_visible(tiles_root, f"#stuTile_{sid}")
                page.wait_for_load_state("domcontentloaded"); sleep(0.8)
            else:
                print(f"DEBUG — tile not found for {s}")
                continue

            # 4) Find Assignments LEFT NAV (if present) and click it (in its own frame)
            nav_root = _find_root_with_selector(page, "td.td2_action:has-text('Assignments')", 1, 4000)
            print("DEBUG — nav_root for Assignments exists:", bool(nav_root))
            if nav_root:
                _click_if_visible(nav_root, "td.td2_action:has-text('Assignments')")
                page.wait_for_load_state("domcontentloaded"); sleep(0.6)

            # 5) Find the CONTENT frame that actually holds the assignment tables
            content_root = (
                _find_root_with_selector(page, "xpath=//div[.//text()[contains(.,'Per:')]]", 1, 4000)
                or _find_root_with_selector(page, "xpath=//table[.//th[contains(.,'Assignment')]]", 1, 4000)
            )
            print("DEBUG — content_root exists:", bool(content_root))

            rows = _scrape_from_root(content_root or page, s)
            print(f"DEBUG — scraped rows for {s}: {len(rows)}")
            all_rows.extend(rows)

            # 6) Click Home anywhere to return to picker for next student
            home_root = _find_root_with_text(page, r"\bHome\b", 4000)
            if home_root:
                _click_if_visible(home_root, "text=Home")
                page.wait_for_load_state("domcontentloaded"); sleep(0.6)

        browser.close()
    return all_rows
