"""
AI Timesheet Generator  (v2.1.0)
================================

A polished, single-file Streamlit application that:

  1. Extracts your Git history WITH COMMIT TIMESTAMPS via `subprocess` (git log).
  2. Cleans up each commit with a FAST Anthropic model (Model 1) -> a readable
     description + status. HOURS are computed deterministically from the time
     gaps between commits (not guessed by the AI).
  3. Lets you review/edit, apply business rules, and export to a Google Sheet
     (3 worksheets) that acts as the database / API, using `gspread`.
  4. Lets you chat with your data using a SMART Anthropic model (Model 2). The
     data is sorted newest-first with explicit ordering guidance so questions
     like "the latest commit" are answered correctly.

The "Two-Model AI Approach":
  - Model 1 (fast, cheap) -> high-volume cleanup + summarization.
  - Model 2 (smart)       -> conversational analysis over the stored data.

Entries are keyed by AUTHOR (no ticket IDs). All text/comments are in English.
"""

from __future__ import annotations

import os
import re
import json
import subprocess
from collections import defaultdict
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import anthropic
import gspread
from gspread.exceptions import SpreadsheetNotFound

# --------------------------------------------------------------------------- #
# A. Initialization & Configuration
# --------------------------------------------------------------------------- #

# Two different models for two different jobs (see README -> Two-Model Approach).
# NOTE: The originally requested `claude-3-5-sonnet-20241022` was retired by
# Anthropic, so we use the current, actively-supported model IDs here.
FAST_MODEL = "claude-haiku-4-5"   # Model 1 -> fast cleanup & summarization
SMART_MODEL = "claude-sonnet-5"   # Model 2 -> deep, conversational chat analysis

CREDENTIALS_FILE = "credentials.json"   # Google service-account key (git-ignored)
MAX_COMMITS = 200                       # Safety cap to keep prompts affordable
APP_VERSION = "v2.1.0"

# Datetime format used to read commits and to store dates in the Sheet.
DT_FORMAT = "%Y-%m-%d %H:%M:%S"

# Hours-from-timestamps heuristic (see estimate_hours_df).
FIRST_COMMIT_HOURS = 1.0   # assumed "warm-up" work before the first commit of a day
MAX_GAP_HOURS = 4.0        # a gap larger than this is treated as a break, capped here
MIN_COMMIT_HOURS = 0.25    # floor so trivial commits still count a little

# Status vocabulary used across the UI and the Status dropdown in the table.
STATUS_OPTIONS = ["Completed", "In Progress", "Review"]

# The 3 internal worksheets (tabs) and their exact column headers.
WORKSHEETS = {
    "Raw_Entries": ["Date", "Author", "Commit_Summary", "Hours", "Status"],
    "Dashboard_Summary": ["Week", "Total_Hours", "Top_Author", "Standup_Summary", "Blockers"],
    "Rates_Config": ["Author", "Client", "Hourly_Rate"],
}

# Example rate-card rows seeded into Rates_Config the first time it is created,
# so billing questions in the chat have something to work with (edit to match
# your real git authors).
EXAMPLE_RATES = [
    ["Alice Example", "Acme Corp", 85],
    ["Bob Example", "Globex", 95],
]

# Green header used when formatting the Google Sheet (RGB 0-1 for the Sheets API).
SHEET_HEADER_COLOR = {"red": 0.176, "green": 0.49, "blue": 0.357}


# --------------------------------------------------------------------------- #
# Cached clients (created once per session)
# --------------------------------------------------------------------------- #

