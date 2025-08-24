# scraper.py
# Student tiles + Assignments nav aware scraper for Q ParentConnection (Cajon Valley)
# Uses :has() selectors to click <div class="studentTile"> by nickname, then clicks the left
# menu cell <td class="td2_action">Assignments</td>, then scrapes all "Per:" sections.

from playwright.sync_api import sync_playwright
from datetime import datetime
import re
from time import sleep

ASSIGNMENTS_HEADER_RE = re.compile(r'^Per:\s*(\S+)\s+(.*)$', re.I)
NUM_CLEAN = re.compile(r'[^0-9.\-]')

def _to_num(v):
    if v is None: return None
    s = str(v).strip()
    if not s: return None
    try: return float(NUM_CLEAN.sub('', s))
    except: return None

def _wait_visible_count(root, selector, min_count=1, timeout_ms=5000):
    """Wait until at least min_count elements are visible for selector, or timeout."""
    elapsed = 0
    step = 200
    while elapsed < timeout_ms:
        try:
            locs = root.locator(selector)
            cnt = locs.count()
            vis = 0
            for i in range(min(cnt, 20)):  # sample to avoid slow loops
                if locs.nth(i).is_visible(): vis += 1
            if vis >= min_count:
                return True
        except Exception:
            pass
        sleep(step/1000)
        elapsed += step
    return False

def _click_student_tile(page, nickname):
    # Click div.studentTile that has a span.tileStudentNickname with the nickname text
    sel = f"css=div.studentTile:has(span.tileStudentNickname:has-text('{nickname}'))"
    try:
        tile = page.locator(sel).first
        if tile.is_visible():
            tile.click()
            return True
    except Exception:
        pass
    # Try more generic text click anywhere
    try:
        page.get_by_text(re.compile(rf"\b{re.escape(nickname)}\b", re.I)).first.click()
        return True
    except Exception:
        return False

def _go_assignments(page):
    """Click the left-nav 'Assignments' cell, then wait for content area to show sections."""
    try:
        # Left menu entry is a td with class td2_action
        nav = page.locator("td.td2_action").filter(has_text=re.compile(r"\bAssignments\b", re.I)).first
        if nav.is_visible():
            nav.click()
    except Exception:
        # Fallback: click any visible text "Assignments"
        try:
            page.get_by_text(re.compile(r"\bAssignments\b", re.I)).first.click()
        except Exception:
            pass
    # Wait for any sign of the assignments content
    _wait_visible_count(page, "text=/^Per:\\s/i", min_count=1, timeout_ms=4000)

def _extract_sections(root):
    """Return divs that contain 'Per:' text and a table."""
    sections = []
    blocks = root.locator("xpath=//div[.//text()[contains(.,'Per:')]]").all()
    for b in blocks:
        try:
            if b.locator("css=table").first.count() > 0:
                sections.append(b)
        except Exception:
            continue
    return sections

def scrape_student_assignments(page, student_name):
    # ensure we’re on the Assignments view
    _go_assignments(page)

    results = []
    sections = _extract_sections(page)
    print(f"DEBUG — sections found for {student_name}: {len(sections)}")

    # Fallback: harvest any table that has 'Assignment' header
    if not sections:
        tables = page.locator("xpath=//table[.//th[contains(.,'Assignment')]]").all()
        print(f"DEBUG — fallback tables for {student_name}: {len(tables)}")
        for t in tables:
            try:
                container = t.locator("xpath=ancestor::div[.//text()[contains(.,'Per:')]][1]").first
                header = container.text_content() or ""
            except Exception:
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

    # Normal path
    for sec in sections:
        try:
            header = sec.text_content() or ""
        except Exception:
            continue
        if "Per:" not in header: 
            continue

        lines = [ln for ln in header.splitlines() if ln.strip()]
        m = None
        for ln in lines[:3]:
            m = ASSIGNMENTS_HEADER_RE.search(ln)
            if m: break
        if not m: 
            continue
        period, course = m.group(1).strip(), m.group(2).strip()

        teacher = ""
        for ln in lines:
            if ln.lower().startswith("teacher:"):
                teacher = ln.split(":",1)[1].strip(); break

        table = sec.locator("css=table").first
        if not table or not table.is_visible():
            continue

        results.extend(_scrape_table(table, student_name, period, course, teacher))
    return results

