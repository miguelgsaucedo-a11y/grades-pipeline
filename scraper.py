# scraper.py
# Login -> find portal iframe -> set student -> scrape class tables -> fetch PDFs -> parse with pdfplumber

from playwright.sync_api import sync_playwright
from datetime import datetime
from urllib.parse import urljoin
import pdfplumber, io, re, time

BASE = "https://parentportal.cajonvalley.net/"

# Use data-stuuniq values from your HTML
STUUNIQ = {
    "adrian": "1547500",
    "jacob":  "1546467",
}

# expected banner ids for sanity-check after switching students
EXPECTED_HSTUDENTID = {
    "adrian": "357342",
    "jacob":  "357354",
}

WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    if s is None:
        return ""
    return WS.sub(" ", str(s).replace("\xa0", " ")).strip()


def get_portal_frame(page):
    """
    Return the iframe that actually contains the PortalMainPage content.
    If none found, fall back to the page itself.
    """
    # Give the frame a moment to appear
    time.sleep(0.4)
    for fr in page.frames:
        u = (fr.url or "")
        if "/Home/PortalMainPage" in u or "parentportal.cajonvalley.net" in u:
            return fr
    return page


# ---------------- PDF parsing ----------------
def parse_progress_pdf(pdf_bytes, default_student="", course_hint="", period_hint="", teacher_hint=""):
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

                def col(*names):
                    for i, h in enumerate(header):
                        for n in names:
                            if re.search(rf"\b{re.escape(n)}\b", h, re.I):
                                return i
                    return None

                c_due  = col("Date Due", "Due Date")
                c_asgd = col("Assigned", "Date Assigned")
                c_asgn = col("Assignment")
                c_poss = col("Pts Possible", "Possible")
                c_sc   = col("Score")
                c_pct  = col("Pct Score", "Pct")
                c_comm = col("Comments", "Comment")

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
                        val = float(pct.replace("%", "")) if pct.endswith("%") else float(pct)
                        if val < 70:
                            flags.append("Low")
                        if val >= 95:
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
    all_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True, base_url=BASE)
        page = ctx.new_page()

        # Login
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#Pin", timeout=15000)
        page.wait_for_selector("#Password", timeout=15000)
        page.fill("#Pin", str(pin))
        page.fill("#Password", str(password))
        page.click("#LoginButton")
        # Land on portal (outer doc may host an iframe)
        page.goto("/Home/PortalMainPage", wait_until="domcontentloaded")
        time.sleep(0.6)

        for s in students:
            key = s.strip().lower()
            stuuniq = STUUNIQ.get(key)
            if not stuuniq:
                print(f"DEBUG — no STUUNIQ mapping for {s}, skipping")
                continue

            # Switch active student at the top level
            page.goto(f"/StudentBanner/SetStudentBanner/{stuuniq}", wait_until="domcontentloaded")
            page.goto("/Home/PortalMainPage", wait_until="domcontentloaded")
            time.sleep(0.6)

            fr = get_portal_frame(page)

            # Verify which student is active via hidden banner field
            hsid = ""
            try:
                fr.wait_for_selector("#hStudentID", timeout=8000)
                hsid = fr.locator("#hStudentID").get_attribute("value") or ""
            except:
                pass
            print(f"DEBUG — hStudentID for {s}: {hsid}")

            # Make sure Assignments area is present; if not, click the left menu
            have_assign = False
            try:
                fr.wait_for_selector("#SP_Assignments", timeout=8000)
                have_assign = True
            except:
                # try opening the menu + clicking Assignments row
                try:
                    fr.locator("#menuTab").click()
                    time.sleep(0.2)
                    fr.locator("#Assignments").click()
                    time.sleep(0.6)
                    fr.wait_for_selector("#SP_Assignments", timeout=8000)
                    have_assign = True
                except:
                    have_assign = False

            if not have_assign:
                print(f"DEBUG — Assignments container NOT visible for {s}")
                continue

            # Find class tables inside Assignments (must query INSIDE the frame)
            tables = fr.locator("#SP_Assignments table[id^='tblAssign_']")
            count = tables.count()
            print(f"DEBUG — class tables for {s}: {count}")

            for i in range(count):
                tbl = tables.nth(i)
                tid = tbl.get_attribute("id") or ""
                m = re.search(r"tblAssign_(\d+)", tid)
                if not m:
                    continue
                mstuniq = m.group(1)

                # Hints: period/course/teacher
                period = ""
                course = ""
                teacher = ""
                try:
                    cap = tbl.locator("caption").first
                    if cap.is_visible():
                        ctext = _norm(cap.inner_text())
                        if "Per" in ctext:
                            after = ctext.split("Per", 1)[1]
                            after = after.split(":", 1)[1]
                            parts = after.split(None, 1)
                            period = parts[0]
                            if len(parts) > 1:
                                course = parts[1]
                except:
                    pass
                try:
                    a_teacher = tbl.locator("a[aria-label^='Teacher:']").first
                    if a_teacher.is_visible():
                        teacher = _norm(a_teacher.inner_text())
                except:
                    pass

                termc = "TP1"
                try:
                    hid = tbl.locator("input[id^='showmrktermc_']").first
                    if hid.is_visible():
                        termc = (hid.get_attribute("value") or "").strip() or "TP1"
                except:
                    pass

                pdf_url = urljoin(BASE, f"/Home/PrintProgressReport/{mstuniq}^{termc}")
                resp = ctx.request.get(pdf_url)
                if not resp.ok:
                    print(f"DEBUG — PDF GET failed {resp.status} for {pdf_url}")
                    continue

                rows = parse_progress_pdf(
                    resp.body(),
                    default_student=s,
                    course_hint=course,
                    period_hint=period,
                    teacher_hint=teacher,
                )
                print(f"DEBUG — parsed rows for {s}, class {mstuniq}: {len(rows)}")
                all_rows.extend(rows)

        browser.close()

    return all_rows
