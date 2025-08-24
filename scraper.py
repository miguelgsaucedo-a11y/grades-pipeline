# scraper.py
from playwright.sync_api import sync_playwright
from datetime import datetime
import re

ASSIGNMENTS_HEADER_RE = re.compile(r'^Per:\s*(\S+)\s+(.*)$', re.I)
NUM_CLEAN = re.compile(r'[^0-9.\-]')

def _to_num(v):
    if v is None: return None
    s = str(v).strip()
    if not s: return None
    try:
        return float(NUM_CLEAN.sub('', s))
    except:
        return None

def scrape_for_student(page, student_label):
    # Click the student card by visible name
    page.get_by_role("link", name=re.compile(student_label, re.I)).click()

    # Scroll to Assignments block; expand "Show All" if present
    page.locator("text=Assignments").first.scroll_into_view_if_needed()
    show_all = page.locator("text=Show All").first
    if show_all.is_visible():
        show_all.click()

    results = []
    # Each section starts with a line like "Per: 2  Coding 1 (...)"
    sections = page.locator("xpath=//div[.//text()[contains(.,'Per:')]]").all()
    for sec in sections:
        header = sec.text_content() or ""
        if "Per:" not in header: 
            continue

        first_line = header.splitlines()[0]
        m = ASSIGNMENTS_HEADER_RE.search(first_line)
        if not m:
            continue
        period, course = m.group(1).strip(), m.group(2).strip()
        teacher = None
        for line in header.splitlines():
            if line.strip().lower().startswith("teacher:"):
                teacher = line.split(":",1)[1].strip()
                break

        # Find the first table under this section
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

            status_flags = []
            lower = (comm or "").lower()
            if "missing" in lower or score == 0 or pct == 0:
                status_flags.append("Missing")
            if pct is not None and pct < 70:
                status_flags.append("Low")
            if pct is not None and pct >= 95:
                status_flags.append("Win")

            results.append({
                "ImportedAt": imported_at,
                "Student": student_label,
                "Period": period,
                "Course": course,
                "Teacher": teacher or "",
                "DueDate": due,
                "AssignedDate": asgnd,
                "Assignment": assignment,
                "PtsPossible": poss if poss is not None else "",
                "Score": score if score is not None else "",
                "Pct": pct if pct is not None else "",
                "Status": ",".join(status_flags),
                "Comments": comm,
                "SourceURL": page.url
            })
    return results

def run_scrape(username, password, students=("Adrian","Jacob")):
    from time import sleep
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage")

        # Adjust selectors if your portal labels differ
        page.fill('input[name="username"], input#username', username)
        page.fill('input[name="password"], input#password', password)
        # Try common login button texts
        if page.get_by_role("button", name=re.compile("sign in|log in|login", re.I)).is_visible():
            page.get_by_role("button", name=re.compile("sign in|log in|login", re.I)).click()
        else:
            page.click("text=Sign In, Log In, Login")

        for s in students:
            # If we’re not on the student selection page, click Home first
            if page.locator("text=Home").first.is_visible():
                page.locator("text=Home").first.click()
            # Click student card and scrape
            page.get_by_role("link", name=re.compile(s, re.I)).click()
            rows = scrape_for_student(page, s)
            all_rows.extend(rows)
            # Go back to pick the other student
            page.locator("text=Home").first.click()

        browser.close()
    return all_rows
