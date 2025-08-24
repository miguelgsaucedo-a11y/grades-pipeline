# sheets.py
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def append_rows(spreadsheet_name, worksheet_name, rows, creds_info):
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open(spreadsheet_name)
    ws = sh.worksheet(worksheet_name)
    order = ["ImportedAt","Student","Period","Course","Teacher","DueDate","AssignedDate",
             "Assignment","PtsPossible","Score","Pct","Status","Comments","SourceURL"]
    payload = [[r.get(k,"") for k in order] for r in rows]
    if payload:
        ws.append_rows(payload, value_input_option="USER_ENTERED")
