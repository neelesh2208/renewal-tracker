"""
Daily renewal / drop tracker.

  Renewed  -> Postgres se (sql_queries.RENEWED_QUERY)
  Dropped  -> Source Google Sheet se ("Active Renewals" tab)

Dono tracker sheet ke alag tabs mein append hote hain.
Sirf naye rows add hote hain — manual columns kabhi overwrite nahi hote.

Usage:
    python main.py                                  # kal (D-1) ka data
    python main.py --dry-run                        # test, sheet mein kuch nahi
    python main.py --start 2026-07-01               # 1 July se kal tak
    python main.py --start 2026-07-01 --end 2026-07-22
    python main.py --date 2026-07-15                # sirf ek din
"""

import os
import sys
import time
import logging
import argparse
from datetime import date, timedelta

import pandas as pd
import psycopg2
import gspread
from gspread.exceptions import WorksheetNotFound, APIError
from google.oauth2.service_account import Credentials

import config
from sql_queries import RENEWED_QUERY
import emailer

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
            title=tab_name, rows=5000, cols=len(headers)
        ))
        retry(lambda: ws.update(values=[headers], range_name="A1",
                                raw=True))
        retry(lambda: ws.freeze(rows=1))
        return ws, True


def _read_sheet_df(ws) -> pd.DataFrame:
    """
    Sheet ko DataFrame mein padho — get_all_records() ki jagah.

    get_all_records() trailing khaali header columns pe crash karta hai
    ("header row contains duplicates: ['']"). Ye version unhe ignore karta hai.
    """
    values = retry(lambda: ws.get_all_values())
    if not values:
        return pd.DataFrame()

    header = values[0]
    # Trailing khaali headers hata do
    while header and not str(header[-1]).strip():
        header = header[:-1]
    if not header:
        return pd.DataFrame()

    n = len(header)
    rows, idx = [], []
    for i, r in enumerate(values[1:]):
        r = list(r[:n]) + [""] * max(0, n - len(r))
        if any(str(c).strip() for c in r):   # poori khaali row skip
            rows.append(r)
            idx.append(i)                    # original position — row_map ke liye

    return pd.DataFrame(rows, columns=header, index=idx)


def clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """NaN/NaT hatao, sab string banao."""
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
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
            f"[{tab_name}] '{config.KEY_COL}' column output mein nahi hai. "
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
                f"Purana tab delete kar do, script naya bana lega. "
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
        value_input_option="RAW",
        insert_data_option="INSERT_ROWS",
    ))
    log.info("[%s] +%d rows", tab_name, len(payload))
    return len(payload)


def write_log(start_date: str, end_date: str, counts: dict, status: str, note: str = ""):
    """_Run_Log tab mein run ka record."""
    gc = get_client()
    sh = retry(lambda: gc.open_by_key(config.SHEET_ID))
    headers = ["Run_Timestamp", "Start_Date", "End_Date",
               "Renewed_Added", "Dropped_Added", "Status", "Note"]
    ws, _ = get_or_create_tab(sh, config.TAB_LOG, headers)
    retry(lambda: ws.append_rows(
        [[
            time.strftime("%Y-%m-%d %H:%M:%S"),
            start_date,
            end_date,
            str(counts.get("renewed", 0)),
            str(counts.get("dropped", 0)),
            status,
            note[:500],
        ]],
        value_input_option="RAW",
    ))


# ---------- Dropouts (source sheet se) ----------

DROPOUT_DATE_COL = "Payment Date / Dropout Date"
DROPOUT_STATUS_COL = "Renewal Status"
DROPOUT_ID_COL = "Patient Id"


