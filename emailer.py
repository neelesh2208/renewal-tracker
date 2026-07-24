"""
Email reports — 3 alag mails:
    Report 1: New client registrations yesterday
    Report 2: Renewals yesterday
    Report 3: Dropouts yesterday

Har mail mein us category ke saare clients, har ek ka detail block +
ready-to-copy message.

Gmail SMTP use karta hai. .env / GitHub secrets mein chahiye:
    SMTP_USER      -> bhejne wala Gmail address
    SMTP_PASSWORD  -> Gmail App Password (16 char, normal password nahi)
    MAIL_TO        -> comma-separated recipients
"""

import os
import html
import smtplib
import logging
from email.message import EmailMessage

import pandas as pd

log = logging.getLogger("tracker.email")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


# ---------- Message templates ----------

NEW_PLAN_MSG = """Hi {name}

My Name is Tanmay, I am one of the founders of Emoneeds.
Thank you for trusting us with your time. We are very happy to be part of your care journey.
Please feel free to message or call me anytime - in case you face any issues during your care plan.
Have a great rest of your week!

Best,
Emoneeds Team

P.S - If you are free for a quick call this week, please let me know - I would love to know feedback on your experience."""


RENEWAL_MSG = """Hi {name}

My Name is Tanmay, I am one of the founders of Emoneeds.
Thank you for renewing your plan with us. We are very happy to remain part of your care journey.
Please feel free to message or call me anytime - in case you face any issues during your care plan.
Have a great rest of your week!

Best,
Emoneeds Team

P.S - If you are free for a quick call this week, please let me know - I would love to know feedback on your experience."""


DROPOUT_MSG = """Hi {name}

My Name is Tanmay, I am one of the founders of Emoneeds.
I am sorry to hear that you will not be continuing with Emoneeds. I hope that our services were useful.
If you are free for a quick call this week, please let me know - I would love to know feedback on your experience. And how we can improve in the future.
Have a great rest of your week!

Best,
Emoneeds Team"""


# ---------- Helpers ----------

def _s(val) -> str:
    """Safe string — NaN/None ko '-' banao."""
    if val is None:
        return "-"
    if isinstance(val, float) and pd.isna(val):
        return "-"
    text = str(val).strip()
    if text.lower() in ("", "nan", "none", "nat", "<na>"):
        return "-"
    return text


def _ordinal(n) -> str:
    """1 -> 1st, 2 -> 2nd, 3 -> 3rd, 11 -> 11th"""
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        return "-"
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _team(psy, psych, couns) -> str:
    """Teeno roles ek line mein — jo assigned hain wahi."""
    parts = []
    if _s(psy) != "-":
        parts.append(f"Psychologist: {_s(psy)}")
    if _s(psych) != "-":
        parts.append(f"Psychiatrist: {_s(psych)}")
    if _s(couns) != "-":
        parts.append(f"Counsellor: {_s(couns)}")
    return " | ".join(parts) if parts else "-"


def _pick(row, *names):
    """Pehla column jo row mein mile — sheet aur DB ke naam alag hain."""
    for n in names:
        if n in row.index:
            v = row[n]
            if _s(v) != "-":
                return v
    return None


# ---------- HTML building ----------

_CSS = """
body { font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
       font-size: 14px; color: #1a1a1a; line-height: 1.5; }
.client { border: 1px solid #d9d9d9; border-radius: 6px;
          padding: 14px 16px; margin-bottom: 18px; }
.client h3 { margin: 0 0 10px 0; font-size: 15px; }
.fields { margin-bottom: 12px; }
.fields div { padding: 1px 0; }
.label { color: #666; display: inline-block; min-width: 120px; }
.msg { background: #f6f6f6; border-left: 3px solid #bbb;
       padding: 10px 12px; white-space: pre-wrap;
       font-family: Consolas, Menlo, monospace; font-size: 13px; }
.count { color: #666; margin-bottom: 16px; }
.empty { color: #888; font-style: italic; }
"""


def _client_block(title: str, fields: list, message: str) -> str:
    rows = "".join(
        f'<div><span class="label">{html.escape(k)}:</span> {html.escape(_s(v))}</div>'
        for k, v in fields
    )
    return (
        '<div class="client">'
        f'<h3>{html.escape(title)}</h3>'
        f'<div class="fields">{rows}</div>'
        f'<div class="msg">{html.escape(message)}</div>'
        '</div>'
    )


def _wrap(heading: str, run_date: str, body: str, n: int) -> str:
    return (
        f"<html><head><meta charset='utf-8'><style>{_CSS}</style></head><body>"
        f"<h2>{html.escape(heading)}</h2>"
        f"<div class='count'>{run_date} &nbsp;|&nbsp; {n} client(s)</div>"
        f"{body}"
        "</body></html>"
    )


# ---------- Report builders ----------

def build_new_plan_report(df: pd.DataFrame, run_date: str) -> tuple:
    """Renewed df se sirf NEW PLAN wale."""
    if df.empty or "plan_status" not in df.columns:
        sub = df.iloc[0:0] if not df.empty else pd.DataFrame(columns=["event_key"])
    else:
        sub = df[df["plan_status"].astype(str).str.strip().str.upper() == "NEW PLAN"]

    blocks, keys = [], []
    for _, r in sub.iterrows():
        name = _s(r.get("patient_name"))
        keys.append(_s(r.get("event_key")))
        blocks.append(_client_block(
            name,
            [
                ("Name", name),
                ("Age", r.get("age")),
                ("Gender", r.get("gender_name")),
                ("Phone Number", r.get("mobile_number")),
                ("Team", _team(r.get("psychologist_name"),
                               r.get("psychiatrist_name"),
                               r.get("counsellor_name"))),
                ("Plan Type", r.get("package_name")),
            ],
            NEW_PLAN_MSG.format(name=name),
        ))

    body = "".join(blocks) or "<p class='empty'>Koi pending new registration nahi.</p>"
    return (
        f"Report 1 - New Client Registrations ({run_date}) - {len(sub)}",
        _wrap("New Client Registrations", run_date, body, len(sub)),
        keys,
    )


