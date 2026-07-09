# 🕒 AI Timesheet & Git Analyzer

Turn your **Git history** into an **AI-generated timesheet + standup dashboard**,
store it in **Google Sheets** (which doubles as a database/API), and **chat with
your data** in plain English.

Built with **Streamlit**, the **Anthropic** SDK (two models), **gspread**, and
**pandas**.

---

## ✨ What it does

```
  git log (+times)    AI Model 1 (fast)         Google Sheets            AI Model 2 (smart)
 ┌──────────┐        ┌────────────────┐      ┌────────────────┐        ┌────────────────┐
 │ your      │  ───▶ │ clean up msgs   │ ──▶ │ Raw_Entries     │  ───▶  │ conversational  │
 │ commits   │       │ hours = time    │      │ Dashboard_Summ │        │ Q&A over data   │
 │ (subproc) │       │   gaps + standup│      │ Rates_Config   │        │ (chat)          │
 └──────────┘        └────────────────┘      └────────────────┘        └────────────────┘
   Generator  ────────────────────────────────────────────▶        Ask AI
```

1. **Extract** — `subprocess` runs `git log` over your repo for the selected
   number of days, **with full commit timestamps** (optionally filtered to one
   author picked from a dropdown of the repo's authors).
2. **Clean up (Model 1, fast)** — a fast Anthropic model rewrites each commit
   into a clear description and infers a status. **Hours are not guessed by the
   AI** — they are computed deterministically from the time gaps between commits
   (see *Hours calculation* below).
3. **Review & edit** — entries land in an **editable table** (like a spreadsheet)
   keyed by **date-time + author**: tweak hours, change statuses, delete rows, or
   **Recalculate Hours**. Live stat cards and a weekly summary (Days Logged /
   Total Hours / Payable Days) update as you edit, honoring your **business rules**.
4. **Summarize (Model 1, fast)** — the same fast model writes a daily-standup
   style summary and flags likely blockers for the dashboard.
5. **Export** — click **Export to Google Sheets** and `gspread` writes everything
   into a Google Sheet with three tabs (with a styled, frozen header row). The
   Sheet **is** the database/API — no separate backend.
6. **Chat (Model 2, smart)** — a smart Anthropic model reads the Sheet contents
   and answers questions instantly, streaming its response into a chat UI.

The interface is a polished, light-themed dashboard (stat cards, editable table,
sidebar navigation, connection status, and quick actions) modeled on a modern
timesheet tool. The theme lives in [`.streamlit/config.toml`](.streamlit/config.toml).

---

## 🧠 The Two-Model AI Approach

This app deliberately uses **two different models for two different jobs**:

| Role | Model (default) | Why |
| --- | --- | --- |
| **Model 1 — Fast** | `claude-haiku-4-5` | High-volume, repetitive work: parsing many commits into JSON and writing short summaries. Fast and cost-efficient. |
| **Model 2 — Smart** | `claude-sonnet-5` | Low-volume, high-value work: conversational reasoning over your data, billing math, cross-referencing the lookup table. |

You get the **speed and low cost** of a small model for the bulk parsing, and
the **reasoning quality** of a larger model for the part where it matters — the
conversation. Both IDs are constants at the top of `app.py`
(`FAST_MODEL` / `SMART_MODEL`), so they're trivial to swap.

> **Note on model IDs:** the model originally referenced for the smart role
> (`claude-3-5-sonnet-20241022`) has been **retired** by Anthropic and now
> returns a 404, so this project uses the current, actively-supported model IDs.

---

## 🗄️ Google Sheets as the Database / API

One Google Sheet with **three worksheets (tabs)**:

| Worksheet | Written by | Columns |
| --- | --- | --- |
| `Raw_Entries` | Python (append) | `Date` (date-time), `Author`, `Commit_Summary`, `Hours`, `Status` |
| `Dashboard_Summary` | Python (upsert by week) | `Week`, `Total_Hours`, `Top_Author`, `Standup_Summary`, `Blockers` |
| `Rates_Config` | You (lookup table) | `Author`, `Client`, `Hourly_Rate` |

Entries are keyed by **author** (there are no ticket IDs). `Rates_Config` is an
author rate card, seeded with example rows on first run — edit it in Google
Sheets to match your real git authors, and the chat uses it for billing math
(match an entry's `Author` → `Hourly_Rate`).

On export, the app **styles the Sheet** for a clean look: a bold green header
row and a frozen top row on every tab. The `Day` column you see in the app is
**derived from the date for display only** — the stored `Raw_Entries` schema is
exactly the five columns above, so the "database" contract stays stable.

Because the data lives in Sheets, it's also your lightweight "API": any other
tool (or teammate) can read/write the same tabs.

---

## 🔎 How it analyzes your "pushes"

The app inspects your **local repository's commit history** — the commits you've
authored and pushed. Concretely, it runs:

```bash
git -C <repo_path> log --since="<N> days ago" \
    --date=format:"%Y-%m-%d %H:%M:%S" \
    --pretty=format:"%H%x1f%an%x1f%ae%x1f%ad%x1f%s" [--author="<name>"]
```

- `--since` limits the window to the **Days to Analyze** value.
- `--date=format:...` captures the **full commit date-time** (used for hours).
- `--author` (optional) restricts results to one author — picked from a
  **dropdown auto-populated with the repo's authors**.
- Each commit's hash, author, email, date-time, and subject are extracted; the
  message is cleaned up by Model 1 and hours are computed from timestamps.

So "analyzing your pushes" = reading the commits in the repo you point it at.
When you set **Local repository path**, the sidebar resolves and shows the exact
repo (name + absolute path) so you know which one you're analyzing.

### ⏱️ Hours calculation

Hours are derived from **commit timestamps**, per author, per day:

- Commits are grouped by author and calendar day and sorted by time.
- The **first commit of a day** is credited a 1.0h "warm-up" (work before the
  first commit).
- Every later commit is credited the **gap since the previous commit**, clamped
  to `[0.25h, 4.0h]` — a gap longer than 4h is treated as a break, not work.
- Each day's total is then **capped** by your *Cap hours per day* business rule.

Click **🧮 Recalculate Hours** any time to re-run this from the current table
(e.g. after editing the entries).

---

## 🚀 Setup

### 1. Install dependencies

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS/Linux:           source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Get an Anthropic API key

Create one at <https://console.anthropic.com/> → **API Keys**.

### 3. Create a Google service account + `credentials.json`

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create (or pick) a project.
3. **Enable** both **Google Sheets API** and **Google Drive API**.
4. **APIs & Services → Credentials → Create Credentials → Service account.**
5. Open the service account → **Keys → Add key → Create new key → JSON**.
6. Save the downloaded file as **`credentials.json`** in this project folder.

### 4. Create the Google Sheet and share it

1. Create a new Google Sheet and give it a title (e.g. `AI Timesheet DB`).
2. Open `credentials.json` and copy the `client_email` value.
3. In the Sheet, click **Share** and share it with that email as an **Editor**.
   *(This step is what lets the app read/write your Sheet.)*

The three worksheet tabs are created automatically on first run — you don't need
to add them by hand.

### 5. Configure environment variables

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Edit `.env`:

```dotenv
ANTHROPIC_API_KEY=sk-ant-your-key-here
GOOGLE_SHEET_NAME=AI Timesheet DB     # must exactly match the Sheet title
```

### 6. Run

```bash
streamlit run app.py
```

Open the URL Streamlit prints (usually <http://localhost:8501>).

---

## 🖥️ Using the app

The sidebar has two pages: **📋 Generator & Config** and **💬 Ask AI**.

### Sidebar — configuration & actions
- **Repository** — analyzed automatically: the folder you launch the app from,
  or an optional `GIT_REPO_PATH` in your `.env`. The sidebar shows the resolved
  **repo name + absolute path** (or a warning if no repo is detected).
- **Days to Analyze** — 7 / 14 / 30 / 60 / 90.
- **Author to track** — a **dropdown** auto-populated with the repo's authors
  (plus *All authors*).
- **Business rules** (expander):
  - *Count a day as worked* — a day counts if it has ≥ 1 commit.
  - *Include weekends* — off by default for daily workers.
  - *Flag short days* — days under 4h are flagged for review.
  - *Cap hours per day* — caps a day's total (applied to the timestamp-based hours).
- **Connected** — live Google Sheets status + **Open Google Sheet** / **Refresh Connection**.
- **Actions** — **Download CSV** (the current table) and **Clear Chat History**.

### 📋 Generator & Config
1. Click **⚡ Generate Timesheet from Git History**. The app pulls commits and
   Model 1 cleans them up into the editable table (hours from timestamps).
2. Review the **stat cards** (Total Commits, Total Hours, Completed %, Contributors).
3. **Edit** the table — change hours/status, delete rows, or click
   **🧮 Recalculate Hours**. The weekly summary strip updates live.
4. Click **⬆️ Export to Google Sheets** to save entries + dashboard to the Sheet.

### 💬 Ask AI
- Click **🔄 Refresh Data** to pull the latest Sheet data (also loaded
  automatically on your first question). **Suggestion chips** offer quick starts.
- Ask things like:
  - *"What was the most recent commit?"* / *"What was Gemata's last commit?"*
  - *"Who worked the most hours this week?"*
  - *"How much do we bill Acme Corp for this week's work?"*
- Model 2 streams the answer over `Raw_Entries` and `Rates_Config`. The data is
  fed to the model **sorted newest-first with explicit date-based ordering
  rules**, so "latest / last / most recent" questions resolve to the newest
  commit (not the first row) — fixing the earlier ordering mix-up.

---

## 🛠️ Troubleshooting

| Symptom | Fix |
| --- | --- |
| Sidebar: *credentials.json not found* | Put the service-account JSON in this folder, named exactly `credentials.json`. |
| Sidebar: *Sheet not found* | The Sheet title must match `GOOGLE_SHEET_NAME`, **and** be shared with the service account email. |
| Sidebar: *ANTHROPIC_API_KEY missing* | Add the key to `.env` and restart the app. |
| *No commits found* | Point **Local repository path** at a real Git repo, widen **Days to Analyze**, or clear the author filter. |
| Git error on the Generator page | Ensure `git` is installed and the path is a Git repository. |
| Export button disabled | Google Sheets isn't configured — check the **Connected** status in the sidebar. |

---

## 🔐 Security

`.env` and `credentials.json` contain secrets and are listed in `.gitignore` —
**never commit them.** The service account only needs access to the one Sheet
you explicitly share with it.

---

## 📁 Project structure

```
.
├── app.py                 # The entire Streamlit application
├── requirements.txt       # Python dependencies
├── .streamlit/
│   └── config.toml        # Light theme (colors, primary color, layout)
├── .env.example           # Template for your environment variables
├── .env                   # Your secrets (git-ignored)
├── credentials.json       # Google service-account key (git-ignored)
├── .gitignore
└── README.md
```
shared sheet link https://docs.google.com/spreadsheets/d/1XHyHc3jq1Pv4KSviwV6WuS6tzI_IvHSUm6aPMDdrecA/edit?usp=sharing
