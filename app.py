"""
AI Timesheet & Git Analyzer
===========================

A single-file Streamlit application that:

  1. Extracts your Git history with `subprocess` (git log).
  2. Parses it with a FAST Anthropic model (Model 1) into structured timesheet
     entries, and generates a weekly standup/dashboard summary.
  3. Stores everything in a Google Sheet (3 worksheets) that acts as the
     database / API, using `gspread`.
  4. Lets you chat with your data using a SMART Anthropic model (Model 2),
     which reads the Sheet contents and answers questions instantly.

The "Two-Model AI Approach":
  - Model 1 (fast, cheap) -> high-volume parsing + summarization.
  - Model 2 (smart)       -> conversational analysis over the stored data.

All UI text and comments are in English.
"""

from __future__ import annotations

import os
import re
import json
import subprocess
from collections import Counter
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
FAST_MODEL = "claude-haiku-4-5"   # Model 1 -> fast parsing & summarization
SMART_MODEL = "claude-sonnet-5"   # Model 2 -> deep, conversational chat analysis

CREDENTIALS_FILE = "credentials.json"   # Google service-account key (git-ignored)
MAX_COMMITS = 200                       # Safety cap to keep prompts affordable

# The 3 internal worksheets (tabs) and their exact column headers.
WORKSHEETS = {
    "Raw_Entries": ["Date", "Author", "Ticket_ID", "Commit_Summary", "Hours", "Status"],
    "Dashboard_Summary": ["Week", "Total_Hours", "Top_Project", "Standup_Summary", "Blockers"],
    "Projects_Config": ["Prefix", "Project_Name", "Client", "Hourly_Rate"],
}

# Example lookup rows seeded into Projects_Config the first time it is created,
# so billing questions in the chat have data to work with out of the box.
EXAMPLE_PROJECTS = [
    ["ABC", "Apollo Billing Core", "Acme Corp", 85],
    ["WEB", "Marketing Website", "Acme Corp", 70],
    ["API", "Public API Platform", "Globex", 95],
]


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
    """
    Robustly extract JSON from an LLM response.

    Handles pure JSON, markdown-fenced JSON, and JSON embedded in prose.
    Returns the parsed object/list, or None if nothing parseable is found.
    """
    if not text:
        return None
    text = text.strip()

    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to slicing out the outermost array or object.
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


# --------------------------------------------------------------------------- #
# B. Backend Functions — Git + AI + Sheets
# --------------------------------------------------------------------------- #

