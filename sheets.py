# sheets.py
import gspread
from google.oauth2.service_account import Credentials

# Only Sheets scope needed when opening by key
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def append_rows(spreadsheet_id, worksheet_name, rows, creds_info):
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)  # <- open by ID, no Drive listing
    ws = sh.worksheet(worksheet_name)
    order = ["ImportedAt","Student","Period","Course","Teacher","DueDate","AssignedDate",
             "Assignment","PtsPossible","Score","Pct","Status","Comments","SourceURL"]
    payload = [[r.get(k,"") for k in order] for r in rows]
    if payload:
        ws.append_rows(payload, value_input_option="USER_ENTERED")
