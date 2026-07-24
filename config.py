"""Env vars aur constants."""

import os
import json
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()   # local .env padhega; GitHub pe file nahi hogi to skip

DB_CONFIG = {
    "host": os.environ["DB_HOST"],
    "port": os.environ.get("DB_PORT", "5432"),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
    "connect_timeout": 30,
    "options": "-c statement_timeout=120000",
}

# Tracker sheet — jahan output jaata hai
SHEET_ID = os.environ["SHEET_ID"]

# Source sheet — jahan se dropout data aata hai
SOURCE_SHEET_ID = "1TyP-c1YYXI5dEAdeLkTKMktz13v7xXdTlU97K6-MB9E"
SOURCE_TAB = "Active Renewals"

# Local pe sa.json file se, GitHub Actions pe secret se
_sa = os.environ.get("GCP_SA_JSON")
if _sa:
    GCP_SA_INFO = json.loads(_sa)
else:
    with open("sa.json", encoding="utf-8") as f:
        GCP_SA_INFO = json.load(f)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Script khud manage karta hai — mail gaya ya nahi
MAIL_COLS = ["Mail_Sent", "Mail_Sent_On"]

# Outreach team ye columns manually bharegi — script kabhi overwrite nahi karta
OUTREACH_COLS = ["Outreach_Status", "Outreach_Date", "Assigned_To", "Feedback_Notes"]

# Data columns ke baad ye sab lagte hain (order matters)
TRACKER_COLS = MAIL_COLS + OUTREACH_COLS

TAB_RENEWED = "Renewed"
TAB_DROPPED = "Dropped"
TAB_LOG = "_Run_Log"

KEY_COL = "event_key"


def default_run_date() -> str:
    """Kal ki date — YYYY-MM-DD."""
    return (date.today() - timedelta(days=1)).isoformat()