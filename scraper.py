# scraper.py
# Login → Student tile → Assignments → per-class "Print Progress Report" PDF → parse with pdfplumber.

from playwright.sync_api import sync_playwright
from datetime import datetime
import re
import io
import pdfplumber
from time import sleep
from urllib.parse import urljoin

BASE = "https://parentportal.cajonvalley.net/"

TILE_ID = {
    "adrian": "357342",
    "jacob":  "357354",
}

ASSIGNMENTS_HEADER_RE = re.compile(r'^Per:\s*(\S+)\s+(.*)$', re.I)
WS = re.compile(r"\s+")

def _norm(s):
    if s is None: return ""
    s = str(s).replace("\xa0", " ")
    return WS.sub(" ", s).strip()

def _first(root, sel):
    try:
        loc = root.locator(sel).first
        if loc.is_visible(): return loc
    except:
        pass
    return None

def _click(root, sel):
    el = _first(root, sel)
    if el:
        el.click()
        return True
    return False

def _wait_visible(root, sel, timeout=8000):
    try:
        root.locator(sel).first.wait_for(state="visible", timeout=timeout)
        return True
    except:
        return False

def _all_roots(page):
    return [page] + list(page.frames)

# ---------------- PDF parsing ----------------

def parse_progress_pdf(pdf_bytes, default_student=""):
    """
    Returns list of dict rows with keys matching our Sheet header.
    Extracts: Student, Period, Course, Teacher, DueDate, AssignedDate, Assignment,
              PtsPossible, Score, Pct, Status, Comments.
    """
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        student = default_student
        period = ""
        course = ""
        teacher = ""
        for page in pdf.pages:
            text = page.extract_text() or ""
            # Header lines like:
            # Student: Gonzalez-Cuevas, Jacob (357354)
            # Class: T4256YF1-782 Coding 1
            # Period: 2
            # Teacher: Russo, M
            for ln in text.splitlines():
                ln_n = _norm(ln)
                if ln_n.startswith("Student:"):
                    student = _norm(ln_n.split(":", 1)[1])
                if ln_n.startswith("Class:"):
                    course = _norm(ln_n.split(":", 1)[1])
                if ln_n.startswith("Period:"):
                    period = _norm(ln_n.split(":", 1)[1])
                if ln_n.startswith("Teacher:"):
                    teacher = _norm(ln_n.split(":", 1)[1])

            # Extract tables; use line strategies to keep columns straight
            tables = page.extract_tables({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "intersection_tolerance": 5,
            }) or []

            for tbl in tables:
                if not tbl or len(tbl) < 2:
                    continue
                header = [_norm(h) for h in tbl[0]]
                # Heuristic: look for "Assignment" in header row
                if not any("Assignment" in h for h in header):
                    continue

                def idx(*names):
                    for i, h in enumerate(header):
                        for n in names:
                            if re.search(rf"\b{re.escape(n)}\b", h, re.I):
                                return i
                    return None

                c_cat  = idx("Category")
                c_due  = idx("Date Due", "Due Date")
                c_asgn = idx("Assignment")
                c_poss = idx("Pts Possible", "Possible", "Pos")
                c_sc   = idx("Score")
                c_pct  = idx("Pct Score", "Pct")
                c_asgd = idx("Assigned", "Date Assigned")
                c_comm = idx("Comments", "Comment")

                for r in tbl[1:]:
                    cells = [ _norm(x) for x in r ]
                    if not cells or all(not x for x in cells):
                        continue
                    assign = cells[c_asgn] if c_asgn is not None and c_asgn < len(cells) else ""
                    if not assign or assign.lower() == "assignment":
                        continue
                    due   = cells[c_due]  if c_due  is not None and c_due  < len(cells) else ""
                    asgd  = cells[c_asgd] if c_asgd is not None and c_asgd < len(cells) else ""
                    poss  = cells[c_poss] if c_poss is not None and c_poss < len(cells) else ""
                    sc    = cells[c_sc]   if c_sc   is not None and c_sc   < len(cells) else ""
                    pct   = cells[c_pct]  if c_pct  is not None and c_pct  < len(cells) else ""
                    comm  = cells[c_comm] if c_comm is not None and c_comm < len(cells) else ""
                    # Flags
                    flags = []
                    if "missing" in comm.lower() or sc == "0" or pct == "0":
                        flags.append("Missing")
                    try:
                        pct_val = float(pct)
                        if pct_val < 70: flags.append("Low")
                        if pct_val >= 95: flags.append("Win")
                    except:
                        pass

                    rows.append({
                        "ImportedAt": datetime.utcnow().isoformat(),
                        "Student": student or default_student,
                        "Period": period,
                        "Course": course,
                        "Teacher": teacher,
                        "DueDate": due,
                        "AssignedDate": asgd,
                        "Assignment": assign,
                        "PtsPossible": poss,
                        "Score": sc,
                        "Pct": pct,
                        "Status": ",".join(flags),
                        "Comments": comm,
                        "SourceURL": "PDF",
                    })
    return rows

