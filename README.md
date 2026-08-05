# Auto-QA

Record gameplay (Windows) → Gemini finds bugs → review on the web → push to Jira.

## Install

```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
winget install Gyan.FFmpeg          # ffmpeg needs to be on PATH
```

Create `.env`:

```ini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
AI_FPS=1                # fps of the compressed copy sent to the AI
AI_HEIGHT=480

# Jira (optional — missing values fall back to MOCK mode, written to pushed_issues.json)
JIRA_BASE_URL=https://xxx.atlassian.net
JIRA_EMAIL=...
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=QA
JIRA_LABEL=auto-qa
```

## How to run

| Task | Command |
|---|---|
| Main app (record → analyze → review) | `python qa_app.py` |
| Analyze an existing video | `python analyze_file.py video.mp4` |
| Just open the web review of sessions | `python server.py` → http://127.0.0.1:8756 |
| Just record | `python record.py out.mp4` (Ctrl+C to stop) |

Flow in `qa_app.py`: **Pick area** (move the mouse, `F8` select / `Esc` cancel) → pick mic → **Start** → play and speak the bugs out loud → **Stop** → **Analyze** → the review page opens automatically.

## Main features

- **WGC recording** — captures GPU content (Roblox), tracks the window as it moves, keeps recording even when covered. Crops to the exact UI area picked.
- **Mic capture with a VU meter** to confirm audio is coming in; the spoken bug description is read by the AI alongside the video.
- **A separate AI-only copy** (`session.ai.mp4`, 1fps/480p/mono) — sent to Gemini fast and cheap, while the review still shows the original.
- **Review page** with a timeline; clicking a bug jumps the video to that moment. The offline version (`make_review.py`) shares the same CSS as the web page so it looks identical.
- **Push to Jira** bug by bug, picking the project right in the UI. Missing credentials → MOCK mode, nothing is sent for real.

## Structure

```
qa_app.py       Tkinter app, orchestrates the whole pipeline
record.py       WGC + mic recording → mp4
pipeline.py     Compress the AI copy + call Gemini → bugs.json
bug_report.py   Gemini: video → list of bugs (pydantic schema)
server.py       FastAPI: list/analyze/push sessions
ui.html         Web review page
make_review.py  Generates the offline review page
jira_push.py    Jira Cloud REST v3
sessions/<time>/session.mp4, session.ai.mp4, bugs.json
```
