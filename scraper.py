# scraper.py
# Login with PIN/Password on https://parentportal.cajonvalley.net/,
# click known student tiles by ID, open Assignments, and scrape class tables.

from playwright.sync_api import sync_playwright
from datetime import datetime
import re
from time import sleep

ASSIGNMENTS_HEADER_RE = re.compile(r'^Per:\s*(\S+)\s+(.*)$', re.I)
NUM_CLEAN = re.compile(r'[^0-9.\-]')

# --- helpers ---------------------------------------------------------------

def _to_num(v):
    if v is None: return None
    s = str(v).strip()
    if not s: return None
    try: return float(NUM_CLEAN.sub('', s))
    except: return None

def _wait(page, selector, timeout_ms=10000):
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except:
        return False

def _click_if_visible(page, selector):
    try:
        el = page.locator(selector).first
        if el.is_visible():
            el.click(); return True
    except: pass
    return False

def _extract_sections(root):
    """find div blocks that contain 'Per:' and a table"""
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

def _scrape_from_assignments(page, student_name):
    # Click the left-nav "Assignments" cell if present
    _click_if_visible(page, "td.td2_action:has-text('Assignments')")
    sleep(0.5)

    sections = _extract_sections(page)
    print(f"DEBUG — sections for {student_name}: {len(sections)}")

    # Fallback: any visible table that has an "Assignment" header
    if not sections:
        tables = page.locator("xpath=//table[.//th[contains(.,'Assignment')]]").all()
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

# --- main scrape -----------------------------------------------------------

def run_scrape(pin, password, students=("Adrian","Jacob")):
    """
    pin  = your ParentPortal PIN (we use PORTAL_USER env for this)
    password = your ParentPortal password
    """
    # Map student names to known tile IDs you provided.
    TILE_ID = {
        "adrian": "357342",
        "jacob":  "357354",
    }

    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # 1) Go to the **login page** (root URL you shared)
        page.goto("https://parentportal.cajonvalley.net/", wait_until="domcontentloaded")
        print("DEBUG — landed:", page.url)

        # 2) Fill PIN & Password using exact IDs you shared
        pin_ok = _wait(page, "#Pin", 10000)
        pwd_ok = _wait(page, "#Password", 10000)
        print("DEBUG — login fields visible:", pin_ok, pwd_ok)
        if pin_ok: page.fill("#Pin", str(pin))
        if pwd_ok: page.fill("#Password", str(password))

        # 3) Click the Login button (type=button, id=LoginButton)
        clicked = _click_if_visible(page, "#LoginButton")
        if not clicked:
            # as a fallback, press Enter in the password field
            try: page.locator("#Password").press("Enter")
            except: pass

        # 4) Wait for the student picker (your tiles by ID)
        tiles = [f"#stuTile_{TILE_ID['adrian']}", f"#stuTile_{TILE_ID['jacob']}"]
        saw_tiles = any(_wait(page, sel, 12000) for sel in tiles)
        print("DEBUG — saw student tiles:", saw_tiles)

        # If we didn’t navigate, try the main page directly
        if not saw_tiles:
            page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage", wait_until="domcontentloaded")
            saw_tiles = any(_wait(page, sel, 8000) for sel in tiles)
            print("DEBUG — after direct goto, saw tiles:", saw_tiles)

        for s in students:
            sid = TILE_ID.get(s.lower())
            if not sid:
                print(f"DEBUG — no tile id mapping for {s}, skipping")
                continue

            # 5) Click the student tile by **ID**
            clicked_tile = _click_if_visible(page, f"#stuTile_{sid}")
            print(f"DEBUG — clicked tile for {s}: {clicked_tile}")
            sleep(0.8)

            # 6) Scrape Assignments on that page
            rows = _scrape_from_assignments(page, s)
            print(f"DEBUG — scraped rows for {s}: {len(rows)}")
            all_rows.extend(rows)

            # 7) Back to Home (to switch to the other student)
            _click_if_visible(page, "text=Home")
            sleep(0.7)

        browser.close()
    return all_rows