# --------------- scrape via PDFs ----------------

def run_scrape(pin, password, students=("Adrian","Jacob")):
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True, base_url=BASE)
        page = ctx.new_page()

        # Login on the root page you shared
        page.goto("/", wait_until="domcontentloaded")
        print("DEBUG — landed:", page.url)

        _wait_visible(page, "#Pin", 12000)
        _wait_visible(page, "#Password", 12000)
        page.fill("#Pin", str(pin))
        page.fill("#Password", str(password))
        _click(page, "#LoginButton")
        page.wait_for_load_state("domcontentloaded"); sleep(0.8)

        # Ensure we can reach the picker
        page.goto("/Home/PortalMainPage", wait_until="domcontentloaded"); sleep(0.6)

        for s in students:
            sid = TILE_ID.get(s.lower())
            if not sid:
                print(f"DEBUG — no tile id mapping for {s}, skipping"); continue

            # Click student tile
            clicked = _click(page, f"#stuTile_{sid}")
            print(f"DEBUG — clicked tile for {s}: {clicked}")
            page.wait_for_load_state("domcontentloaded"); sleep(0.6)

            # Click Assignments (left menu lives in page; no cross-origin frames)
            # It's a <td class="td2_action">Assignments</td>
            # Search across main + frames just in case.
            clicked_assign = False
            for root in _all_roots(page):
                if _click(root, "td.td2_action:has-text('Assignments')"):
                    clicked_assign = True
                    break
            print("DEBUG — clicked Assignments:", clicked_assign)
            page.wait_for_load_state("domcontentloaded"); sleep(0.6)

            # Find all "print" icons for classes on the Assignments view.
            # They are typically <a> wrapping an <img> whose src/title mentions print.
            print_icons = []
            for root in _all_roots(page):
                try:
                    anchors = root.locator(
                        "xpath=//a[.//img[contains(translate(@src,'PRINT','print'),'print') or "
                        "contains(translate(@title,'PRINT','print'),'print')]]"
                    )
                    cnt = anchors.count()
                    for i in range(cnt):
                        el = anchors.nth(i)
                        if el.is_visible():
                            print_icons.append((root, el))
                except:
                    pass

            print(f"DEBUG — print icons found for {s}: {len(print_icons)}")

            # For each class: open the modal, grab the "Print Progress Report" href, fetch the PDF, parse.
            for idx, (root, a) in enumerate(print_icons):
                try:
                    a.click()
                except:
                    continue

                # Wait for the "Progress Report Terms" modal to appear
                modal_root = None
                for rr in _all_roots(page):
                    if _wait_visible(rr, "xpath=//div[contains(.,'Progress Report Terms')]", timeout=3000):
                        modal_root = rr; break

                if not modal_root:
                    # try a generic dialog selector
                    for rr in _all_roots(page):
                        if _wait_visible(rr, "xpath=//a[contains(.,'Print Progress Report')]", timeout=2000):
                            modal_root = rr; break

                if not modal_root:
                    print("DEBUG — modal not found; skipping one class")
                    continue

                # Prefer the first row (usually "Trimester 1 Progress")
                link = _first(modal_root, "xpath=(//a[contains(.,'Print Progress Report')])[1]")
                if not link:
                    print("DEBUG — 'Print Progress Report' link not visible; skipping")
                    continue

                href = link.get_attribute("href")
                if not href:
                    print("DEBUG — no href on link; skipping")
                    continue

                pdf_url = urljoin(BASE, href)
                # Fetch with session cookies via context.request
                resp = ctx.request.get(pdf_url)
                if resp.ok:
                    pdf_bytes = resp.body()
                    rows = parse_progress_pdf(pdf_bytes, default_student=s)
                    print(f"DEBUG — parsed rows from class {idx+1} for {s}: {len(rows)}")
                    all_rows.extend(rows)
                else:
                    print(f"DEBUG — PDF request failed status={resp.status} url={pdf_url}")

                # Close modal (X button or ESC)
                closed = _click(modal_root, "css=button, .ui-dialog-titlebar-close, .close") or _click(page, "text=×")
                if not closed:
                    try: page.keyboard.press("Escape")
                    except: pass
                sleep(0.3)

            # Back to picker for next student
            page.goto("/Home/PortalMainPage", wait_until="domcontentloaded"); sleep(0.5)

        browser.close()
    return all_rows
