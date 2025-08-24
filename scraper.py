# scraper.py
# Cross-frame student tile + assignments scraper for Q ParentConnection (Cajon Valley)

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
    return [page] + list(page.frames)

def _first_visible(root, sel):
    try:
        loc = root.locator(sel).first
        if loc.is_visible(): return loc
    except: pass
    return None

def _find_root_with_selector(page, sel, min_visible=1, timeout_ms=5000):
    elapsed = 0
    step = 200
    while elapsed < timeout_ms:
        for r in _all_roots(page):
            try:
                locs = r.locator(sel)
                count = locs.count()
                vis = 0
                for i in range(min(20, count)):
                    if locs.nth(i).is_visible(): vis += 1
                if vis >= min_visible:
                    return r
            except: 
                pass
        sleep(step/1000); elapsed += step
    return None

def _click_student_tile_in(root, nickname, id_hint=None):
    # Prefer exact tile id if provided
    if id_hint:
        loc = _first_visible(root, f"#stuTile_{id_hint}")
        if loc:
            loc.click(); return True
    # Use nickname inside the tile
    sel = f"css=div.studentTile:has(span.tileStudentNickname:has-text('{nickname}'))"
    loc = _first_visible(root, sel)
    if loc:
        loc.click(); return True
    # Fallback: any studentTile that contains the nickname text anywhere
    sel2 = f"css=div.studentTile:has-text('{nickname}')"
    loc = _first_visible(root, sel2)
    if loc:
        loc.click(); return True
    return False

def _go_assignments_in(root):
    # Left menu cells look like <td class="td2_action">Assignments</td>
    loc = _first_visible(root, "td.td2_action:has-text('Assignments')")
    if loc:
        loc.click()
        return True
    # fallback
    try:
        root.get_by_text(re.compile(r"\bAssignments\b", re.I)).first.click()
        return True
    except:
        return False

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
        low  = (pct is not None and pct < 70)
        miss = ("missing" in (comm or "").lower()) or score == 0 or pct == 0
        win  = (pct is not None and pct >= 95)
        if miss: flags.append("Missing")
        if low:  flags.append("Low")
        if win:  flags.append("Win")

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

def _scrape_from_assignments_root(root, student_name):
    results = []
    sections = _extract_sections(root)
    print(f"DEBUG — sections (assignments) for {student_name}: {len(sections)}")

    if not sections:
        # fallback: any table that has 'Assignment' header
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
        try:
            header = sec.text_content() or ""
        except:
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

def run_scrape(username, password, students=("Adrian","Jacob")):
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage", wait_until="domcontentloaded")
        print("DEBUG — landed:", page.url)
        sleep(0.8)

        # Pre-login link sometimes present
        try:
            page.get_by_text(re.compile(r"\b(Sign In|Log In|Login)\b", re.I)).first.click()
        except: pass

        # Fill username/login/pin (page or frames)
        for root in _all_roots(page):
            for sel in [
                'input[name="username"]','input#username',
                'input[name="UserName"]','input#UserName',
                'input[name="Pin"]','input#Pin',
                'input[name="Login"]','input#Login',
                'input[type="text"]'
            ]:
                try:
                    el = root.locator(sel).first
                    if el.is_visible(): el.fill(username); raise StopIteration
                except StopIteration:
                    break
                except: pass
            else:
                continue
            break

        # Fill password
        for root in _all_roots(page):
            for sel in [
                'input[name="password"]','input#password',
                'input[name="Password"]','input#Password',
                'input[type="password"]'
            ]:
                try:
                    el = root.locator(sel).first
                    if el.is_visible(): el.fill(password); raise StopIteration
                except StopIteration:
                    break
                except: pass
            else:
                continue
            break

        # Click login/submit wherever visible
        clicked_login = False
        for root in _all_roots(page):
            for click_sel in [
                "text=Sign In","text=Log In","text=Login",
                'css=input[type="submit"]','css=button[type="submit"]','#btnLogin'
            ]:
                try:
                    el = root.locator(click_sel).first
                    if el.is_visible():
                        el.click(); clicked_login = True; raise StopIteration
                except StopIteration:
                    break
                except: pass
            if clicked_login: break

        page.wait_for_load_state("domcontentloaded"); sleep(1)

        # Find the root that actually contains the student tiles
        tiles_root = _find_root_with_selector(page, "css=div.studentTile", min_visible=1, timeout_ms=5000)
        print("DEBUG — tiles_root:", "main" if tiles_root is page else ("frame" if tiles_root else "none"))

        for s in students:
            if not tiles_root:
                # try Home and look again
                for root in _all_roots(page):
                    try:
                        root.get_by_text(re.compile(r"\bHome\b", re.I)).first.click()
                        break
                    except: pass
                page.wait_for_load_state("domcontentloaded"); sleep(0.6)
                tiles_root = _find_root_with_selector(page, "css=div.studentTile", 1, 3000)
                print("DEBUG — after Home, tiles_root:", "main" if tiles_root is page else ("frame" if tiles_root else "none"))

            clicked = False
            if tiles_root:
                # If you want to hard-hint IDs, uncomment the next two lines with your numbers
                # id_hint = "357342" if s.lower().startswith("adrian") else ("357354" if s.lower().startswith("jacob") else None)
                id_hint = None
                clicked = _click_student_tile_in(tiles_root, s, id_hint=id_hint)

            print(f"DEBUG — clicked tile for {s}: {clicked}")
            page.wait_for_load_state("domcontentloaded"); sleep(1.0)

            # Find root that contains Assignments and click it
            assign_root = _find_root_with_selector(page, "td.td2_action:has-text('Assignments')", 1, 4000)
            print("DEBUG — assign_root:", "main" if assign_root is page else ("frame" if assign_root else "none"))
            if assign_root:
                _go_assignments_in(assign_root)
                page.wait_for_load_state("domcontentloaded"); sleep(0.8)
                rows = _scrape_from_assignments_root(assign_root, s)
            else:
                rows = []

            print(f"DEBUG — scraped rows for {s}: {len(rows)}")
            all_rows.extend(rows)

            # Navigate back to picker for next student
            for root in _all_roots(page):
                try:
                    root.get_by_text(re.compile(r"\bHome\b", re.I)).first.click()
                    break
                except: pass
            page.wait_for_load_state("domcontentloaded"); sleep(0.6)

        browser.close()
    return all_rows