def _scrape_table(table, student_name, period, course, teacher):
    rows = table.locator("tr").all()
    out = []
    if len(rows) < 2: 
        return out

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

        status = []
        lower = (comm or "").lower()
        if "missing" in lower or score == 0 or pct == 0: status.append("Missing")
        if pct is not None and pct < 70: status.append("Low")
        if pct is not None and pct >= 95: status.append("Win")

        out.append({
            "ImportedAt": imported_at,
            "Student": student_name,
            "Period": period, "Course": course, "Teacher": teacher,
            "DueDate": due, "AssignedDate": asgnd, "Assignment": assignment,
            "PtsPossible": poss if poss is not None else "",
            "Score": score if score is not None else "",
            "Pct": pct if pct is not None else "",
            "Status": ",".join(status), "Comments": comm,
            "SourceURL": table.page.url if hasattr(table, "page") else ""
        })
    return out

def run_scrape(username, password, students=("Adrian","Jacob")):
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage", wait_until="domcontentloaded")
        print("DEBUG — landed:", page.url)
        sleep(0.8)

        # Some sites show a pre-login link
        try:
            page.get_by_text(re.compile(r"\b(Sign In|Log In|Login)\b", re.I)).first.click()
        except Exception:
            pass

        # Fill username/login/pin
        for sel in [
            'input[name="username"]','input#username',
            'input[name="UserName"]','input#UserName',
            'input[name="Pin"]','input#Pin',
            'input[name="Login"]','input#Login',
            'input[type="text"]'
        ]:
            try:
                if page.locator(sel).first.is_visible():
                    page.fill(sel, username); break
            except Exception: pass

        # Fill password
        for sel in [
            'input[name="password"]','input#password',
            'input[name="Password"]','input#Password',
            'input[type="password"]'
        ]:
            try:
                if page.locator(sel).first.is_visible():
                    page.fill(sel, password); break
            except Exception: pass

        # Click login/submit
        for click_sel in [
            "text=Sign In","text=Log In","text=Login",
            'css=input[type="submit"]','css=button[type="submit"]','#btnLogin'
        ]:
            try:
                loc = page.locator(click_sel).first
                if loc.is_visible():
                    loc.click(); break
            except Exception: pass

        page.wait_for_load_state("domcontentloaded"); sleep(1)
        # Count tiles to confirm we’re on the picker
        tile_sel = "css=div.studentTile"
        tiles_visible = _wait_visible_count(page, tile_sel, min_count=1, timeout_ms=4000)
        tile_count = page.locator(tile_sel).count() if tiles_visible else 0
        print("DEBUG — student tiles visible:", tiles_visible, "count:", tile_count)

        for s in students:
            if tile_count == 0:
                # Try going Home to get back to the picker
                try:
                    page.get_by_text(re.compile(r"\bHome\b", re.I)).first.click()
                    page.wait_for_load_state("domcontentloaded"); sleep(0.6)
                except Exception:
                    pass
                tiles_visible = _wait_visible_count(page, tile_sel, min_count=1, timeout_ms=3000)
                tile_count = page.locator(tile_sel).count() if tiles_visible else 0
                print("DEBUG — after Home, tiles visible:", tiles_visible, "count:", tile_count)

            clicked = _click_student_tile(page, s)
            print(f"DEBUG — clicked tile for {s}: {clicked}")
            page.wait_for_load_state("domcontentloaded"); sleep(0.8)

            rows = scrape_student_assignments(page, s)
            print(f"DEBUG — scraped rows for {s}: {len(rows)}")
            all_rows.extend(rows)

            # go back to picker for next student
            try:
                page.get_by_text(re.compile(r"\bHome\b", re.I)).first.click()
                page.wait_for_load_state("domcontentloaded"); sleep(0.6)
            except Exception:
                pass

        browser.close()
    return all_rows
