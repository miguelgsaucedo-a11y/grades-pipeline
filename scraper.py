import re
import time
from typing import Dict, List, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

LOGIN_URL = "https://parentportal.cajonvalley.net/"
PORTAL_HOME = "https://parentportal.cajonvalley.net/Home/PortalMainPage"

# ---------- Utility: resilient navigation ----------
def safe_goto(page, url: str, wait: str = "domcontentloaded", timeout: int = 15000):
    try:
        page.goto(url, wait_until=wait, timeout=timeout)
    except PlaywrightError:
        # Fall back to 'commit' to handle portals that interrupt domcontentloaded with redirects
        print(f"DEBUG — goto({url}) {wait} aborted: retrying with 'commit'")
        page.goto(url, wait_until="commit", timeout=timeout)

# ---------- Login flow ----------
def ensure_logged_in(page, username: str, password: str) -> None:
    safe_goto(page, LOGIN_URL, "domcontentloaded")
    print(f"DEBUG — landed: {page.url}")

    # If already on PortalMainPage (session/cookies), bail early
    if page.url.startswith(PORTAL_HOME):
        return

    # Handle “session timed out” dialogs, if any
    timeout_dialog = page.locator("text=timed out, Click OK to Continue").first
    if timeout_dialog.is_visible():
        timeout_dialog.click()

    # Detect login fields in a tolerant way
    pin = page.locator('input[name="PIN"], input#PIN, input[id*="PIN" i], input[placeholder*="PIN" i]').first
    pwd = page.locator(
        'input[type="password"], input[name="Password"], input#Password, input[id*="Password" i]'
    ).first
    login_btn = page.locator(
        'button:has-text("Login"), input[type="submit"][value*="Login" i], a:has-text("Login")'
    ).first

    fields_visible = pin.is_visible() and pwd.is_visible()
    print(f"DEBUG — login fields visible: {fields_visible}")

    if fields_visible:
        pin.fill(username)
        pwd.fill(password)
        if login_btn.is_visible():
            login_btn.click()
        else:
            pwd.press("Enter")

    # Land on main page
    safe_goto(page, PORTAL_HOME, "domcontentloaded")

# ---------- Student switching ----------
def switch_to_student_by_header_text(page, student: str) -> None:
    """
    The portal shows the active student's name in the header/nav; clicking
    the other student's name switches context.
    We wait for any text change in the header region.
    """
    # Try a broad search to find the clickable student name
    candidate = page.get_by_text(student, exact=True).first
    if candidate.is_visible():
        candidate.click()
        time.sleep(0.4)  # small settle
        print(f"DEBUG — switched via header text to student '{student}'")
    else:
        print(f"DEBUG — could not locate a picker for '{student}'; skipping switch")

# ---------- Table pairing helpers ----------
def _collect_tables_positioned(page) -> Tuple[List[dict], List[dict]]:
    """
    Returns (assignment_tables, meta_tables) with ids and top positions.
    Assignment tables have id like 'tblAssign_12345'.
    Meta tables are any tables whose text hints at 'Period', 'Course', or 'Teacher'.
    """
    # All tables
    ids = page.eval_on_selector_all(
        "table", "els => els.map(e => ({ id: e.id || '', text: e.innerText || '', top: e.getBoundingClientRect().top }))"
    )

    assign = []
    meta = []
    for item in ids:
        tid = (item.get("id") or "").strip()
        text = (item.get("text") or "").strip()
        top = float(item.get("top") or 0.0)
        if tid.startswith("tblAssign_"):
            assign.append({"id": tid, "top": top})
        else:
            # simple heuristic for the info table above each assignment list
            if re.search(r"\b(Period|Course|Teacher)\b", text, flags=re.I):
                meta.append({"id": tid, "top": top, "text": text})
    return assign, meta

def _pair_meta_for_assign(assign_tables: List[dict], meta_tables: List[dict]) -> Dict[str, dict]:
    """
    For each assignments table, pick the nearest meta table *above* it.
    """
    meta_sorted = sorted(meta_tables, key=lambda m: m["top"])
    mapping = {}
    for a in assign_tables:
        tops_below = [m for m in meta_sorted if m["top"] < a["top"]]
        if tops_below:
            mapping[a["id"]] = tops_below[-1]  # closest above
        else:
            mapping[a["id"]] = {"id": "", "top": -1, "text": ""}
    return mapping

def _parse_meta_text(text: str) -> dict:
    """
    Parse a small "detail table" or header text into fields.
    Looks for 'Period:', 'Course:', 'Teacher:' (order/format tolerant).
    """
    meta = {"Period": "", "Course": "", "Teacher": ""}

    # Try straight 'Key: Value' patterns
    for key in ("Period", "Course", "Teacher"):
        m = re.search(rf"{key}\s*[:\-–]\s*([^\n\r|]+)", text, flags=re.I)
        if m:
            meta[key] = m.group(1).strip()

    # If Course still empty, sometimes the first non-empty line is the course
    if not meta["Course"]:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for ln in lines:
            if not re.search(r"(Period|Teacher|Terms of Use)", ln, flags=re.I):
                meta["Course"] = ln
                break

    # Period might be embedded like "Period 2" without colon
    if not meta["Period"]:
        m = re.search(r"Period\s*([A-Za-z0-9\-]+)", text, flags=re.I)
        if m:
            meta["Period"] = m.group(1).strip()

    return meta

