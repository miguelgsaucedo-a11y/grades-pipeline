# scraper.py
# Fully self-contained scraper for Q ParentConnection (Cajon Valley).
# - Handles varied login field names (username/pin/login + password) and possible iframes
# - Navigates to each student card
# - Expands Assignments and scrapes every class table
# - Normalizes numeric fields and derives Status flags

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

def _first_visible(page_or_frame, selectors):
    """Return first locator that is visible among selectors on a page/frame; else None."""
    for sel in selectors:
        try:
            loc = page_or_frame.locator(sel).first
            if loc.is_visible():
                return loc
        except Exception:
            pass
    return None

def _try_fill(page, selectors, value):
    """Try to fill in main page or any frame."""
    # main page
    loc = _first_visible(page, selectors)
    if loc:
        loc.fill(value)
        return True
    # frames
    for fr in page.frames:
        loc = _first_visible(fr, selectors)
        if loc:
            loc.fill(value)
            return True
    return False

def _try_click(page, selectors_or_texts):
    """
    Try CSS/xpath/text selectors first on page then frames.
    Also supports role-based clicks by text using regex.
    """
    # treat strings that look like selectors as selectors; others as text patterns
    selector_like = []
    text_like = []
    for s in selectors_or_texts:
        if any(x in s for x in ('#', '.', '[', ']', 'css=', 'xpath=', 'text=', '=')):
            selector_like.append(s)
        else:
            text_like.append(s)

    # selectors on main page
    loc = _first_visible(page, selector_like)
    if loc:
        loc.click()
        return True

    # role-based by text (buttons/links)
    for t in text_like:
        try:
            pat = re.compile(t, re.I)
        except re.error:
            pat = re.compile(re.escape(t), re.I)
        try:
            btn = page.get_by_role("button", name=pat)
            if btn.is_visible():
                btn.click()
                return True
        except Exception:
            pass
        try:
            lnk = page.get_by_role("link", name=pat)
            if lnk.is_visible():
                lnk.click()
                return True
        except Exception:
            pass

    # frames
    for fr in page.frames:
        loc = _first_visible(fr, selector_like)
        if loc:
            loc.click()
            return True
        for t in text_like:
            try:
                pat = re.compile(t, re.I)
            except re.error:
                pat = re.compile(re.escape(t), re.I)
            try:
                btn = fr.get_by_role("button", name=pat)
                if btn.is_visible():
                    btn.click()
                    return True
            except Exception:
                pass
            try:
                lnk = fr.get_by_role("link", name=pat)
                if lnk.is_visible():
                    lnk.click()
                    return True
            except Exception:
                pass
    return False

def _extract_sections(container):
    """
    Return a list of section elements that look like class blocks:
    Each with "Per:" in header and a table following it.
    """
    # fallback strategy: look for any div containing "Per:" text and a table
    sections = []
    blocks = container.locator("xpath=//div[.//text()[contains(.,'Per:')]]").all()
    for b in blocks:
        try:
            if "Per:" in (b.text_content() or "") and b.locator("css=table").first.count() > 0:
                sections.append(b)
        except Exception:
            continue
    return sections

def scrape_student_assignments(page, student_name):
    """
    Assumes we're already on the student's page.
    Scrapes the Assignments widget for all classes (Per: ... sections).
    """
    # Scroll to Assignments and expand
    try:
        page.locator("text=Assignments").first.scroll_into_view_if_needed()
    except Exception:
        pass

    _try_click(page, ["text=Show All", "text=Show All Assignments"])

    results = []
    sections = _extract_sections(page)
    for sec in sections:
        try:
            header = sec.text_content() or ""
        except Exception:
            continue
        if "Per:" not in header:
            continue

        first_line = next((ln for ln in header.splitlines() if ln.strip()), "")
        m = ASSIGNMENTS_HEADER_RE.search(first_line)
        if not m:
            # sometimes the header is on the second line
            lines = [ln for ln in header.splitlines() if ln.strip()]
            for ln in lines[:3]:
                m = ASSIGNMENTS_HEADER_RE.search(ln)
                if m:
                    break
        if not m:
            continue

        period = m.group(1).strip()
        course = m.group(2).strip()

        teacher = ""
        for ln in header.splitlines():
            ln = ln.strip()
            if ln.lower().startswith("teacher:"):
                teacher = ln.split(":", 1)[1].strip()
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
                "SourceURL": page.url
            })
    return results

def _on_student_picker(page):
    """Heuristic: student cards visible (links or buttons containing student names)."""
    try:
        # look for the 'Please Select a Student' prompt or multiple student tiles
        if page.locator("text=Please Select a Student").first.is_visible():
            return True
    except Exception:
        pass
    # otherwise, assume not
    return False

def run_scrape(username, password, students=("Adrian","Jacob")):
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage", wait_until="domcontentloaded")
        sleep(1)

        # Sometimes a pre-login "Sign In / Log In" link/button exists
        _try_click(page, ["Sign In", "Log In", "Login"])

        # Fill username/login/pin
        _try_fill(page, [
            'input[name="username"]', 'input#username',
            'input[name="UserName"]', 'input#UserName',
            'input[name="Pin"]', 'input#Pin',
            'input[name="Login"]', 'input#Login',
            'input[type="text"]'
        ], username)

        # Fill password
        _try_fill(page, [
            'input[name="password"]', 'input#password',
            'input[name="Password"]', 'input#Password',
            'input[type="password"]'
        ], password)

        # Click login/submit
        _try_click(page, [
            "Sign In", "Log In", "Login",
            'css=input[type="submit"]', 'css=button[type="submit"]', "#btnLogin"
        ])

        page.wait_for_load_state("domcontentloaded")
        sleep(1)

        # If we landed somewhere else, try navigating to Home/PortalMainPage again
        if not _on_student_picker(page):
            _try_click(page, ["Home"])
            sleep(0.5)
            page.goto("https://parentportal.cajonvalley.net/Home/PortalMainPage", wait_until="domcontentloaded")
            sleep(0.5)

        # Iterate each student
        for s in students:
            # Ensure we’re on the picker; if not, click Home
            if not _on_student_picker(page):
                _try_click(page, ["Home"])
                page.wait_for_load_state("domcontentloaded")
                sleep(0.5)

            # Click the student card by visible label
            clicked = _try_click(page, [s])
            if not clicked:
                # try partial name (first name only)
                first = s.split()[0]
                _try_click(page, [first])

            page.wait_for_load_state("domcontentloaded")
            sleep(0.5)

            # Scrape
            rows = scrape_student_assignments(page, s)
            all_rows.extend(rows)

            # Back to Home to choose next student
            _try_click(page, ["Home"])
            page.wait_for_load_state("domcontentloaded")
            sleep(0.5)

        browser.close()
    return all_rows
