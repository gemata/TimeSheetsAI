# 🕒 AI Timesheet & Git Analyzer

Turn your **Git history** into an **AI-generated timesheet + standup dashboard**,
store it in **Google Sheets** (which doubles as a database/API), and **chat with
your data** in plain English.

Built with **Streamlit**, the **Anthropic** SDK (two models), **gspread**, and
**pandas**.

---

## ✨ What it does

```
   git log            AI Model 1 (fast)         Google Sheets            AI Model 2 (smart)
 ┌──────────┐        ┌────────────────┐      ┌────────────────┐        ┌────────────────┐
 │ your      │  ───▶ │ parse commits   │ ──▶ │ Raw_Entries     │  ───▶  │ conversational  │
 │ commits   │       │ + estimate hrs  │      │ Dashboard_Summ │        │ Q&A over data   │
 │ (subproc) │       │ + weekly standup│      │ Projects_Config│        │ (chat)          │
 └──────────┘        └────────────────┘      └────────────────┘        └────────────────┘
     Tab 1  ──────────────────────────────────────────────▶        Tab 2
```

1. **Extract** — `subprocess` runs `git log` over your repo for the selected
   number of days (optionally filtered by author).
2. **Parse (Model 1, fast)** — a fast Anthropic model reads each commit and
   returns structured JSON: date, author, ticket ID, a clean summary, estimated
   hours, and a status.
3. **Summarize (Model 1, fast)** — the same fast model writes a daily-standup
   style summary and flags likely blockers for the weekly dashboard.
4. **Store** — `gspread` writes everything into a Google Sheet with three tabs.
   The Sheet **is** the database/API — no separate backend.
5. **Chat (Model 2, smart)** — a smart Anthropic model reads the Sheet contents
   and answers questions instantly, streaming its response into a chat UI.

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
| `Raw_Entries` | Python (append) | `Date`, `Author`, `Ticket_ID`, `Commit_Summary`, `Hours`, `Status` |
| `Dashboard_Summary` | Python (upsert by week) | `Week`, `Total_Hours`, `Top_Project`, `Standup_Summary`, `Blockers` |
| `Projects_Config` | You (lookup table) | `Prefix`, `Project_Name`, `Client`, `Hourly_Rate` |

`Projects_Config` is seeded with example rows on first run so billing questions
work out of the box. Edit it directly in Google Sheets to match your projects —
the chat reads it live. Tickets are matched to projects by the text **before the
dash** in the Ticket ID (e.g. `ABC-123` → prefix `ABC`).

Because the data lives in Sheets, it's also your lightweight "API": any other
tool (or teammate) can read/write the same tabs.

---

## 🔎 How it analyzes your "pushes"

The app inspects your **local repository's commit history** — the commits you've
authored and pushed. Concretely, it runs:

```bash
git -C <repo_path> log --since="<N> days ago" --date=short \
    --pretty=format:"%H%x1f%an%x1f%ad%x1f%s" [--author="<you>"]
```

- `--since` limits the window to the **Days to analyze** slider value.
- `--author` (optional) restricts results to **your** commits.
- Each commit's hash, author, date, and subject line are extracted, then handed
  to Model 1 for parsing and hour-estimation.

So "analyzing your pushes" = reading the commits in the repo you point it at.
Make sure the **Repository path** in the sidebar points to a folder that is a Git
repository (it defaults to `.`, the folder you launch the app from).

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

**Tab 1 — Timesheet & Dashboard**
- Set **Days to analyze**, **Repository path**, and (optionally) an **author**
  filter in the sidebar.
- Click **🚀 Analyze Git & Update Sheets**. The app pulls commits, parses them
  with Model 1, writes to Google Sheets, and shows the dashboard.
- Click **📥 Load latest from Google Sheets** to re-display current stored data.

**Tab 2 — Ask the Data (Chat)**
- Click **🔄 Refresh Data from Sheets** to pull the latest entries into memory
  (also loaded automatically on your first question).
- Ask things like:
  - *"Who worked the most hours this week?"*
  - *"What did the team do on the API project?"*
  - *"How much do we bill Acme Corp for this week's work?"*
- Model 2 streams the answer, reasoning over `Raw_Entries` and `Projects_Config`.

---

## 🛠️ Troubleshooting

| Symptom | Fix |
| --- | --- |
| Sidebar: *credentials.json not found* | Put the service-account JSON in this folder, named exactly `credentials.json`. |
| Sidebar: *Sheet not found* | The Sheet title must match `GOOGLE_SHEET_NAME`, **and** be shared with the service account email. |
| Sidebar: *ANTHROPIC_API_KEY missing* | Add the key to `.env` and restart the app. |
| *No commits found* | Point **Repository path** at a real Git repo, widen **Days to analyze**, or clear the author filter. |
| Git error in Tab 1 | Ensure `git` is installed and the path is a Git repository. |

---

## 🔐 Security

`.env` and `credentials.json` contain secrets and are listed in `.gitignore` —
**never commit them.** The service account only needs access to the one Sheet
you explicitly share with it.

---

## 📁 Project structure

```
.
├── app.py              # The entire Streamlit application
├── requirements.txt    # Python dependencies
├── .env.example        # Template for your environment variables
├── .env                # Your secrets (git-ignored)
├── credentials.json    # Google service-account key (git-ignored)
├── .gitignore
└── README.md
```