def fetch_dropouts(start_date: str, end_date: str) -> pd.DataFrame:
    """
    Source sheet ("Active Renewals") se dropout rows nikaalo.

    Filter: Renewal Status = 'Dropout'
            AND Payment Date / Dropout Date range ke andar
    """
    gc = get_client()
    sh = retry(lambda: gc.open_by_key(config.SOURCE_SHEET_ID))

    try:
        ws = retry(lambda: sh.worksheet(config.SOURCE_TAB))
    except WorksheetNotFound:
        raise ValueError(
            f"Source sheet mein '{config.SOURCE_TAB}' tab nahi mila. "
            f"Available: {[w.title for w in sh.worksheets()]}"
        )

    df = _read_sheet_df(ws).reset_index(drop=True)
    log.info("Source sheet -> %d rows", len(df))

    if df.empty:
        return df

    for col in (DROPOUT_DATE_COL, DROPOUT_STATUS_COL, DROPOUT_ID_COL):
        if col not in df.columns:
            raise ValueError(
                f"Source sheet mein '{col}' column nahi hai. "
                f"Mile: {list(df.columns)}"
            )

    # Date parse — sheet mein format mixed hota hai (2026-07-05 aur 22-07-2026 dono)
    # Pehle ISO try karo, jo bache unhe dayfirst se
    raw = df[DROPOUT_DATE_COL].astype(str).str.strip()
    iso = pd.to_datetime(raw, errors="coerce", format="ISO8601")
    rest = pd.to_datetime(raw.where(iso.isna()), errors="coerce",
                          dayfirst=True, format="mixed")
    parsed = iso.fillna(rest)

    bad = parsed.isna() & raw.ne("")
    if bad.any():
        log.warning("%d rows ki date parse nahi hui — skip", int(bad.sum()))
    df["_dropout_date"] = parsed

    mask = (
        df[DROPOUT_STATUS_COL].astype(str).str.strip().str.casefold().eq("dropout")
        & df["_dropout_date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    )
    out = df[mask].copy()
    log.info("Dropouts filtered -> %d rows", len(out))

    if out.empty:
        return out

    # Unique key — Patient Id se, khaali ho to Mobile Number se fallback
    pid = out[DROPOUT_ID_COL].astype(str).str.strip()
    if "Mobile Number" in out.columns:
        mob = out["Mobile Number"].astype(str).str.strip()
    else:
        mob = pd.Series("", index=out.index)

    ident = pid.where(pid.ne("") & pid.str.lower().ne("nan"), "M" + mob)
    out["event_key"] = ident + "_" + out["_dropout_date"].dt.strftime("%Y-%m-%d")

    # Normalized date column (sheet mein saaf format)
    out["dropout_date"] = out["_dropout_date"].dt.strftime("%Y-%m-%d")
    out = out.drop(columns=["_dropout_date"])

    # event_key aur dropout_date pehle, baaki source columns waise hi
    front = ["event_key", "dropout_date"]
    rest = [c for c in out.columns if c not in front]
    out = out[front + rest]

    # Jinke paas na Patient Id hai na Mobile — unhe skip (key nahi ban sakti)
    keep = ident.str.strip().ne("") & ident.str.strip().ne("M")
    dropped_n = int((~keep).sum())
    if dropped_n:
        log.warning("%d rows mein na Patient Id na Mobile — skip", dropped_n)
    out = out[keep.values]

    return out.reset_index(drop=True)


# ---------- Mail pending / marking ----------

def read_pending_for_mail(tab_name: str) -> tuple:
    """
    Sheet se wo rows padho jinka Mail_Sent khaali hai.

    Returns: (DataFrame, {event_key -> sheet row number})
    Purani date ke rows bhi aate hain agar mail nahi gaya — jaan-boojh kar,
    taaki koi miss na ho.
    """
    gc = get_client()
    sh = retry(lambda: gc.open_by_key(config.SHEET_ID))

    try:
        ws = sh.worksheet(tab_name)
    except WorksheetNotFound:
        log.info("[%s] tab nahi hai — mail ke liye kuch nahi", tab_name)
        return pd.DataFrame(), {}

    df = _read_sheet_df(ws)
    if df.empty:
        return pd.DataFrame(), {}

    if "Mail_Sent" not in df.columns:
        log.warning("[%s] 'Mail_Sent' column nahi hai — purana tab delete karo", tab_name)
        return pd.DataFrame(), {}

    sent = df["Mail_Sent"].astype(str).str.strip().str.casefold()
    pending_mask = ~sent.isin(["yes", "y", "true", "1"])

    pending = df[pending_mask].copy()
    if pending.empty:
        log.info("[%s] mail ke liye koi pending row nahi", tab_name)
        return pending, {}

    # Sheet row number: header row 1 hai, data row 2 se
    row_map = {}
    for idx in pending.index:
        key = str(pending.at[idx, config.KEY_COL]).strip()
        if key:
            row_map[key] = int(idx) + 2

    log.info("[%s] mail pending: %d rows", tab_name, len(pending))
    return pending, row_map


def mark_mail_sent(tab_name: str, keys: list, row_map: dict) -> int:
    """Jin rows ka mail gaya, unme Mail_Sent = Yes aur date likh do."""
    if not keys:
        return 0

    gc = get_client()
    sh = retry(lambda: gc.open_by_key(config.SHEET_ID))
    ws = retry(lambda: sh.worksheet(tab_name))

    headers = retry(lambda: ws.row_values(1))
    if "Mail_Sent" not in headers or "Mail_Sent_On" not in headers:
        log.warning("[%s] Mail_Sent columns nahi mile — mark skip", tab_name)
        return 0

    col_sent = headers.index("Mail_Sent") + 1
    col_on = headers.index("Mail_Sent_On") + 1
    today = time.strftime("%Y-%m-%d")

    updates = []
    for k in keys:
        r = row_map.get(str(k).strip())
        if not r:
            continue
        updates.append({"range": gspread.utils.rowcol_to_a1(r, col_sent), "values": [["Yes"]]})
        updates.append({"range": gspread.utils.rowcol_to_a1(r, col_on), "values": [[today]]})

    if not updates:
        return 0

    retry(lambda: ws.batch_update(updates, value_input_option="RAW"))
    n = len(updates) // 2
    log.info("[%s] %d rows Mail_Sent = Yes", tab_name, n)
    return n


# ---------- Main ----------

def resolve_dates(args) -> tuple:
    """
    Date range nikaalo. Priority:
      --date          -> ek hi din
      --start/--end   -> range (missing side default = kal)
      kuch nahi       -> kal se kal (D-1)
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    if args.date:
        d = args.date.strip()
        return d, d

    start = (args.start or "").strip() or os.environ.get("START_DATE", "").strip()
    end   = (args.end or "").strip()   or os.environ.get("END_DATE", "").strip()

    if not start and not end:
        return yesterday, yesterday

    return start or yesterday, end or yesterday


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default="", help="Sirf ek din. YYYY-MM-DD")
    p.add_argument("--start", default="", help="Range start. YYYY-MM-DD")
    p.add_argument("--end", default="", help="Range end. Default: kal")
    p.add_argument("--dry-run", action="store_true", help="Sheet mein mat likho")
    p.add_argument("--no-mail", action="store_true", help="Email mat bhejo")
    p.add_argument("--mail-empty", action="store_true",
                   help="Khaali report bhi bhejo (default: skip)")
    args = p.parse_args()

    start_date, end_date = resolve_dates(args)
    params = {"start_date": start_date, "end_date": end_date}

    log.info("=" * 55)
    log.info("Range: %s  ->  %s | dry_run=%s", start_date, end_date, args.dry_run)
    log.info("=" * 55)

    counts, failed = {}, []
    df_ren = pd.DataFrame()
    df_drop = pd.DataFrame()

    # ---- Renewed (Postgres) ----
    try:
        df_ren = run_query(RENEWED_QUERY, params)
        if args.dry_run:
            log.info("[DRY RUN] Renewed -> %d rows", len(df_ren))
            if not df_ren.empty:
                log.info("Columns: %s", list(df_ren.columns))
                print(df_ren.head(10).to_string())
            counts["renewed"] = 0
        else:
            counts["renewed"] = append_new_rows(config.TAB_RENEWED, df_ren)
    except Exception as e:
        log.exception("[Renewed] fail: %s", e)
        failed.append(f"Renewed: {e}")
        counts["renewed"] = 0

    # ---- Dropped (source sheet) ----
    try:
        df_drop = fetch_dropouts(start_date, end_date)
        if args.dry_run:
            log.info("[DRY RUN] Dropped -> %d rows", len(df_drop))
            if not df_drop.empty:
                log.info("Columns: %s", list(df_drop.columns))
                print(df_drop.head(10).to_string())
            counts["dropped"] = 0
        else:
            counts["dropped"] = append_new_rows(config.TAB_DROPPED, df_drop)
    except Exception as e:
        log.exception("[Dropped] fail: %s", e)
        failed.append(f"Dropped: {e}")
        counts["dropped"] = 0

    status = "FAILED" if failed else "OK"

    # ---- Email reports (sheet ke pending rows se, query se nahi) ----
    if args.no_mail:
        log.info("Email skip (--no-mail)")
    elif args.dry_run:
        log.info("Email skip (dry-run)")
    else:
        try:
            pend_ren, map_ren = read_pending_for_mail(config.TAB_RENEWED)
            pend_drop, map_drop = read_pending_for_mail(config.TAB_DROPPED)

            label = start_date if start_date == end_date else f"{start_date} to {end_date}"
            result = emailer.send_all_reports(
                pend_ren, pend_drop, label, send_empty=args.mail_empty
            )

            # Jinka mail gaya unhe mark karo
            if result.get("new_plan_keys"):
                mark_mail_sent(config.TAB_RENEWED, result["new_plan_keys"], map_ren)
            if result.get("renewal_keys"):
                mark_mail_sent(config.TAB_RENEWED, result["renewal_keys"], map_ren)
            if result.get("dropout_keys"):
                mark_mail_sent(config.TAB_DROPPED, result["dropout_keys"], map_drop)

            log.info("Emails bheje: %d", result.get("sent", 0))
        except Exception as e:
            log.exception("Email fail: %s", e)
            failed.append(f"email: {e}")
            status = "FAILED"

    if not args.dry_run:
        try:
            write_log(start_date, end_date, counts, status, " | ".join(failed))
        except Exception:
            log.exception("Run log likhne mein dikkat (non-fatal)")

    log.info("-" * 55)
    log.info("Renewed: +%d | Dropped: +%d | %s",
             counts.get("renewed", 0), counts.get("dropped", 0), status)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())