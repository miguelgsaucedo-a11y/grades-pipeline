# scraper.py
# Login -> set student -> Assignments -> build PrintProgressReport URL per class -> fetch PDF -> parse via pdfplumber

from playwright.sync_api import sync_playwright
from datetime import datetime
import re
import io
import pdfplumber
from time import sleep
from urllib.parse import urljoin

BASE = "https://parentportal.cajonvalley.net/"

# Use the data-stuuniq values from your HTML (NOT the 357342/357354 image ids)
STUUNIQ = {
    "adrian": "1547500",
    "jacob":  "1546467",
}

WS = re.compile(r"\s+")


def _norm(s):
    if s is None:
        return ""
    s = str(s).replace("\xa0", " ")
    return WS.sub(" ", s).strip()


def _first(root, sel):
    try:
        loc = root.locator(sel).first
        if loc.is_visible():
            return loc
    except:
        pass
    return None


def _wait_visible(root, sel, timeout=10000):
    try:
        root.locator(sel).first.wait_for(state="visible", timeout=timeout)
        return True
    except:
        return False


# ---------------- PDF parsing ----------------
def parse_progress_pdf(pdf_bytes, default_student="", course_hint="", period_hint="", teacher_hint=""):
    """
    Returns list of dict rows with keys matching our Sheet header.
    Extracts: Student, Period, Course, Teacher, DueDate, AssignedDate, Assignment,
              PtsPossible, Score, Pct, Status, Comments.
    """
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        student = default_student
        period = period_hint
        course = course_hint
        teacher = teacher_hint

        for page in pdf.pages:
            text = page.extract_text() or ""
            for ln in text.splitlines():
                ln_n = _norm(ln)
                if ln_n.startswith("Student:"):
                    student = _norm(ln_n.split(":", 1)[1])
                elif ln_n.startswith("Class:"):
                    course = _norm(ln_n.split(":", 1)[1])
                elif ln_n.startswith("Period:"):
                    period = _norm(ln_n.split(":", 1)[1])
                elif ln_n.startswith("Teacher:"):
                    teacher = _norm(ln_n.split(":", 1)[1])

            tables = page.extract_tables({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "intersection_tolerance": 5,
            }) or []

            for tbl in tables:
                if not tbl or len(tbl) < 2:
                    continue
                header = [_norm(h) for h in tbl[0]]
                if not any("Assignment" in h for h in header):
                    continue

                def idx(*names):
                    for i, h in enumerate(header):
                        for n in names:
                            if re.search(rf"\b{re.escape(n)}\b", h, re.I):
                                return i
                    return None

                c_due  = idx("Date Due", "Due Date")
                c_asgd = idx("Assigned", "Date Assigned")
                c_asgn = idx("Assignment")
                c_poss = idx("Pts Possible", "Possible")
                c_sc   = idx("Score")
                c_pct  = idx("Pct Score", "Pct")
                c_comm = idx("Comments", "Comment")

                for r in tbl[1:]:
                    cells = [_norm(x) for x in r]
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

                    flags = []
                    if "missing" in comm.lower() or sc == "0" or pct == "0":
                        flags.append("Missing")
                    try:
                        if pct.endswith("%"):
                            pct_val = float(pct.replace("%", ""))
                        else:
                            pct_val = float(pct)
                        if pct_val < 70:
                            flags.append("Low")
                        if pct_val >= 95:
                            flags.append("Win")
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


# --------------- main scrape ----------------
def run_scrape(pin, password, students=("Adrian", "Jacob")):
    """
    Returns a list of row dicts ready for Google Sheets.
    """
    all_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True, base_url=BASE)
        page = ctx.new_page()

        # Login
        page.goto("/", wait_until="domcontentloaded")
        print("DEBUG — landed:", page.url)

        _wait_visible(page, "#Pin", 15000)
        _wait_visible(page, "#Password", 15000)
        page.fill("#Pin", str(pin))
        page.fill("#Password", str(password))
        page.click("#LoginButton")
        page.wait_for_load_state("domcontentloaded"); sleep(0.8)

        # Ensure we're on the main portal page
        page.goto("/Home/PortalMainPage", wait_until="domcontentloaded"); sleep(0.6)

        for s in students:
            key = s.strip().lower()
            stuuniq = STUUNIQ.get(key)
            if not stuuniq:
                print(f"DEBUG — no STUUNIQ mapping for {s}, skipping")
                continue

            # Switch the active student directly
            page.goto(f"/StudentBanner/SetStudentBanner/{stuuniq}", wait_until="domcontentloaded")
            page.goto("/Home/PortalMainPage", wait_until="domcontentloaded"); sleep(0.6)

            # Assignments area exists on the page; find tables
            if not _wait_visible(page, "#SP_Assignments", 8000):
                print(f"DEBUG — Assignments container not visible for {s}")
                continue

            tbls = page.locator("#SP_Assignments table[id^='tblAssign_']")
            count = tbls.count()
            print(f"DEBUG — class tables for {s}: {count}")

            for i in range(count):
                tbl = tbls.nth(i)

                # mstuniq is in the id "tblAssign_<mstuniq>"
                tid = tbl.get_attribute("id") or ""
                m = re.search(r"tblAssign_(\d+)", tid)
                if not m:
                    continue
                mstuniq = m.group(1)

                # Get period/course/teacher hints from the table content
                period = ""
                course = ""
                teacher = ""

                # Caption text has "Per : X   Course (Code)"
                cap = _first(tbl, "caption")
                if cap:
                    cap_text = cap.inner_text()
                    cap_text = _norm(cap_text)
                    # Try to split "Per : X   <course>"
                    if "Per" in cap_text:
                        try:
                            after = cap_text.split("Per", 1)[1]
                            after = after.split(":", 1)[1]
                            parts = after.split(None, 1)  # ["X", "Course..."]
                            period = parts[0]
                            if len(parts) > 1:
                                course = parts[1]
                        except:
                            pass

                # Teacher anchor has aria-label starting with "Teacher:"
                a_teacher = _first(tbl, "a[aria-label^='Teacher:']")
                if a_teacher:
                    teacher = _norm(a_teacher.inner_text())

                # The hidden input inside header carries the term code, e.g., TP1
                termc = ""
                hid = _first(tbl, "input[id^='showmrktermc_']")
                if hid:
                    termc = (hid.get_attribute("value") or "").strip()

                if not termc:
                    # Safe default, but log it
                    print(f"DEBUG — term code missing for mstuniq={mstuniq}; defaulting TP1")
                    termc = "TP1"

                pdf_path = f"/Home/PrintProgressReport/{mstuniq}^{termc}"
                pdf_url = urljoin(BASE, pdf_path)

                resp = ctx.request.get(pdf_url)
                if not resp.ok:
                    print(f"DEBUG — PDF GET failed {resp.status} for {pdf_url}")
                    continue

                pdf_bytes = resp.body()
                rows = parse_progress_pdf(
                    pdf_bytes,
                    default_student=s,
                    course_hint=course,
                    period_hint=period,
                    teacher_hint=teacher,
                )
                print(f"DEBUG — parsed rows for {s}, class mstuniq={mstuniq}: {len(rows)}")
                all_rows.extend(rows)

        browser.close()

    return all_rows