def get_git_data(days_back: int, repo_path: str = ".", author: str | None = None):
    """
    Run `git log` and return a list of commit dicts.

    Each dict: {"hash", "author", "date", "message"}.
    Raises RuntimeError with a friendly message on any git failure.
    """
    sep = "\x1f"  # unit separator — safe delimiter that won't appear in messages
    fmt = sep.join(["%H", "%an", "%ad", "%s"])
    cmd = [
        "git", "-C", repo_path, "log",
        f"--since={days_back} days ago",
        "--date=short",
        f"--pretty=format:{fmt}",
    ]
    if author:
        cmd.append(f"--author={author}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError("Git is not installed or not on your PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("`git log` timed out.")

    if result.returncode != 0:
        detail = result.stderr.strip() or "Unknown git error."
        raise RuntimeError(detail)

    commits = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(sep)
        if len(parts) != 4:
            continue
        commit_hash, commit_author, commit_date, message = parts
        commits.append({
            "hash": commit_hash[:10],
            "author": commit_author,
            "date": commit_date,
            "message": message,
        })
    return commits


def parse_with_ai_model_1(client, commits: list):
    """
    Model 1 (fast): turn raw commits into structured timesheet entries.

    Asks for a strict JSON array. Returns whatever JSON was parsed (list/dict),
    to be normalized by `normalize_entries`.
    """
    log_text = "\n".join(
        f"- date={c['date']} | author={c['author']} | msg={c['message']}"
        for c in commits
    )
    system = (
        "You convert raw git commits into structured timesheet entries. "
        "Respond with STRICT JSON only — no prose, no markdown code fences."
    )
    user = f"""Convert each git commit below into exactly one timesheet entry.

For each commit, produce a JSON object with these keys:
- "date": the commit date in YYYY-MM-DD format
- "author": the commit author
- "ticket_id": the ticket key found in the message (e.g. ABC-123). Use "N/A" if none is present.
- "commit_summary": a short, human-readable summary of the work done
- "hours": estimated effort in hours as a number between 0.5 and 8
- "status": one of "Done", "In Progress", or "Review", inferred from the message

Commits:
{log_text}

Return ONLY a JSON array of these objects."""

    response = client.messages.create(
        model=FAST_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return extract_json(_text_of(response))


def normalize_entries(raw) -> list:
    """Coerce the AI output into clean rows matching the Raw_Entries schema."""
    if isinstance(raw, dict):
        raw = raw.get("entries") or raw.get("data") or []
    if not isinstance(raw, list):
        return []

    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entries.append({
            "Date": str(item.get("date") or item.get("Date") or ""),
            "Author": str(item.get("author") or item.get("Author") or ""),
            "Ticket_ID": str(item.get("ticket_id") or item.get("Ticket_ID") or "N/A"),
            "Commit_Summary": str(
                item.get("commit_summary") or item.get("summary")
                or item.get("Commit_Summary") or ""
            ),
            "Hours": _to_float(item.get("hours") or item.get("Hours")),
            "Status": str(item.get("status") or item.get("Status") or "Logged"),
        })
    return entries


def compute_top_project(entries: list, projects_df: pd.DataFrame | None = None) -> str:
    """Find the most common ticket prefix and map it to a project name if known."""
    prefixes = [
        e["Ticket_ID"].split("-")[0].upper()
        for e in entries
        if e.get("Ticket_ID") and e["Ticket_ID"] != "N/A" and "-" in e["Ticket_ID"]
    ]
    if not prefixes:
        return "General"

    top = Counter(prefixes).most_common(1)[0][0]
    if projects_df is not None and not projects_df.empty and "Prefix" in projects_df.columns:
        match = projects_df[projects_df["Prefix"].astype(str).str.upper() == top]
        if not match.empty and "Project_Name" in match.columns:
            return str(match.iloc[0]["Project_Name"])
    return top


def generate_dashboard_with_ai_model_1(client, entries: list,
                                        projects_df: pd.DataFrame | None = None) -> dict:
    """
    Model 1 (fast): produce the weekly Dashboard_Summary row.

    Totals and the top project are computed in Python; the standup narrative and
    blockers are written by the fast model.
    """
    total_hours = round(sum(_to_float(e.get("Hours")) for e in entries), 2)
    top_project = compute_top_project(entries, projects_df)
    week = datetime.now().strftime("%G-W%V")  # ISO year + ISO week, e.g. 2026-W28

    log_text = "\n".join(
        f"- [{e['Ticket_ID']}] {e['Commit_Summary']} ({e['Hours']}h, {e['Status']})"
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
    except Exception:
        # Keep the dashboard functional even if the narrative call fails.
        pass

    return {
        "Week": week,
        "Total_Hours": total_hours,
        "Top_Project": top_project,
        "Standup_Summary": summary,
        "Blockers": blockers,
    }


def setup_sheets(spreadsheet):
    """Ensure the 3 worksheets exist with correct headers; seed example projects."""
    existing = {ws.title: ws for ws in spreadsheet.worksheets()}
    for title, headers in WORKSHEETS.items():
        if title in existing:
            worksheet = existing[title]
            if worksheet.row_values(1) != headers:
                worksheet.update(range_name="A1", values=[headers])
        else:
            worksheet = spreadsheet.add_worksheet(
                title=title, rows=500, cols=max(6, len(headers))
            )
            worksheet.update(range_name="A1", values=[headers])

    # Seed the lookup table only if it is still empty (header row only).
    projects = spreadsheet.worksheet("Projects_Config")
    if len(projects.get_all_values()) <= 1:
        projects.append_rows(EXAMPLE_PROJECTS, value_input_option="USER_ENTERED")


def read_worksheet_df(spreadsheet, title: str) -> pd.DataFrame:
    """Read a worksheet into a DataFrame using its header row."""
    records = spreadsheet.worksheet(title).get_all_records()
    return pd.DataFrame(records)


def export_to_sheets(spreadsheet, entries: list, dashboard_row: dict):
    """Append entries to Raw_Entries and upsert the weekly Dashboard_Summary row."""
    # 1) Append raw entries.
    raw_ws = spreadsheet.worksheet("Raw_Entries")
    headers = WORKSHEETS["Raw_Entries"]
    rows = [[entry[h] for h in headers] for entry in entries]
    if rows:
        raw_ws.append_rows(rows, value_input_option="USER_ENTERED")

    # 2) Upsert the dashboard row keyed by Week.
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


def build_chat_system(raw_df: pd.DataFrame | None, projects_df: pd.DataFrame | None) -> str:
    """Build the system prompt for Model 2 by embedding the current sheet data."""
    raw_csv = (
        raw_df.to_csv(index=False)
        if raw_df is not None and not raw_df.empty else "(no timesheet entries yet)"
    )
    proj_csv = (
        projects_df.to_csv(index=False)
        if projects_df is not None and not projects_df.empty else "(no projects configured)"
    )
    return f"""You are a precise, friendly data analyst for a software team's timesheet system.
Answer questions using ONLY the data provided below. If the data does not contain
the answer, say so plainly rather than guessing.

RAW_ENTRIES (CSV — one row per commit-derived timesheet entry):
{raw_csv}

PROJECTS_CONFIG (CSV — lookup table):
{proj_csv}

Guidance:
- A ticket's project is found by matching the part before the dash in Ticket_ID
  (e.g. "ABC-123" -> prefix "ABC") against the Prefix column in PROJECTS_CONFIG.
- For billing questions, multiply an entry's Hours by that project's Hourly_Rate.
- Keep answers concise and conversational."""


def stream_chat(client, messages: list, raw_df, projects_df):
    """Model 2 (smart): stream a conversational answer over the sheet data."""
    system = build_chat_system(raw_df, projects_df)
    # Thinking disabled keeps streaming snappy for interactive chat.
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
# Analysis pipeline (Tab 1 action)
# --------------------------------------------------------------------------- #

def run_analysis(client, cfg: dict):
    """End-to-end: git -> Model 1 parse -> Model 1 dashboard -> export to Sheets."""
    with st.spinner("Reading Git history..."):
        commits = get_git_data(cfg["days"], cfg["repo"], cfg["author"])

    if not commits:
        st.warning("No commits found for the selected range. Try more days or a different repo path.")
        return

    if len(commits) > MAX_COMMITS:
        st.info(f"Found {len(commits)} commits — analyzing the {MAX_COMMITS} most recent to stay efficient.")
        commits = commits[:MAX_COMMITS]
    else:
        st.caption(f"Found {len(commits)} commit(s).")

    with st.spinner(f"AI Model 1 ({FAST_MODEL}) parsing commits into entries..."):
        entries = normalize_entries(parse_with_ai_model_1(client, commits))

    if not entries:
        st.error("The AI parser did not return any usable entries. Please try again.")
        return

    with st.spinner("Connecting to Google Sheets & preparing worksheets..."):
        spreadsheet = open_spreadsheet()
        setup_sheets(spreadsheet)
        projects_df = read_worksheet_df(spreadsheet, "Projects_Config")

    with st.spinner(f"AI Model 1 ({FAST_MODEL}) building the weekly dashboard..."):
        dashboard_row = generate_dashboard_with_ai_model_1(client, entries, projects_df)

    with st.spinner("Writing results to Google Sheets..."):
        export_to_sheets(spreadsheet, entries, dashboard_row)

    # Cache results for display without re-reading the Sheet.
    st.session_state.last_entries_df = pd.DataFrame(entries)
    st.session_state.last_dashboard = dashboard_row
    st.session_state.projects_df = projects_df
    st.success("✅ Analysis complete — Google Sheets updated!")


def load_dashboard_view():
    """Fetch the latest state of all three worksheets for display."""
    spreadsheet = open_spreadsheet()
    setup_sheets(spreadsheet)
    st.session_state.last_entries_df = read_worksheet_df(spreadsheet, "Raw_Entries")
    st.session_state.projects_df = read_worksheet_df(spreadsheet, "Projects_Config")

    dash_df = read_worksheet_df(spreadsheet, "Dashboard_Summary")
    if not dash_df.empty:
        st.session_state.last_dashboard = dash_df.iloc[-1].to_dict()
    st.success("Loaded latest data from Google Sheets.")


def get_chat_data(force: bool = False):
    """Load Raw_Entries + Projects_Config into session state for the chat tab."""
    if not force and st.session_state.get("chat_data_loaded"):
        return st.session_state.get("chat_raw_df"), st.session_state.get("chat_projects_df")

    spreadsheet = open_spreadsheet()
    raw_df = read_worksheet_df(spreadsheet, "Raw_Entries")
    projects_df = read_worksheet_df(spreadsheet, "Projects_Config")
    st.session_state.chat_raw_df = raw_df
    st.session_state.chat_projects_df = projects_df
    st.session_state.chat_data_loaded = True
    return raw_df, projects_df


# --------------------------------------------------------------------------- #
# C. UI — Sidebar
# --------------------------------------------------------------------------- #

def render_sidebar() -> dict:
    st.sidebar.title("⚙️ Configuration")

    days = st.sidebar.slider("Days to analyze", min_value=1, max_value=90, value=7)
    repo = st.sidebar.text_input("Repository path", value=".",
                                 help="Path to the Git repo to analyze.")
    author = st.sidebar.text_input("Filter by author (optional)", value="",
                                   help="Leave blank to include all authors.")

    st.sidebar.divider()
    st.sidebar.subheader("Status")

    # Anthropic status.
    if os.getenv("ANTHROPIC_API_KEY"):
        st.sidebar.success("Anthropic API: key detected")
    else:
        st.sidebar.error("Anthropic API: ANTHROPIC_API_KEY missing")

    # Google Sheets status.
    if not os.path.exists(CREDENTIALS_FILE):
        st.sidebar.error("Google Sheets: credentials.json not found")
    elif not os.getenv("GOOGLE_SHEET_NAME"):
        st.sidebar.error("Google Sheets: GOOGLE_SHEET_NAME not set")
    else:
        try:
            open_spreadsheet()
            st.sidebar.success(f"Google Sheets: connected to '{os.getenv('GOOGLE_SHEET_NAME')}'")
        except SpreadsheetNotFound:
            st.sidebar.error("Sheet not found — is it shared with the service account?")
        except Exception as exc:  # noqa: BLE001 — surface any auth/network error
            st.sidebar.error(f"Google Sheets error: {exc}")

    st.sidebar.divider()
    st.sidebar.caption(f"Model 1 (fast): `{FAST_MODEL}`")
    st.sidebar.caption(f"Model 2 (smart): `{SMART_MODEL}`")

    return {"days": days, "repo": repo.strip() or ".", "author": author.strip() or None}


# --------------------------------------------------------------------------- #
# C. UI — Tab 1: Timesheet & Dashboard
# --------------------------------------------------------------------------- #

def _sheets_configured() -> bool:
    return os.path.exists(CREDENTIALS_FILE) and bool(os.getenv("GOOGLE_SHEET_NAME"))


def render_dashboard_view():
    dash = st.session_state.get("last_dashboard")
    if not dash:
        st.info("Run an analysis or load data to see the dashboard.")
        return

    st.markdown("### 📈 Weekly Dashboard")
    c1, c2, c3 = st.columns(3)
    c1.metric("Week", str(dash.get("Week", "-")))
    c2.metric("Total Hours", str(dash.get("Total_Hours", "-")))
    c3.metric("Top Project", str(dash.get("Top_Project", "-")))

    st.info(f"🗣️ **Standup:** {dash.get('Standup_Summary', '-')}")

    blockers = str(dash.get("Blockers", "")).strip()
    if blockers and blockers.lower() not in ("none reported", "none", "n/a", ""):
        st.warning(f"🚧 **Blockers:** {blockers}")


def render_entries_view():
    df = st.session_state.get("last_entries_df")
    if df is not None and not df.empty:
        st.markdown("### 🧾 Raw Entries")
        st.dataframe(df, use_container_width=True, hide_index=True)


def render_projects_view():
    df = st.session_state.get("projects_df")
    with st.expander("📁 Projects Config (lookup table)"):
        if df is not None and not df.empty:
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("No projects loaded yet — run an analysis or load latest data.")


def render_tab1(client, cfg: dict):
    st.subheader("Timesheet & Dashboard")

    col_a, col_b = st.columns(2)
    with col_a:
        analyze = st.button("🚀 Analyze Git & Update Sheets", type="primary",
                            use_container_width=True)
    with col_b:
        refresh = st.button("📥 Load latest from Google Sheets", use_container_width=True)

    if analyze:
        if client is None:
            st.error("Anthropic API key missing. Add ANTHROPIC_API_KEY to your .env.")
        elif not _sheets_configured():
            st.error("Google Sheets is not configured — see the sidebar status.")
        else:
            try:
                run_analysis(client, cfg)
            except Exception as exc:  # noqa: BLE001 — show any pipeline error to the user
                st.error(f"Analysis failed: {exc}")

    if refresh:
        if not _sheets_configured():
            st.error("Google Sheets is not configured — see the sidebar status.")
        else:
            try:
                load_dashboard_view()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not load from Google Sheets: {exc}")

    st.divider()
    render_dashboard_view()
    render_entries_view()
    render_projects_view()


# --------------------------------------------------------------------------- #
# D. UI — Tab 2: Ask the Data (Chat)
# --------------------------------------------------------------------------- #

def render_tab2(client):
    st.subheader("Ask the Data 💬")

    col_a, col_b = st.columns([1, 1])
    with col_a:
        if st.button("🔄 Refresh Data from Sheets", use_container_width=True):
            if not _sheets_configured():
                st.error("Google Sheets is not configured — see the sidebar status.")
            else:
                try:
                    get_chat_data(force=True)
                    st.success("Data refreshed from Google Sheets.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not load data: {exc}")
    with col_b:
        if st.button("🧹 Clear chat", use_container_width=True):
            st.session_state.chat_history = []
            st.rerun()

    # Render the existing conversation.
    for message in st.session_state.chat_history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    prompt = st.chat_input("e.g. 'Who worked the most hours this week?' or 'How much do we bill Acme Corp?'")
    if not prompt:
        return

    if client is None:
        st.error("Anthropic API key missing. Add ANTHROPIC_API_KEY to your .env.")
        return

    # Lazily load the sheet data the chat model reasons over.
    raw_df, projects_df = None, None
    if _sheets_configured():
        try:
            raw_df, projects_df = get_chat_data()
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Answering without live sheet data: {exc}")

    st.session_state.chat_history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        try:
            answer = st.write_stream(
                stream_chat(client, st.session_state.chat_history, raw_df, projects_df)
            )
        except Exception as exc:  # noqa: BLE001
            answer = f"Sorry, I hit an error: {exc}"
            st.error(answer)

    st.session_state.chat_history.append({"role": "assistant", "content": answer})


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    load_dotenv()
    st.set_page_config(page_title="AI Timesheet & Git Analyzer", page_icon="🕒", layout="wide")
    st.session_state.setdefault("chat_history", [])

    st.title("🕒 AI Timesheet & Git Analyzer")
    st.caption(
        "Extract Git history → parse with a fast AI model → store in Google Sheets "
        "→ chat with a smart AI model."
    )

    cfg = render_sidebar()
    client = get_anthropic_client()

    tab1, tab2 = st.tabs(["📊 Timesheet & Dashboard", "💬 Ask the Data (Chat)"])
    with tab1:
        render_tab1(client, cfg)
    with tab2:
        render_tab2(client)


if __name__ == "__main__":
    main()
