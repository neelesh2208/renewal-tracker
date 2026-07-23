"""
Daily renewal / drop tracker.

Postgres se data nikaal kar Google Sheet ke tabs mein append karta hai.
Sirf naye rows add hote hain — manual columns kabhi overwrite nahi hote.
"""

import sys
import time
import logging
import argparse

import pandas as pd
import psycopg2
import gspread
from gspread.exceptions import WorksheetNotFound, APIError
from google.oauth2.service_account import Credentials

import config
from sql_queries import RENEWED_QUERY, DROPPED_QUERY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tracker")


# ---------- Database ----------

def run_query(sql: str, params: dict) -> pd.DataFrame:
    """Query chalao, DataFrame return karo."""
    with psycopg2.connect(**config.DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    log.info("Query -> %d rows, %d cols", len(df), len(df.columns))
    return df


# ---------- Google Sheets ----------

def get_client():
    creds = Credentials.from_service_account_info(config.GCP_SA_INFO, scopes=config.SCOPES)
    return gspread.authorize(creds)


def retry(fn, attempts: int = 4, base_delay: float = 2.0):
    """Google API rate limit ke liye exponential backoff."""
    for i in range(attempts):
        try:
            return fn()
        except APIError as e:
            if i == attempts - 1:
                raise
            wait = base_delay * (2 ** i)
            log.warning("API error (%d/%d): %s — %.0fs baad retry", i + 1, attempts, e, wait)
            time.sleep(wait)


def get_or_create_tab(sh, tab_name: str, headers: list):
    """Tab dhoondho, nahi mila to header ke saath banao."""
    try:
        return sh.worksheet(tab_name), False
    except WorksheetNotFound:
        log.info("Tab '%s' bana rahe hain", tab_name)
        ws = retry(lambda: sh.add_worksheet(
            title=tab_name, rows=5000, cols=max(len(headers) + 5, 26)
        ))
        retry(lambda: ws.update(values=[headers], range_name="A1"))
        retry(lambda: ws.freeze(rows=1))
        return ws, True


def clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """NaN/NaT hatao, sab string banao."""
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d %H:%M:%S")
    return out.fillna("").astype(str).replace(
        {"NaT": "", "None": "", "nan": "", "<NA>": ""}
    )


def append_new_rows(tab_name: str, df: pd.DataFrame) -> int:
    """Naye rows append karo, purane skip. Manual columns khaali chhodo."""
    if df.empty:
        log.info("[%s] 0 rows — kuch nahi karna", tab_name)
        return 0

    if config.KEY_COL not in df.columns:
        raise ValueError(
            f"[{tab_name}] '{config.KEY_COL}' column query output mein nahi hai. "
            f"Mile: {list(df.columns)}"
        )

    gc = get_client()
    sh = retry(lambda: gc.open_by_key(config.SHEET_ID))

    data_cols = list(df.columns)
    headers = data_cols + config.TRACKER_COLS
    ws, created = get_or_create_tab(sh, tab_name, headers)

    # Existing keys nikaalo — sirf key column padhte hain (fast)
    existing = set()
    if not created:
        sheet_headers = retry(lambda: ws.row_values(1))
        if config.KEY_COL not in sheet_headers:
            raise ValueError(
                f"[{tab_name}] sheet header mein '{config.KEY_COL}' nahi hai. "
                f"Headers: {sheet_headers}"
            )
        idx = sheet_headers.index(config.KEY_COL) + 1
        vals = retry(lambda: ws.col_values(idx))
        existing = {v.strip() for v in vals[1:] if v.strip()}
        log.info("[%s] sheet mein pehle se %d keys", tab_name, len(existing))

    clean = clean_df(df)
    new = clean[~clean[config.KEY_COL].str.strip().isin(existing)]

    if new.empty:
        log.info("[%s] koi naya row nahi", tab_name)
        return 0

    payload = [
        list(r) + [""] * len(config.TRACKER_COLS)
        for r in new[data_cols].itertuples(index=False, name=None)
    ]
    retry(lambda: ws.append_rows(
        payload,
        value_input_option="USER_ENTERED",
        insert_data_option="INSERT_ROWS",
        table_range="A1",
    ))
    log.info("[%s] +%d rows", tab_name, len(payload))
    return len(payload)


def write_log(run_date: str, counts: dict, status: str, note: str = ""):
    """_Run_Log tab mein run ka record."""
    gc = get_client()
    sh = retry(lambda: gc.open_by_key(config.SHEET_ID))
    headers = ["Run_Timestamp", "Run_Date", "Renewed_Added", "Dropped_Added", "Status", "Note"]
    ws, _ = get_or_create_tab(sh, config.TAB_LOG, headers)
    retry(lambda: ws.append_rows(
        [[
            time.strftime("%Y-%m-%d %H:%M:%S"),
            run_date,
            str(counts.get("renewed", 0)),
            str(counts.get("dropped", 0)),
            status,
            note[:500],
        ]],
        value_input_option="USER_ENTERED",
    ))


# ---------- Main ----------

JOBS = [
    (RENEWED_QUERY, config.TAB_RENEWED, "renewed"),
    (DROPPED_QUERY, config.TAB_DROPPED, "dropped"),
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-date", default="", help="YYYY-MM-DD. Default: kal.")
    p.add_argument("--dry-run", action="store_true", help="Sheet mein mat likho.")
    args = p.parse_args()

    import os
    run_date = args.run_date.strip() or os.environ.get("RUN_DATE", "").strip() or config.default_run_date()
    params = {"run_date": run_date}

    log.info("=" * 55)
    log.info("Run date: %s | dry_run=%s", run_date, args.dry_run)
    log.info("=" * 55)

    counts, failed = {}, []

    for sql, tab, label in JOBS:
        try:
            df = run_query(sql, params)
            if args.dry_run:
                log.info("[DRY RUN] %s -> %d rows", tab, len(df))
                if not df.empty:
                    log.info("Columns: %s", list(df.columns))
                    print(df.head(5).to_string())
                counts[label] = 0
            else:
                counts[label] = append_new_rows(tab, df)
        except Exception as e:
            log.exception("[%s] fail: %s", tab, e)
            failed.append(f"{tab}: {e}")
            counts[label] = 0

    status = "FAILED" if failed else "OK"

    if not args.dry_run:
        try:
            write_log(run_date, counts, status, " | ".join(failed))
        except Exception:
            log.exception("Run log likhne mein dikkat (non-fatal)")

    log.info("-" * 55)
    log.info("Renewed: +%d | Dropped: +%d | %s",
             counts.get("renewed", 0), counts.get("dropped", 0), status)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())