def build_renewal_report(df: pd.DataFrame, run_date: str) -> tuple:
    """Renewed df se NEW PLAN ke alawa sab (RENEWAL / LATE RENEWAL / REVIVAL)."""
    if df.empty or "plan_status" not in df.columns:
        sub = df.iloc[0:0] if not df.empty else pd.DataFrame(columns=["event_key"])
    else:
        sub = df[df["plan_status"].astype(str).str.strip().str.upper() != "NEW PLAN"]

    blocks, keys = [], []
    for _, r in sub.iterrows():
        name = _s(r.get("patient_name"))
        keys.append(_s(r.get("event_key")))
        months = _ordinal(r.get("months_with_us"))
        blocks.append(_client_block(
            f"{name}  ({_s(r.get('plan_status'))})",
            [
                ("Name", name),
                ("Age", r.get("age")),
                ("Gender", r.get("gender_name")),
                ("Phone Number", r.get("mobile_number")),
                ("Months with us", f"This is their {months} month" if months != "-" else "-"),
                ("Team", _team(r.get("psychologist_name"),
                               r.get("psychiatrist_name"),
                               r.get("counsellor_name"))),
                ("Plan Type", r.get("package_name")),
            ],
            RENEWAL_MSG.format(name=name),
        ))

    body = "".join(blocks) or "<p class='empty'>Koi pending renewal nahi.</p>"
    return (
        f"Report 2 - Renewals ({run_date}) - {len(sub)}",
        _wrap("Renewals", run_date, body, len(sub)),
        keys,
    )


def build_dropout_report(df: pd.DataFrame, run_date: str) -> tuple:
    """Source sheet se aaye dropouts."""
    blocks, keys = [], []
    for _, r in df.iterrows():
        name = _s(_pick(r, "patient_name", "Patient Name"))
        keys.append(_s(r.get("event_key")))
        months = _ordinal(_pick(r, "Months With Us", "months_with_us"))
        blocks.append(_client_block(
            name,
            [
                ("Name", name),
                ("Phone Number", _pick(r, "Mobile Number", "mobile_number")),
                ("Months with us", f"This is their {months} month" if months != "-" else "-"),
                ("Team", _team(_pick(r, "Psychologist Name", "psychologist_name"),
                               _pick(r, "Psychatrist Name", "Psychiatrist Name", "psychiatrist_name"),
                               _pick(r, "Counsellor Name", "counsellor_name"))),
                ("Plan Type", _pick(r, "Price Plan", "package_name")),
                ("Unit", _pick(r, "Unit")),
                ("Lead Source", _pick(r, "lead Source", "Lead Source", "lead_source")),
            ],
            DROPOUT_MSG.format(name=name),
        ))

    body = "".join(blocks) or "<p class='empty'>Koi pending dropout nahi.</p>"
    return (
        f"Report 3 - Dropouts ({run_date}) - {len(df)}",
        _wrap("Dropouts", run_date, body, len(df)),
        keys,
    )


# ---------- Sending ----------

def send_mail(subject: str, html_body: str) -> bool:
    """Ek email bhejo. True = gaya, False = fail."""
    user = os.environ.get("SMTP_USER", "").strip()
    pwd = os.environ.get("SMTP_PASSWORD", "").strip()
    to = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]

    if not (user and pwd and to):
        log.warning("SMTP_USER / SMTP_PASSWORD / MAIL_TO set nahi — mail skip")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.set_content("Ye email HTML mein hai. HTML-capable client mein kholo.")
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        log.info("Mail bheja: %s", subject)
        return True
    except Exception as e:
        log.error("Mail fail (%s): %s", subject, e)
        return False


def send_all_reports(df_renewed: pd.DataFrame, df_dropped: pd.DataFrame,
                     run_date: str, send_empty: bool = False) -> dict:
    """
    Teeno reports bhejo. send_empty=False hai to khaali report skip.

    Returns dict:
        sent            -> kitne mail gaye
        new_plan_keys   -> Report 1 mein jo event_keys the (mail gaya to)
        renewal_keys    -> Report 2 ke keys
        dropout_keys    -> Report 3 ke keys
    """
    subj1, body1, keys1 = build_new_plan_report(df_renewed, run_date)
    subj2, body2, keys2 = build_renewal_report(df_renewed, run_date)
    subj3, body3, keys3 = build_dropout_report(df_dropped, run_date)

    out = {"sent": 0, "new_plan_keys": [], "renewal_keys": [], "dropout_keys": []}

    for subject, body, keys, slot in [
        (subj1, body1, keys1, "new_plan_keys"),
        (subj2, body2, keys2, "renewal_keys"),
        (subj3, body3, keys3, "dropout_keys"),
    ]:
        if not keys:
            if send_empty:
                if send_mail(subject, body):
                    out["sent"] += 1
            else:
                log.info("Skip (khaali): %s", subject)
            continue

        if send_mail(subject, body):
            out["sent"] += 1
            out[slot] = keys
        else:
            log.warning("Mail nahi gaya, rows pending rahenge: %s", subject)

    return out