@st.cache_resource(show_spinner=False)
def get_anthropic_client():
    """Return an Anthropic client, or None if no API key is configured."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    return anthropic.Anthropic()


@st.cache_resource(show_spinner=False)
def _gspread_client():
    """Authenticate to Google Sheets via a service-account file."""
    return gspread.service_account(filename=CREDENTIALS_FILE)


def open_spreadsheet():
    """Open the configured spreadsheet. Raises on connection/config errors."""
    name = os.getenv("GOOGLE_SHEET_NAME", "").strip()
    if not name:
        raise RuntimeError("GOOGLE_SHEET_NAME is not set.")
    return _gspread_client().open(name)


def sheets_configured() -> bool:
    """True when both the credentials file and the sheet name are present."""
    return os.path.exists(CREDENTIALS_FILE) and bool(os.getenv("GOOGLE_SHEET_NAME"))


@st.cache_data(ttl=300, show_spinner=False)
def check_connection():
    """
    Cheaply check the Google Sheets connection (cached ~5 min so the sidebar
    doesn't call the API on every rerun). Returns (ok, url, error_kind).
    """
    try:
        spreadsheet = open_spreadsheet()
        return True, spreadsheet.url, ""
    except SpreadsheetNotFound:
        return False, "", "not_found"
    except Exception as exc:  # noqa: BLE001
        return False, "", str(exc)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _text_of(response) -> str:
    """Concatenate all text blocks from an Anthropic message response."""
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def _to_float(value, default: float = 1.0) -> float:
    """Best-effort float conversion for AI-provided numbers."""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


def extract_json(text: str):
    """Robustly extract JSON (pure, fenced, or embedded) from an LLM response."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


# --------------------------------------------------------------------------- #
# B. Git helpers (with datetime + author discovery)
# --------------------------------------------------------------------------- #

def _run_git(args, timeout=30):
    """Run a git command and return CompletedProcess. Raises RuntimeError nicely."""
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("Git is not installed or not on your PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("A git command timed out.")


@st.cache_data(ttl=60, show_spinner=False)
def get_repo_info(repo_path: str):
    """Resolve the repo root. Returns (is_repo, name, absolute_path)."""
    try:
        result = _run_git(["-C", repo_path, "rev-parse", "--show-toplevel"], timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            top = result.stdout.strip()
            return True, os.path.basename(top.rstrip("/\\")) or top, top
    except Exception:  # noqa: BLE001
        pass
    return False, "", ""


@st.cache_data(ttl=60, show_spinner=False)
def get_git_authors(repo_path: str, limit: int = 2000):
    """Return the distinct commit authors in a repo (most-recent-first order)."""
    try:
        result = _run_git(["-C", repo_path, "log", "-n", str(limit), "--format=%an"], timeout=15)
        if result.returncode != 0:
            return []
    except Exception:  # noqa: BLE001
        return []
    seen, ordered = set(), []
    for name in result.stdout.splitlines():
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def get_git_data(days_back: int, repo_path: str = ".", author: str | None = None):
    """
    Run `git log` and return commit dicts WITH full timestamps.

    Each dict: {"hash", "id", "author", "email", "datetime", "message"}.
    """
    sep = "\x1f"  # unit separator — safe delimiter that won't appear in messages
    fmt = sep.join(["%H", "%an", "%ae", "%ad", "%s"])
    cmd = [
        "-C", repo_path, "log",
        f"--since={days_back} days ago",
        f"--date=format:{DT_FORMAT}",
        f"--pretty=format:{fmt}",
    ]
    if author:
        cmd.append(f"--author={author}")

    result = _run_git(cmd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Unknown git error.")

    commits = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(sep)
        if len(parts) != 5:
            continue
        commit_hash, name, email, when, message = parts
        commits.append({
            "hash": commit_hash,
            "id": commit_hash[:8],
            "author": name,
            "email": email,
            "datetime": when,
            "message": message,
        })
    return commits


# --------------------------------------------------------------------------- #
# C. AI: cleanup (Model 1) + narrative (Model 1)
# --------------------------------------------------------------------------- #

def parse_with_ai_model_1(client, commits: list) -> dict:
    """
    Model 1 (fast): clean up each commit into a description + status.

    Returns a mapping {commit_id: {"description", "status"}}. Hours are NOT
    produced here — they are computed from timestamps (see estimate_hours_df).
    """
    listing = "\n".join(
        f'{c["id"]} | {c["datetime"]} | {c["author"]} | {c["message"]}'
        for c in commits
    )
    system = (
        "You clean up raw git commits into concise timesheet descriptions. "
        "Respond with STRICT JSON only — no prose, no markdown code fences."
    )
    user = f"""For each commit line below (format: ID | datetime | author | message),
produce a JSON object with these keys:
- "id": the exact ID copied from the line
- "description": a short, clear, human-readable description of the work done
- "status": one of "Completed", "In Progress", or "Review", inferred from the message

Commits:
{listing}

Return ONLY a JSON array with one object per commit."""

    response = client.messages.create(
        model=FAST_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    data = extract_json(_text_of(response)) or []
    if isinstance(data, dict):
        data = data.get("entries") or data.get("data") or []

    mapping = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("id") is not None:
                mapping[str(item["id"])] = item
    return mapping


def ai_narrative(client, entries: list) -> tuple[str, str]:
    """Model 1 (fast): write the standup summary and blockers for the dashboard."""
    log_text = "\n".join(
        f"- {e['Date']} | {e['Author']} | {e['Commit_Summary']} ({e['Hours']}h, {e['Status']})"
        for e in entries
    )
    system = "You are an engineering manager assistant. Respond with STRICT JSON only."
    user = f"""Based on this week's work log, write a short daily-standup style
summary and list any likely blockers.

Work log:
{log_text}

Return ONLY a JSON object with exactly these keys:
{{"standup_summary": "<2-4 sentence summary>", "blockers": "<comma-separated blockers, or 'None reported'>"}}"""

    summary, blockers = "Summary unavailable.", "None reported"
    try:
        response = client.messages.create(
            model=FAST_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        data = extract_json(_text_of(response)) or {}
        summary = str(data.get("standup_summary") or summary)
        blockers = str(data.get("blockers") or blockers)
    except Exception:  # noqa: BLE001 — keep the pipeline alive if narrative fails
        pass
    return summary, blockers


def compute_top_author(entries: list) -> str:
    """Return the author with the most total hours."""
    hours_by_author: dict[str, float] = defaultdict(float)
    for entry in entries:
        author = str(entry.get("Author") or "").strip()
        if author:
            hours_by_author[author] += _to_float(entry.get("Hours"), 0.0)
    if not hours_by_author:
        return "—"
    return max(hours_by_author, key=hours_by_author.get)


def build_dashboard_row(entries: list, summary: str, blockers: str) -> dict:
    """Assemble a single Dashboard_Summary row from entries + AI narrative."""
    return {
        "Week": datetime.now().strftime("%G-W%V"),  # ISO year + week, e.g. 2026-W28
        "Total_Hours": round(sum(_to_float(e.get("Hours"), 0.0) for e in entries), 2),
        "Top_Author": compute_top_author(entries),
        "Standup_Summary": summary,
        "Blockers": blockers,
    }


# --------------------------------------------------------------------------- #
# D. Google Sheets
# --------------------------------------------------------------------------- #

def setup_sheets(spreadsheet):
    """Ensure the 3 worksheets exist with correct headers; seed example rates."""
    existing = {ws.title: ws for ws in spreadsheet.worksheets()}
    for title, headers in WORKSHEETS.items():
        if title in existing:
            worksheet = existing[title]
            if worksheet.row_values(1) != headers:
                # Clear the whole header row first so columns dropped in a schema
                # change (e.g. the old Ticket_ID) don't leave a stale header behind.
                worksheet.batch_clear(["A1:Z1"])
                worksheet.update(range_name="A1", values=[headers])
        else:
            worksheet = spreadsheet.add_worksheet(
                title=title, rows=500, cols=max(6, len(headers))
            )
            worksheet.update(range_name="A1", values=[headers])

    rates = spreadsheet.worksheet("Rates_Config")
    if len(rates.get_all_values()) <= 1:
        rates.append_rows(EXAMPLE_RATES, value_input_option="USER_ENTERED")


def style_worksheets(spreadsheet):
    """Apply a clean style: bold green header row + frozen top row on each tab."""
    header_fmt = {
        "backgroundColor": SHEET_HEADER_COLOR,
        "horizontalAlignment": "LEFT",
        "textFormat": {
            "bold": True,
            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
        },
    }
    for title, headers in WORKSHEETS.items():
        try:
            worksheet = spreadsheet.worksheet(title)
            last_col = chr(ord("A") + len(headers) - 1)
            worksheet.format(f"A1:{last_col}1", header_fmt)
            worksheet.freeze(rows=1)
        except Exception:  # noqa: BLE001 — styling is best-effort, never fatal
            continue


def read_worksheet_df(spreadsheet, title: str) -> pd.DataFrame:
    """Read a worksheet into a DataFrame using its header row."""
    return pd.DataFrame(spreadsheet.worksheet(title).get_all_records())


def export_to_sheets(spreadsheet, entries: list, dashboard_row: dict):
    """Append entries to Raw_Entries and upsert the weekly Dashboard_Summary row."""
    raw_ws = spreadsheet.worksheet("Raw_Entries")
    headers = WORKSHEETS["Raw_Entries"]
    rows = [[entry[h] for h in headers] for entry in entries]
    if rows:
        raw_ws.append_rows(rows, value_input_option="USER_ENTERED")

    dash_ws = spreadsheet.worksheet("Dashboard_Summary")
    dash_headers = WORKSHEETS["Dashboard_Summary"]
    dash_values = [dashboard_row[h] for h in dash_headers]

    all_values = dash_ws.get_all_values()
    target_row = None
    for index, row in enumerate(all_values[1:], start=2):  # data starts on row 2
        if row and row[0] == dashboard_row["Week"]:
            target_row = index
            break

    if target_row:
        dash_ws.update(range_name=f"A{target_row}", values=[dash_values])
    else:
        dash_ws.append_row(dash_values, value_input_option="USER_ENTERED")


# --------------------------------------------------------------------------- #
# E. Chat (Model 2)
# --------------------------------------------------------------------------- #

def build_chat_system(raw_df, rates_df) -> str:
    """System prompt for Model 2 — data sorted NEWEST FIRST with clear ordering rules."""
    if raw_df is not None and not raw_df.empty:
        tmp = raw_df.copy()
        tmp["_dt"] = pd.to_datetime(tmp.get("Date"), errors="coerce")
        tmp = tmp.sort_values("_dt", ascending=False, na_position="last").drop(columns=["_dt"])
        raw_csv = tmp.to_csv(index=False)
    else:
        raw_csv = "(no timesheet entries yet)"

    rates_csv = (
        rates_df.to_csv(index=False)
        if rates_df is not None and not rates_df.empty else "(no rates configured)"
    )

    return f"""You are a precise, friendly data analyst for a software team's timesheet system.
Answer using ONLY the data below. If it does not contain the answer, say so plainly.

RAW_ENTRIES (CSV) — SORTED NEWEST FIRST. The FIRST data row is the MOST RECENT
commit; the LAST data row is the OLDEST. The `Date` column is a full datetime
(YYYY-MM-DD HH:MM:SS).
{raw_csv}

RATES_CONFIG (CSV) — hourly rate per author:
{rates_csv}

Rules for answering:
- "last", "latest", "most recent", "newest" commit/work  -> the row with the
  MAXIMUM Date (the FIRST data row). Never return the first-in-file row unless it
  is also the newest.
- "first", "earliest", "oldest"  -> the row with the MINIMUM Date (the LAST row).
- When a person is named, filter RAW_ENTRIES by the `Author` column first.
- For billing, match an entry's Author to the Author column in RATES_CONFIG to get
  Hourly_Rate, then multiply by Hours. If an author has no rate, say so.
- Always ground answers in the actual Date/Author/Hours values — never guess."""


def stream_chat(client, messages: list, raw_df, rates_df):
    """Model 2 (smart): stream a conversational answer over the sheet data."""
    system = build_chat_system(raw_df, rates_df)
    with client.messages.stream(
        model=SMART_MODEL,
        max_tokens=2048,
        system=system,
        thinking={"type": "disabled"},
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield text


# --------------------------------------------------------------------------- #
# F. Entries DataFrame, hours estimation, business rules
# --------------------------------------------------------------------------- #

def build_entries_df(commits: list, ai_map: dict) -> pd.DataFrame:
    """Combine git commits + AI cleanup into the editable table DataFrame."""
    rows = []
    for commit in commits:
        info = ai_map.get(commit["id"], {})
        status = str(info.get("status") or "In Progress")
        if status not in STATUS_OPTIONS:
            status = "In Progress"
        rows.append({
            "Date": commit["datetime"],
            "Author": commit["author"],
            "Description": info.get("description") or commit["message"],
            "Hours": 0.0,  # filled in by estimate_hours_df
            "Status": status,
        })

    df = pd.DataFrame(rows, columns=["Date", "Author", "Description", "Hours", "Status"])
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df.insert(1, "Day", df["Date"].map(lambda t: t.strftime("%A") if pd.notna(t) else ""))
    return df[["Date", "Day", "Author", "Description", "Hours", "Status"]]


def estimate_hours_df(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Compute Hours from commit TIME GAPS (per author, per day).

    For each author's commits on a given day, sorted by time:
      - the first commit gets FIRST_COMMIT_HOURS (warm-up before the first commit),
      - every later commit gets the gap since the previous commit, clamped to
        [MIN_COMMIT_HOURS, MAX_GAP_HOURS] (a long gap = a break, not work).
    Each day's total is then capped at `cap_hours` (0 = no cap).
    """
    if df is None or df.empty:
        return df

    df = df.copy()
    dt = pd.to_datetime(df["Date"], errors="coerce")
    day = dt.dt.date
    hours = pd.Series(FIRST_COMMIT_HOURS, index=df.index, dtype=float)

    groups = df.groupby([df["Author"], day]).groups
    for _, idx in groups.items():
        ordered = dt.loc[idx].sort_values()
        prev = None
        for i in ordered.index:
            current = ordered.loc[i]
            if prev is None or pd.isna(current):
                hours.loc[i] = FIRST_COMMIT_HOURS
            else:
                gap = (current - prev).total_seconds() / 3600.0
                hours.loc[i] = round(min(max(gap, MIN_COMMIT_HOURS), MAX_GAP_HOURS), 2)
            if not pd.isna(current):
                prev = current

    cap = cfg.get("cap_hours", 0)
    if cap and cap > 0:
        for _, idx in groups.items():
            total = hours.loc[idx].sum()
            if total > cap > 0:
                hours.loc[idx] = (hours.loc[idx] * (cap / total)).round(2)

    df["Hours"] = hours
    return df


def df_to_entries(df: pd.DataFrame) -> list:
    """Convert the (edited) table DataFrame back to Raw_Entries dict rows."""
    entries = []
    for _, row in df.iterrows():
        description = str(row.get("Description", "") or "").strip()
        date_val = row.get("Date")
        if isinstance(date_val, pd.Timestamp) and pd.notna(date_val):
            date_str = date_val.strftime(DT_FORMAT)
        else:
            date_str = str(date_val or "").strip()
        if not description and not date_str:
            continue  # skip empty rows added in the editor

        status = str(row.get("Status", "In Progress") or "In Progress")
        entries.append({
            "Date": date_str,
            "Author": str(row.get("Author", "") or ""),
            "Commit_Summary": description,
            "Hours": _to_float(row.get("Hours"), 0.0),
            "Status": status if status in STATUS_OPTIONS else "In Progress",
        })
    return entries


def weekly_summary(df: pd.DataFrame, cfg: dict) -> dict:
    """Compute Days Logged / Total Hours / Payable Days honoring business rules."""
    result = {"days_logged": 0, "total_hours": 0.0, "payable_days": 0, "short_days": 0}
    if df is None or df.empty:
        return result

    tmp = df.copy()
    tmp["Hours"] = pd.to_numeric(tmp["Hours"], errors="coerce").fillna(0.0)
    tmp["_day"] = pd.to_datetime(tmp["Date"], errors="coerce").dt.date
    cap = cfg.get("cap_hours", 0)

    for day, group in tmp.groupby("_day"):  # NaT days are dropped by groupby
        day_hours = group["Hours"].sum()
        capped = min(day_hours, cap) if cap and cap > 0 else day_hours
        is_weekend = day.weekday() >= 5
        worked = len(group) > 0 if cfg.get("count_worked", True) else day_hours > 0
        included = (not is_weekend) or cfg.get("include_weekends", False)

        result["days_logged"] += 1
        result["total_hours"] += capped
        if worked and included:
            result["payable_days"] += 1
        if cfg.get("flag_short", True) and 0 < capped < 4:
            result["short_days"] += 1

    result["total_hours"] = round(result["total_hours"], 2)
    return result


def fmt_date_range(df: pd.DataFrame) -> str:
    """Format the min-max date of the entries as a friendly range string."""
    if df is None or df.empty:
        return "—"
    dates = pd.to_datetime(df["Date"], errors="coerce").dropna()
    if dates.empty:
        return "—"
    lo, hi = dates.min(), dates.max()
    if lo.year == hi.year:
        return f"{lo.strftime('%b %d')} – {hi.strftime('%b %d, %Y')}"
    return f"{lo.strftime('%b %d, %Y')} – {hi.strftime('%b %d, %Y')}"


# --------------------------------------------------------------------------- #
# G. Pipeline actions
# --------------------------------------------------------------------------- #

def run_generate(client, cfg: dict):
    """Git (+timestamps) -> Model 1 cleanup -> timestamp hours -> editable table."""
    with st.spinner("Reading Git history..."):
        commits = get_git_data(cfg["days"], cfg["repo"], cfg["author"])

    if not commits:
        st.warning("No commits found for the selected range. Try more days or a different repo/author.")
        return

    st.session_state.commit_count = len(commits)
    if len(commits) > MAX_COMMITS:
        st.info(f"Found {len(commits)} commits — analyzing the {MAX_COMMITS} most recent.")
        commits = commits[:MAX_COMMITS]

    with st.spinner(f"AI Model 1 ({FAST_MODEL}) cleaning up {len(commits)} commits..."):
        ai_map = parse_with_ai_model_1(client, commits)

    df = build_entries_df(commits, ai_map)
    df = estimate_hours_df(df, cfg)

    rates_df = None
    if sheets_configured():
        try:
            spreadsheet = open_spreadsheet()
            setup_sheets(spreadsheet)
            rates_df = read_worksheet_df(spreadsheet, "Rates_Config")
            st.session_state.sheet_url = spreadsheet.url
        except Exception:  # noqa: BLE001 — rates are optional at generate time
            rates_df = None

    with st.spinner(f"AI Model 1 ({FAST_MODEL}) writing the standup summary..."):
        summary, blockers = ai_narrative(client, df_to_entries(df))

    st.session_state.entries_df = df
    st.session_state.standup_summary = summary
    st.session_state.blockers = blockers
    st.session_state.rates_df = rates_df
    st.success("✅ Timesheet generated — review, edit, then export.")


def export_current():
    """Push the current (edited) table + dashboard to Google Sheets."""
    df = st.session_state.get("entries_df")
    if df is None or df.empty:
        st.warning("Nothing to export yet — generate a timesheet first.")
        return

    entries = df_to_entries(df)
    with st.spinner("Connecting to Google Sheets..."):
        spreadsheet = open_spreadsheet()
        setup_sheets(spreadsheet)
        style_worksheets(spreadsheet)

    dashboard_row = build_dashboard_row(
        entries,
        st.session_state.get("standup_summary", "Summary unavailable."),
        st.session_state.get("blockers", "None reported"),
    )

    with st.spinner("Writing rows to Google Sheets..."):
        export_to_sheets(spreadsheet, entries, dashboard_row)

    st.session_state.sheet_url = spreadsheet.url
    st.session_state.last_dashboard = dashboard_row
    st.success("✅ Exported to Google Sheets!")


def get_chat_data(force: bool = False):
    """Load Raw_Entries + Rates_Config into session state for the chat page."""
    if not force and st.session_state.get("chat_data_loaded"):
        return st.session_state.get("chat_raw_df"), st.session_state.get("chat_rates_df")

    spreadsheet = open_spreadsheet()
    raw_df = read_worksheet_df(spreadsheet, "Raw_Entries")
    rates_df = read_worksheet_df(spreadsheet, "Rates_Config")
    st.session_state.chat_raw_df = raw_df
    st.session_state.chat_rates_df = rates_df
    st.session_state.chat_data_loaded = True
    st.session_state.sheet_url = spreadsheet.url
    return raw_df, rates_df


# --------------------------------------------------------------------------- #
# H. Styling (CSS) + render helpers
# --------------------------------------------------------------------------- #

CSS = """
<style>
/* Layout */
.block-container { padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1250px; }
section[data-testid="stSidebar"] { border-right: 1px solid #e6e9ef; }
section[data-testid="stSidebar"] .block-container { padding-top: 1rem; }

/* Hero header */
.hero { background: linear-gradient(135deg,#eef2ff 0%,#f5f3ff 55%,#ecfeff 100%);
        border: 1px solid #e6e9ef; border-radius: 20px; padding: 22px 26px; margin-bottom: 18px;
        display:flex; justify-content:space-between; align-items:center; gap:16px;
        box-shadow: 0 2px 10px rgba(79,70,229,.06); }
.hero h1 { font-size: 27px; font-weight: 800; margin:0; color:#0f172a; letter-spacing:-.02em; }
.hero .sub { color:#5b6472; font-size:14.5px; margin-top:5px; max-width:640px; }

/* Badges / chips */
.badge { display:inline-flex; align-items:center; gap:7px; padding:6px 13px; border-radius:999px; font-size:13px; font-weight:700; white-space:nowrap; }
.badge .dot { width:7px; height:7px; border-radius:50%; display:inline-block; }
.badge-green { background:#e7f6ee; color:#15803d; } .badge-green .dot { background:#22c55e; }
.badge-amber { background:#fef3c7; color:#b45309; } .badge-amber .dot { background:#f59e0b; }
.badge-slate { background:#eef2f7; color:#475569; } .badge-slate .dot { background:#94a3b8; }

/* Stat cards */
.stat-card { background:#fff; border:1px solid #e6e9ef; border-radius:16px; padding:18px 20px;
             box-shadow:0 1px 2px rgba(16,24,40,.04); height:100%; transition:transform .12s ease, box-shadow .12s ease; }
.stat-card:hover { transform: translateY(-2px); box-shadow:0 8px 22px rgba(16,24,40,.08); }
.stat-top { display:flex; align-items:center; gap:12px; margin-bottom:12px; }
.stat-icon { width:42px; height:42px; border-radius:12px; display:flex; align-items:center; justify-content:center; font-size:19px; }
.stat-label { font-size:12px; letter-spacing:.03em; color:#8a94a3; font-weight:700; text-transform:uppercase; }
.stat-value { font-size:29px; font-weight:800; color:#0f172a; line-height:1.05; }
.stat-sub { font-size:12.5px; color:#9aa4b2; margin-top:6px; }

/* Section titles + pills */
.section-title { font-size:18px; font-weight:800; color:#0f172a; display:flex; align-items:center; gap:10px; }
.pill { background:#eef2f7; color:#475569; font-size:12px; font-weight:700; padding:3px 10px; border-radius:999px; }

/* Weekly summary strip */
.summary-strip { display:flex; gap:40px; padding:14px 6px 2px; flex-wrap:wrap; }
.summary-item .k { font-size:12px; letter-spacing:.03em; color:#8a94a3; font-weight:700; text-transform:uppercase; }
.summary-item .v { font-size:24px; font-weight:800; color:#0f172a; }
.summary-item .v small { font-size:14px; color:#94a3b8; font-weight:700; }

/* Sidebar brand */
.brand { display:flex; align-items:center; gap:12px; margin-bottom:6px; }
.brand .logo { width:40px; height:40px; border-radius:11px; background:linear-gradient(135deg,#2563eb,#4f46e5); color:#fff; display:flex; align-items:center; justify-content:center; font-size:20px; }
.brand .name { font-size:17px; font-weight:800; color:#0f172a; line-height:1.1; }
.brand .name small { display:block; font-size:12px; color:#94a3b8; font-weight:500; }
.side-label { font-size:11px; letter-spacing:.06em; color:#94a3b8; font-weight:800; text-transform:uppercase; margin:12px 0 2px; }

/* Buttons */
.stButton>button, .stDownloadButton>button, .stLinkButton>a { border-radius:10px; font-weight:700; }
.stButton>button[kind="primary"] { background:linear-gradient(135deg,#2563eb,#4f46e5); border:none; box-shadow:0 4px 14px rgba(37,99,235,.28); }

/* Chat bubbles */
[data-testid="stChatMessage"] { background:#fff; border:1px solid #eaedf3; border-radius:14px; box-shadow:0 1px 2px rgba(16,24,40,.03); }
</style>
"""


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def status_badge(ready: bool) -> str:
    if ready:
        return '<span class="badge badge-green"><span class="dot"></span>Ready</span>'
    return '<span class="badge badge-amber"><span class="dot"></span>Setup needed</span>'


def render_hero(title: str, subtitle: str, badge_html: str):
    st.markdown(
        f'<div class="hero"><div><h1>{title}</h1>'
        f'<div class="sub">{subtitle}</div></div><div>{badge_html}</div></div>',
        unsafe_allow_html=True,
    )


def stat_card(col, icon: str, icon_bg: str, label: str, value, sub: str):
    col.markdown(
        f'<div class="stat-card"><div class="stat-top">'
        f'<div class="stat-icon" style="background:{icon_bg}">{icon}</div>'
        f'<div class="stat-label">{label}</div></div>'
        f'<div class="stat-value">{value}</div>'
        f'<div class="stat-sub">{sub}</div></div>',
        unsafe_allow_html=True,
    )


def render_stat_cards(df: pd.DataFrame):
    hours = pd.to_numeric(df["Hours"], errors="coerce").fillna(0.0)
    total_hours = round(hours.sum(), 1)
    total_commits = st.session_state.get("commit_count", len(df))
    completed = int((df["Status"] == "Completed").sum())
    contributors = df["Author"].astype(str).str.strip().replace("", pd.NA).dropna().nunique()
    total = max(len(df), 1)
    pct = round(completed / total * 100)

    c1, c2, c3, c4 = st.columns(4)
    stat_card(c1, "🔀", "#e0e7ff", "Total Commits", total_commits, "in selected period")
    stat_card(c2, "🕒", "#ede9fe", "Total Hours", f"{total_hours} h", "from commit time gaps")
    stat_card(c3, "✅", "#dcfce7", "Completed", completed, f"{pct}% of total")
    stat_card(c4, "👥", "#ffedd5", "Contributors", contributors, fmt_date_range(df))


def render_summary_strip(summary: dict, cfg: dict):
    short = ""
    if cfg.get("flag_short", True) and summary["short_days"]:
        short = (f'<span class="badge badge-amber" style="margin-left:14px;">'
                 f'<span class="dot"></span>{summary["short_days"]} short day(s)</span>')
    st.markdown(
        '<div class="summary-strip">'
        f'<div class="summary-item"><div class="k">Days Logged</div><div class="v">{summary["days_logged"]}</div></div>'
        f'<div class="summary-item"><div class="k">Total Hours</div><div class="v">{summary["total_hours"]} <small>h</small></div></div>'
        f'<div class="summary-item"><div class="k">Payable Days</div><div class="v">{summary["payable_days"]} <small>× daily rate</small></div></div>'
        f'<div class="summary-item" style="align-self:center;">{short}</div>'
        '</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# I. Sidebar
# --------------------------------------------------------------------------- #

def render_sidebar():
    sb = st.sidebar
    sb.markdown(
        '<div class="brand"><div class="logo">🗓️</div>'
        '<div class="name">AI Timesheet<small>Generator</small></div></div>',
        unsafe_allow_html=True,
    )

    page = sb.radio("Navigation", ["📋 Generator & Config", "💬 Ask AI"],
                    label_visibility="collapsed")

    sb.markdown('<div class="side-label">Configuration</div>', unsafe_allow_html=True)
    days = int(sb.selectbox("Days to Analyze",
                            ["7 days", "14 days", "30 days", "60 days", "90 days"]).split()[0])

    # The Git repository to analyze is the folder the app is launched from ("."),
    # or an optional GIT_REPO_PATH override in your .env — no sidebar field needed.
    repo = os.getenv("GIT_REPO_PATH", ".").strip() or "."
    is_repo, repo_name, repo_abs = get_repo_info(repo)
    if is_repo:
        sb.caption(f"📁 Repository: **{repo_name}**  ·  `{repo_abs}`")
        authors = get_git_authors(repo)
    else:
        sb.caption("⚠️ No Git repo detected. Launch the app from inside your "
                   "repository, or set `GIT_REPO_PATH` in your `.env`.")
        authors = []

    author_choice = sb.selectbox("Author to track (optional)", ["All authors", *authors])
    author = None if author_choice == "All authors" else author_choice

    with sb.expander("⚙️ Business rules", expanded=False):
        count_worked = st.toggle("Count a day as worked", value=True,
                                 help="A day counts if it has at least one commit.")
        include_weekends = st.toggle("Include weekends", value=False,
                                     help="Off by default for daily workers.")
        flag_short = st.toggle("Flag short days", value=True,
                               help="Days under 4h are flagged for review.")
        cap_hours = st.number_input("Cap hours per day", min_value=0, max_value=24, value=8,
                                    help="0 = no cap. Applied to the timestamp-based hours.")

    sb.markdown('<div class="side-label">Connected</div>', unsafe_allow_html=True)
    render_connection_status(sb)

    sb.markdown('<div class="side-label">Actions</div>', unsafe_allow_html=True)
    df = st.session_state.get("entries_df")
    csv_bytes = df.to_csv(index=False).encode("utf-8") if df is not None and not df.empty else b""
    sb.download_button("⬇️  Download CSV", data=csv_bytes, file_name="timesheet.csv",
                       mime="text/csv", use_container_width=True,
                       disabled=(df is None or df.empty))
    if sb.button("🧹  Clear Chat History", use_container_width=True):
        st.session_state.chat_history = []
        st.toast("Chat history cleared.")

    sb.divider()
    sb.caption(f"Model 1 (fast): `{FAST_MODEL}`")
    sb.caption(f"Model 2 (smart): `{SMART_MODEL}`")
    sb.caption(f"AI Timesheet Generator · {APP_VERSION}")

    cfg = {
        "days": days, "repo": repo, "author": author,
        "cap_hours": int(cap_hours), "count_worked": count_worked,
        "include_weekends": include_weekends, "flag_short": flag_short,
    }
    return page, cfg


def render_connection_status(sb):
    """Show the Google Sheets connection chip + Open/Refresh actions."""
    if not os.path.exists(CREDENTIALS_FILE):
        sb.markdown('<span class="badge badge-slate"><span class="dot"></span>credentials.json missing</span>',
                    unsafe_allow_html=True)
        return
    if not os.getenv("GOOGLE_SHEET_NAME"):
        sb.markdown('<span class="badge badge-slate"><span class="dot"></span>GOOGLE_SHEET_NAME not set</span>',
                    unsafe_allow_html=True)
        return

    ok, url, error = check_connection()
    if ok:
        st.session_state.sheet_url = url
        sb.markdown(
            f'<span class="badge badge-green"><span class="dot"></span>Google Sheets · Connected</span>'
            f'<div style="color:#64748b;font-size:12.5px;margin-top:6px;">{os.getenv("GOOGLE_SHEET_NAME")}</div>',
            unsafe_allow_html=True,
        )
        sb.link_button("🔗  Open Google Sheet", url, use_container_width=True)
    elif error == "not_found":
        sb.markdown('<span class="badge badge-amber"><span class="dot"></span>Sheet not found / not shared</span>',
                    unsafe_allow_html=True)
    else:
        sb.markdown('<span class="badge badge-amber"><span class="dot"></span>Connection error</span>',
                    unsafe_allow_html=True)
        sb.caption(error)

    if sb.button("🔄  Refresh Connection", use_container_width=True):
        _gspread_client.clear()
        check_connection.clear()
        st.rerun()


# --------------------------------------------------------------------------- #
# J. Page: Generator & Config
# --------------------------------------------------------------------------- #

def page_generator(client, cfg: dict):
    ready = client is not None
    render_hero(
        "Generate Timesheet from Git History",
        f"Scan the last {cfg['days']} day(s) of commits and build a structured, editable timesheet.",
        status_badge(ready),
    )

    if client is None:
        st.error("Anthropic API key missing — add ANTHROPIC_API_KEY to your .env and restart.")

    generate = st.button("⚡  Generate Timesheet from Git History",
                         type="primary", use_container_width=True, disabled=client is None)
    if generate:
        try:
            run_generate(client, cfg)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Generation failed: {exc}")

    df = st.session_state.get("entries_df")
    if df is None or df.empty:
        st.info("Configure your sources in the sidebar, then click **Generate** to build a timesheet from your commits.")
        return

    st.write("")
    render_stat_cards(df)
    st.write("")

    with st.container(border=True):
        header_cols = st.columns([3, 1])
        header_cols[0].markdown(
            f'<div class="section-title">Timesheet Entries '
            f'<span class="pill">{len(df)} entries</span></div>',
            unsafe_allow_html=True,
        )
        if header_cols[1].button("🧮  Recalculate Hours", use_container_width=True):
            st.session_state.entries_df = estimate_hours_df(df, cfg)
            df = st.session_state.entries_df

        st.caption("Hours are estimated from the time gaps between your commits "
                   "(first commit of a day = warm-up), then capped by your business rules.")

        edited = st.data_editor(
            df,
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            column_order=["Date", "Day", "Author", "Description", "Hours", "Status"],
            column_config={
                "Date": st.column_config.DatetimeColumn(
                    "Date & Time", format="YYYY-MM-DD HH:mm", disabled=True, width="medium"
                ),
                "Day": st.column_config.TextColumn("Day", disabled=True, width="small"),
                "Author": st.column_config.TextColumn("Author", disabled=True, width="small"),
                "Description": st.column_config.TextColumn("Description", width="large"),
                "Hours": st.column_config.NumberColumn(
                    "Hours", min_value=0.0, max_value=24.0, step=0.25, format="%.2f"
                ),
                "Status": st.column_config.SelectboxColumn(
                    "Status", options=STATUS_OPTIONS, width="small"
                ),
            },
        )
        # Echo-back pattern (no widget key): edits persist across reruns and
        # programmatic mutations like Recalculate take effect immediately.
        st.session_state.entries_df = edited
        render_summary_strip(weekly_summary(edited, cfg), cfg)

    summary = st.session_state.get("standup_summary")
    if summary:
        st.info(f"🗣️ **Standup:** {summary}")
    blockers = str(st.session_state.get("blockers", "")).strip()
    if blockers and blockers.lower() not in ("none reported", "none", "n/a", ""):
        st.warning(f"🚧 **Blockers:** {blockers}")

    st.write("")
    exp_cols = st.columns([3, 1])
    with exp_cols[0]:
        if sheets_configured():
            st.success("Data ready to export! Review the entries above, then export to Google Sheets.")
        else:
            st.warning("Configure Google Sheets (sidebar) to enable export.")
    with exp_cols[1]:
        if st.button("⬆️  Export to Google Sheets", type="primary",
                     use_container_width=True, disabled=not sheets_configured()):
            try:
                export_current()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Export failed: {exc}")


# --------------------------------------------------------------------------- #
# J. Page: Ask AI (chat)
# --------------------------------------------------------------------------- #

SUGGESTIONS = [
    "What was the most recent commit?",
    "Who worked the most hours?",
    "Summarize this week's work",
    "Total hours per author",
]


def run_chat_turn(client, prompt: str):
    """Append the user prompt, stream Model 2's answer, and store both."""
    raw_df, rates_df = None, None
    if sheets_configured():
        try:
            raw_df, rates_df = get_chat_data()
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Answering without live sheet data: {exc}")

    st.session_state.chat_history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        try:
            answer = st.write_stream(
                stream_chat(client, st.session_state.chat_history, raw_df, rates_df)
            )
        except Exception as exc:  # noqa: BLE001
            answer = f"Sorry, I hit an error: {exc}"
            st.error(answer)
    st.session_state.chat_history.append({"role": "assistant", "content": answer})


def page_ask_ai(client):
    render_hero(
        "Ask AI About Your Timesheet",
        "Chat over your stored Google Sheets data with the smart model. Ask about hours, authors, dates, or billing.",
        status_badge(client is not None),
    )

    top = st.columns([1, 1, 3])
    with top[0]:
        if st.button("🔄  Refresh Data", use_container_width=True):
            if not sheets_configured():
                st.error("Google Sheets is not configured — see the sidebar.")
            else:
                try:
                    get_chat_data(force=True)
                    st.success("Data refreshed from Google Sheets.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not load data: {exc}")
    with top[1]:
        if st.button("🧹  Clear Chat", use_container_width=True):
            st.session_state.chat_history = []
            st.rerun()

    # Suggestion chips (only worth showing before the first question).
    if not st.session_state.chat_history:
        st.markdown('<div class="side-label" style="margin-top:2px;">Try asking</div>',
                    unsafe_allow_html=True)
        chip_cols = st.columns(len(SUGGESTIONS))
        for col, question in zip(chip_cols, SUGGESTIONS):
            if col.button(question, use_container_width=True):
                st.session_state.pending_prompt = question
                st.rerun()

    for message in st.session_state.chat_history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    # A chip click (pending_prompt) or a typed message both feed the same handler.
    prompt = st.chat_input("e.g. 'What was Gemata's last commit?' or 'How much do we bill Acme Corp?'")
    prompt = prompt or st.session_state.pop("pending_prompt", None)
    if not prompt:
        return

    if client is None:
        st.error("Anthropic API key missing — add ANTHROPIC_API_KEY to your .env.")
        return
    run_chat_turn(client, prompt)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    load_dotenv()
    st.set_page_config(
        page_title="AI Timesheet Generator",
        page_icon="🗓️",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_css()
    st.session_state.setdefault("chat_history", [])

    client = get_anthropic_client()
    page, cfg = render_sidebar()

    if page.endswith("Ask AI"):
        page_ask_ai(client)
    else:
        page_generator(client, cfg)


if __name__ == "__main__":
    main()
