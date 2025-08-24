# scraper.py
# Frame-aware scraper for Q ParentConnection (Cajon Valley).
# - Robust login (multiple selectors + iframes)
# - Finds the iframe that contains "Assignments"
# - Scrapes every class table and normalizes fields
# - Emits DEBUG logs so we can see what it finds

from playwright.sync_api import sync_playwright
from datetime import datetime
import re
from time import sleep

ASSIGNMENTS_HEADER_RE = re.compile(r'^Per:\s*(\S+)\s+(.*)$', re.I)
NUM_CLEAN = re.compile(r'[^0-9.\-]')

def _to_num(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(NUM_CLEAN.sub('', s))
    except Exception:
        return None

def _first_visible(root, selectors):
    """Return first locator visible among selectors on a given page/frame."""
    for sel in selectors:
        try:
            loc = root.locator(sel).first
            if loc.is_visible():
                return loc
        except Exception:
            pass
    return None

def _try_fill_anywhere(page, selectors, value):
    """Try on main page; if not, try each frame."""
    loc = _first_visible(page, selectors)
    if loc:
        loc.fill(value); return True
    for fr in page.frames:
        loc = _first_visible(fr, selectors)
        if loc:
            loc.fill(value); return True
    return False

def _try_click_anywhere(page, selectors_or_texts):
    """Try CSS/text on page, then in frames."""
    selector_like, text_like = [], []
    for s in selectors_or_texts:
        if any(x in s for x in ('#','.', '[',']','css=','xpath=','text=','=')):
            selector_like.append(s)
        else:
            text_like.append(s)

    # page selectors
    loc = _first_visible(page, selector_like)
    if loc: loc.click(); return True

    # page by role/text
    for t in text_like:
        pat = re.compile(t, re.I)
        try:
            btn = page.get_by_role("button", name=pat)
            if btn.is_visible(): btn.click(); return True
        except Exception: pass
        try:
            lnk = page.get_by_role("link", name=pat)
            if lnk.is_visible(): lnk.click(); return True
        except Exception: pass

    # frames
    for fr in page.frames:
        loc = _first_visible(fr, selector_like)
        if loc: loc.click(); return True
        for t in text_like:
            pat = re.compile(t, re.I)
            try:
                btn = fr.get_by_role("button", name=pat)
                if btn.is_visible(): btn.click(); return True
            except Exception: pass
            try:
                lnk = fr.get_by_role("link", name=pat)
                if lnk.is_visible(): lnk.click(); return True
            except Exception: pass
    return False

def _find_assignments_frame(page):
    """Return the frame whose visible text contains 'Assignments'."""
    try:
        # quick win: if main page shows it, use page
        if page.locator("text=Assignments").first.is_visible():
            return page
    except Exception:
        pass
    for fr in page.frames:
        try:
            if fr.locator("text=Assignments").first.is_visible():
                return fr
        except Exception:
            continue
    # fallback to page
    return page

def _extract_sections(root):
    """Return likely class blocks that contain 'Per:' and a table."""
    sections = []
    # any div that has "Per:" somewhere and also contains a table
    blocks = root.locator("xpath=//div[.//text()[contains(.,'Per:')]]").all()
    for b in blocks:
        try:
            if b.locator("css=table").first.count() > 0:
                sections.append(b)
        except Exception:
            continue
    return sections

def _scrape_tables_fallback(root):
    """
    Fallback if we can't find 'sections':
    get any table that looks like the Assignments table (has 'Assignment' header),
    then climb to a parent that contains a 'Per:' header to parse course/period/teacher.
    """
    results = []
    tables = root.locator("xpath=//table[.//th[contains(.,'Assignment')]]").all()
    print(f"DEBUG — fallback tables found: {len(tables)}")
    for t in tables:
        try:
            # find nearest ancestor that has 'Per:' somewhere
            container = t.locator("xpath=ancestor::div[.//text()[contains(.,'Per:')]][1]").first
            header = container.text_content() or ""
        except Exception:
            header = ""
        period, course, teacher = "", "", ""
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

        rows = t.locator("tr").all()
        if len(rows) < 2: 
            continue
        headers = [c.inner_text().strip() for c in rows[0].locator("th,td").all()]
        def col(*names):
            for n in names:
                if n in headers:
                    return headers.index(n)
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
            if not tds: 
                continue
            assignment = tds[c_asgn] if c_asgn is not None and c_asgn < len(tds) else ""
            if not assignment or assignment.lower() == "assignment":
                continue

            due   = tds[c_due]   if c_due   is not None and c_due   < len(tds) else ""
            asgnd = tds[c_asgnd] if c_asgnd is not None and c_asgnd < len(tds) else ""
            poss  = _to_num(tds[c_poss])  if c_poss  is not None and c_poss  < len(tds) else None
            score = _to_num(tds[c_score]) if c_score is not None and c_score < len(tds) else None
            pct   = _to_num(tds[c_pct])   if c_pct   is not None and c_pct   < len(tds) else None
            comm  = tds[c_comm] if c_comm is not None and c_comm < len(tds) else ""

            status = []
            lower = (comm or "").lower()
            if "missing" in lower or score == 0 or pct == 0:
                status.append("Missing")
            if pct is not None and pct < 70:
                status.append("Low")
            if pct is not None and pct >= 95:
                status.append("Win")

            results.append({
                "ImportedAt": imported_at,
                "Student": "",  # filled by caller
                "Period": period,
                "Course": course,
                "Teacher": teacher,
                "DueDate": due,
                "AssignedDate": asgnd,
                "Assignment": assignment,
                "PtsPossible": poss if poss is not None else "",
                "Score": score if score is not None else "",
                "Pct": pct if pct is not None else "",
                "Status": ",".join(status),
                "Comments": comm,
                "SourceURL": root.url if hasattr(root, "url") else ""
            })
    return results

def scrape_student_assignments(page, student_name):
    """
    Assumes we're on the student's page. Finds the Assignments frame and scrapes within it.
    """
    root = _find_assignments_frame(page)
    print("DEBUG — using frame:", "main-page" if root is page else "iframe")

    # Try to bring the Assignments header into view and expand "Show All"
    try:
        root.locator("text=Assignments").first.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        sa = root.locator("text=Show All").first
        if sa.is_visible():
            sa.click()
    except Exception:
        pass

    results = []
    sections = _extract_sections(root)
    print(f"DEBUG — sections found: {len(sections)}")
    if not sections:
        # fallback: scan tables directly
        results = _scrape_tables_fallback(root)
        # stamp student name
        for r in results: r["Student"] = student_name
        return results

    for sec in sections:
        try:
            header = sec.text_content() or ""
        except Exception:
            continue
        if "Per:" not in header:
            continue

        # parse first 1–3 lines to get "Per: X Course"
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
                teacher = ln.split(":",1)[1].strip()
                break

        table = sec.locator("css=table").first
        if not table or not table.is_visible():
            continue

        rows = table.locator("tr").all()
        if len(rows) < 2:
            continue

        headers = [c.inner_text().strip() for c in rows[0].locator("th,td").all()]
        def col(*names):
            for n in names:
                if n in headers:
                    return headers.index(n)
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
            if not tds:
                continue
            assignment = tds[c_asgn] if c_asgn is not None and c_asgn < len(tds) else ""
            if not assignment or assignment.lower() == "assignment":
                continue

            due   = tds[c_due]   if c_due   is not None and c_due   < len(tds) else ""
            asgnd = tds[c_asgnd] if c_asgnd is not None and c_asgnd < len(tds) else ""
            poss  = _to_num(tds[c_poss])  if c_poss  is not None and c_poss  < len(tds) else None
            score = _to_num(tds[c_score]) if c_score is not None and c_score < len(tds) else None
            pct   = _to_num(tds[c_pct])   if c_pct   is not None and c_pct   < len(tds) else None
            comm  = tds[c_comm] if c_comm is not None and c_comm < len(tds) else ""

            status = []
            lower = (comm or "").lower()
            if "missing" in lower or score == 0 or pct == 0:
                status.append("Missing")
            if pct is not None and pct < 70:
                status.append("Low")
            if pct is not None and pct >= 95:
                status.append("Win")

            results.append({
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
                "Status": ",".join(status),
                "Comments": comm,
                "SourceURL": root.url if hasattr(root, "url") else ""
            })
    return results

def _on_student_picker(page):
    try:
        if page.locator("text=Please Select a Student").first.is_visible():
            return True
    except Exception:
        pass
    return False

def run_scrape(username, password, students=("Adrian","Jacob")):
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage", wait_until="domcontentloaded")
        sleep(1)

        _try_click_anywhere(page, ["Sign In", "Log In", "Login"])

        # Fill username/login/pin
        _try_fill_anywhere(page, [
            'input[name="username"]','input#username',
            'input[name="UserName"]','input#UserName',
            'input[name="Pin"]','input#Pin',
            'input[name="Login"]','input#Login',
            'input[type="text"]'
        ], username)

        # Fill password
        _try_fill_anywhere(page, [
            'input[name="password"]','input#password',
            'input[name="Password"]','input#Password',
            'input[type="password"]'
        ], password)

        # Click login/submit
        _try_click_anywhere(page, [
            "Sign In","Log In","Login",
            'css=input[type="submit"]','css=button[type="submit"]','#btnLogin'
        ])

        page.wait_for_load_state("domcontentloaded"); sleep(1)

        if not _on_student_picker(page):
            _try_click_anywhere(page, ["Home"])
            page.wait_for_load_state("domcontentloaded"); sleep(0.5)

        for s in students:
            if not _on_student_picker(page):
                _try_click_anywhere(page, ["Home"])
                page.wait_for_load_state("domcontentloaded"); sleep(0.5)

            clicked = _try_click_anywhere(page, [s, s.split()[0]])
            if not clicked:
                print(f"DEBUG — could not click student card for {s}")
            page.wait_for_load_state("domcontentloaded"); sleep(0.8)

            rows = scrape_student_assignments(page, s)
            print(f"DEBUG — scraped rows for {s}: {len(rows)}")
            all_rows.extend(rows)

            _try_click_anywhere(page, ["Home"])
            page.wait_for_load_state("domcontentloaded"); sleep(0.5)

        browser.close()
    return all_rows
