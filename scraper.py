# scraper.py
# Ultra-robust login + cross-frame scraping for Q ParentConnection (Cajon Valley)

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

def _all_roots(page):
    # main page first, then frames
    return [page] + list(page.frames)

def _first_visible(root, sel):
    try:
        loc = root.locator(sel).first
        if loc.is_visible(): return loc
    except: pass
    return None

def _press_enter_on(root, selector):
    try:
        el = root.locator(selector).first
        if el.is_visible():
            el.press("Enter")
            return True
    except:
        pass
    return False

def _log_frames(page, label):
    try:
        print(f"DEBUG — {label}: {len(page.frames)} frames")
        for i, fr in enumerate(page.frames):
            try:
                print(f"DEBUG — frame[{i}] url={fr.url} name={fr.name}")
            except:
                pass
    except:
        pass

def _find_root_with_selector(page, sel, min_visible=1, timeout_ms=6000):
    elapsed = 0
    step = 250
    while elapsed < timeout_ms:
        for r in _all_roots(page):
            try:
                locs = r.locator(sel)
                count = locs.count()
                vis = 0
                for i in range(min(25, count)):
                    if locs.nth(i).is_visible(): vis += 1
                if vis >= min_visible:
                    return r
            except:
                pass
        sleep(step/1000); elapsed += step
    return None

def _click_student_tile_anywhere(page, nickname, id_hint=None):
    for root in _all_roots(page):
        # 1) Try ID first (most reliable)
        if id_hint:
            loc = _first_visible(root, f"#stuTile_{id_hint}")
            if loc:
                loc.click(); return True
        # 2) Try nickname inside tile
        loc = _first_visible(root, f"css=div.studentTile:has(span.tileStudentNickname:has-text('{nickname}'))")
        if loc:
            loc.click(); return True
        # 3) Any tile that contains nickname text
        loc = _first_visible(root, f"css=div.studentTile:has-text('{nickname}')")
        if loc:
            loc.click(); return True
    return False

def _go_assignments_anywhere(page):
    # Try in any root (page or frames)
    for root in _all_roots(page):
        loc = _first_visible(root, "td.td2_action:has-text('Assignments')")
        if loc:
            loc.click(); return root
    # Fallback: any visible text "Assignments"
    for root in _all_roots(page):
        try:
            el = root.get_by_text(re.compile(r"\bAssignments\b", re.I)).first
            if el.is_visible():
                el.click(); return root
        except: pass
    return None

def _extract_sections(root):
    sections = []
    blocks = root.locator("xpath=//div[.//text()[contains(.,'Per:')]]").all()
    for b in blocks:
        try:
            if b.locator("css=table").first.count() > 0:
                sections.append(b)
        except: 
            continue
    return sections

def _scrape_table(table, student_name, period, course, teacher):
    out = []
    rows = table.locator("tr").all()
    if len(rows) < 2: return out

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

        flags = []
        if ("missing" in (comm or "").lower()) or score == 0 or pct == 0: flags.append("Missing")
        if pct is not None and pct < 70: flags.append("Low")
        if pct is not None and pct >= 95: flags.append("Win")

        out.append({
            "ImportedAt": imported_at, "Student": student_name,
            "Period": period, "Course": course, "Teacher": teacher,
            "DueDate": due, "AssignedDate": asgnd, "Assignment": assignment,
            "PtsPossible": poss if poss is not None else "",
            "Score": score if score is not None else "",
            "Pct": pct if pct is not None else "",
            "Status": ",".join(flags), "Comments": comm,
            "SourceURL": table.page.url if hasattr(table, "page") else ""
        })
    return out

def _scrape_from_assignments_root(root, student_name):
    results = []
    sections = _extract_sections(root)
    print(f"DEBUG — sections for {student_name}: {len(sections)}")
    if not sections:
        # fallback: any table with 'Assignment' header
        tables = root.locator("xpath=//table[.//th[contains(.,'Assignment')]]").all()
        print(f"DEBUG — fallback tables for {student_name}: {len(tables)}")
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

def run_scrape(username, password, students=("Adrian","Jacob")):
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage", wait_until="domcontentloaded")
        print("DEBUG — landed:", page.url)
        sleep(0.8)
        _log_frames(page, "after landing")

        # Some sites show a pre-login link
        for root in _all_roots(page):
            try:
                root.get_by_text(re.compile(r"\b(Sign In|Log In|Login)\b", re.I)).first.click()
                break
            except: pass

        # Fill the first visible text + password inputs anywhere (page or frames)
        text_filled = False
        pass_filled = False
        for root in _all_roots(page):
            try:
                el = root.locator('input[type="text"]').first
                if el.is_visible():
                    el.fill(username); text_filled = True; break
            except: pass
        for root in _all_roots(page):
            try:
                el = root.locator('input[type="password"]').first
                if el.is_visible():
                    el.fill(password); pass_filled = True; pwd_root = root; break
            except: pass
        print("DEBUG — filled text:", text_filled, "filled password:", pass_filled)

        # Submit: press Enter on password field; also try clicking any obvious submit
        if pass_filled:
            _press_enter_on(pwd_root, 'input[type="password"]')
        clicked_submit = False
        for root in _all_roots(page):
            for sel in ["text=Sign In","text=Log In","text=Login",'css=input[type="submit"]','css=button[type="submit"]','#btnLogin']:
                try:
                    el = root.locator(sel).first
                    if el.is_visible():
                        el.click(); clicked_submit = True; break
                except: pass
            if clicked_submit: break

        page.wait_for_load_state("domcontentloaded"); sleep(1.2)
        _log_frames(page, "after login")

        # Try to find the tiles anywhere; if not, navigate Home explicitly
        tiles_root = _find_root_with_selector(page, "css=div.studentTile", 1, 6000)
        if not tiles_root:
            for root in _all_roots(page):
                try:
                    root.get_by_text(re.compile(r"\bHome\b", re.I)).first.click()
                    break
                except: pass
            page.wait_for_load_state("domcontentloaded"); sleep(0.8)
            _log_frames(page, "after Home")
            tiles_root = _find_root_with_selector(page, "css=div.studentTile", 1, 6000)

        print("DEBUG — tiles_root exists:", bool(tiles_root))

        for s in students:
            # map known IDs (from your HTML)
            id_hint = "357342" if s.lower().startswith("adrian") else ("357354" if s.lower().startswith("jacob") else None)
            clicked = _click_student_tile_anywhere(page, s, id_hint=id_hint)
            print(f"DEBUG — clicked tile for {s}: {clicked}")
            page.wait_for_load_state("domcontentloaded"); sleep(1.0)

            # Click Assignments in whichever root has it
            assign_root = _find_root_with_selector(page, "td.td2_action:has-text('Assignments')", 1, 5000)
            print("DEBUG — assign_root exists:", bool(assign_root))
            rows = []
            if assign_root:
                assign_clicked_in = _go_assignments_anywhere(page)
                page.wait_for_load_state("domcontentloaded"); sleep(0.8)
                # scrape from the root that actually contains assignments (in case nav switched frames)
                assign_root = _find_root_with_selector(page, "xpath=//table[.//th[contains(.,'Assignment')]]", 1, 5000) or assign_root
                rows = _scrape_from_assignments_root(assign_root, s)
            print(f"DEBUG — scraped rows for {s}: {len(rows)}")
            all_rows.extend(rows)

            # Back to picker
            for root in _all_roots(page):
                try:
                    root.get_by_text(re.compile(r"\bHome\b", re.I)).first.click()
                    break
                except: pass
            page.wait_for_load_state("domcontentloaded"); sleep(0.6)

        browser.close()
    return all_rows