# ---------- Assignment extraction ----------
def _extract_rows_from_assign_table(page, table_id: str) -> List[dict]:
    """
    From a single assignments table (by id), read header cells to build a column map,
    then collect each row. Returns list of dicts with the *assignment-only* fields.
    """
    return page.evaluate(
        """
(id) => {
  const t = document.getElementById(id);
  if (!t) return [];
  // Header detection: use the first row containing THs or, if missing, first TR.
  let headerCells = [];
  const headThs = t.querySelectorAll("thead th");
  if (headThs && headThs.length) {
    headerCells = Array.from(headThs).map(x => (x.textContent || "").trim());
  } else {
    const first = t.querySelector("tr");
    if (first) headerCells = Array.from(first.children).map(x => (x.textContent || "").trim());
  }
  const norm = s => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const idx = {};
  headerCells.forEach((h, i) => idx[norm(h)] = i);

  const getVal = (tds, ...keys) => {
    for (const k of keys) {
      const i = idx[norm(k)];
      if (i !== undefined && tds[i] !== undefined) return (tds[i].innerText || "").trim();
    }
    return "";
  };

  const bodyRows = Array.from(t.querySelectorAll("tbody tr")).filter(r => r.querySelectorAll("td").length);
  return bodyRows.map(tr => {
    const tds = Array.from(tr.children);
    return {
      DueDate: getVal(tds, "Due Date", "Due"),
      AssignedDate: getVal(tds, "Assigned Date", "Assigned"),
      Assignment: getVal(tds, "Assignment", "Assignment Title", "Title"),
      PtsPossible: getVal(tds, "Points Possible", "Pts Possible", "Points"),
      Score: getVal(tds, "Score"),
      Pct: getVal(tds, "Pct", "Percent", "Percentage"),
      Status: getVal(tds, "Status"),
      Comments: getVal(tds, "Comments", "Comment"),
      _hasAny: true
    };
  });
}
""",
        table_id,
    )

# ---------- Orchestration for one student ----------
def extract_assignments_for_student(page, student: str) -> List[dict]:
    switch_to_student_by_header_text(page, student)

    # Give the page a moment to update after switching
    time.sleep(0.5)

    # Collect positions of all tables; identify assignment tables + their nearest meta
    assign_tables, meta_tables = _collect_tables_positioned(page)

    # Basic visibility snapshot (debug)
    print(f"DEBUG — check 1: tables present={len(assign_tables)}, visible=0")
    print(f"DEBUG — check 2: tables present={len(meta_tables)}, visible=0")

    # Pair
    pair_map = _pair_meta_for_assign(assign_tables, meta_tables)

    all_rows: List[dict] = []
    for a in assign_tables:
        table_id = a["id"]
        rows = _extract_rows_from_assign_table(page, table_id)

        if not rows:
            continue

        meta_txt = pair_map.get(table_id, {}).get("text", "")
        meta = _parse_meta_text(meta_txt)

        for r in rows:
            if not r.get("_hasAny"):
                continue
            # Normalize dates as-is (let Sheets treat them as strings; users can format)
            row = {
                "Student": student,
                "Period": meta.get("Period", ""),
                "Course": meta.get("Course", ""),
                "Teacher": meta.get("Teacher", ""),
                "DueDate": r.get("DueDate", ""),
                "AssignedDate": r.get("AssignedDate", ""),
                "Assignment": r.get("Assignment", ""),
                "PtsPossible": r.get("PtsPossible", ""),
                "Score": r.get("Score", ""),
                "Pct": r.get("Pct", ""),
                "Status": r.get("Status", ""),
                "Comments": r.get("Comments", ""),
                "SourceURL": f"{PORTAL_HOME}#{table_id}",
            }
            all_rows.append(row)

    print(f"DEBUG — class tables for {student}: {len(assign_tables)}")
    return all_rows

# ---------- Public entry ----------
def run_scrape(username: str, password: str, students: List[str]) -> List[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-features=IsolateOrigins,site-per-process"])
        context = browser.new_context()
        page = context.new_page()

        ensure_logged_in(page, username, password)

        # Minor portal settle
        safe_goto(page, PORTAL_HOME, "domcontentloaded")

        # Some portals flash a "Terms of Use" (or similar) page title in <h1>/<h2>
        try:
            page.wait_for_timeout(250)
        except PlaywrightTimeoutError:
            pass

        all_rows: List[dict] = []
        for s in students:
            rows = extract_assignments_for_student(page, s)
            all_rows.extend(rows)

        context.close()
        browser.close()

        return all_rows
