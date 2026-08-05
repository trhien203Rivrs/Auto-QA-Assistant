"""Review server: lists sessions, runs Gemini analysis, pushes bugs to Jira.

Run standalone:  python server.py   ->  http://127.0.0.1:8756
(qa_app.py also runs this server in the background when you click "View sessions".)
"""
import datetime
import json
import shutil
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse

import jira_push
import pipeline
from record import find_ffmpeg

ROOT = Path(__file__).resolve().parent
SESSIONS = ROOT / "sessions"
SESSIONS.mkdir(exist_ok=True)

app = FastAPI(title="QA Review")
_analyzing: set[str] = set()


def _sdir(sid: str) -> Path:
    d = SESSIONS / Path(sid).name  # blocks path traversal
    if not d.is_dir():
        raise HTTPException(404, f"Session {sid} does not exist")
    return d


def _raw(d: Path) -> dict | None:
    f = d / "bugs.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def _bugs(d: Path):
    raw = _raw(d)
    return raw["bugs"] if raw else None


def _save_bugs(d: Path, bugs: list):
    data = _raw(d) or {}
    data["bugs"] = bugs  # keep timing and other keys untouched
    (d / "bugs.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _secs(t: str) -> int:
    s = 0
    for p in t.split(":"):
        s = s * 60 + int(p)
    return s


# ------------------------------------------------------------- sessions ----
@app.get("/api/sessions")
def list_sessions():
    out = []
    for d in sorted(SESSIONS.iterdir(), reverse=True):
        if not (d / "session.mp4").exists():
            continue
        bugs = _bugs(d)
        out.append({
            "id": d.name,
            "analyzed": bugs is not None,
            "analyzing": d.name in _analyzing,
            "bug_count": len(bugs) if bugs else 0,
            "pushed_count": sum(1 for b in (bugs or []) if b.get("jira_key")),
            "size_mb": round((d / "session.mp4").stat().st_size / 1e6, 1),
        })
    return out


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    d = _sdir(sid)
    err = d / "analyze_error.txt"
    raw = _raw(d)
    return {"id": d.name, "video": "session.mp4",
            "bugs": raw["bugs"] if raw else None,
            "timing": (raw or {}).get("timing"),
            "analyzing": d.name in _analyzing,
            "error": err.read_text(encoding="utf-8") if err.exists() else None}


@app.post("/api/sessions/upload")
async def upload_session(file: UploadFile):
    """Upload an existing video as a new session (test the pipeline without recording)."""
    sid = datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "_upload"
    d = SESSIONS / sid
    d.mkdir(parents=True)
    with open(d / "session.mp4", "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"id": sid}


@app.post("/api/sessions/{sid}/analyze")
def analyze(sid: str):
    d = _sdir(sid)
    if d.name in _analyzing:
        raise HTTPException(400, "Already analyzing")
    (d / "analyze_error.txt").unlink(missing_ok=True)
    _analyzing.add(d.name)

    def work():
        try:
            pipeline.analyze_session(d)
        except Exception as e:
            (d / "analyze_error.txt").write_text(str(e), encoding="utf-8")
        finally:
            _analyzing.discard(d.name)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}  # UI polls GET /api/sessions/{sid} until analyzing=False


# ----------------------------------------------------------------- jira ----
@app.get("/api/jira/settings")
def jira_settings():
    return jira_push.public()


@app.get("/api/jira/projects")
def jira_projects():
    try:
        return jira_push.list_projects()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/jira/settings")
def set_jira_project(body: dict):
    try:
        jira_push.set_project(body.get("project_key"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return jira_push.public()


@app.post("/api/sessions/{sid}/bugs/{i}/push")
def push_bug(sid: str, i: int):
    d = _sdir(sid)
    bugs = _bugs(d)
    if bugs is None or not (0 <= i < len(bugs)):
        raise HTTPException(404, "Bug does not exist")
    b = bugs[i]
    if b.get("jira_key"):
        raise HTTPException(400, f"Already pushed: {b['jira_key']}")

    # cut a clip (start-3s .. end+2s) + a screenshot at start, to attach
    ff = find_ffmpeg()
    video = d / "session.mp4"
    start = _secs(b["start_time"])
    end = _secs(b["end_time"]) if b.get("end_time") else start + 5
    ss = max(0, start - 3)
    clip, shot = d / f"bug{i}_clip.mp4", d / f"bug{i}.jpg"
    subprocess.run([ff, "-y", "-ss", str(ss), "-t", str(end + 2 - ss), "-i", str(video),
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(clip)], capture_output=True)
    subprocess.run([ff, "-y", "-ss", str(start), "-i", str(video),
                    "-frames:v", "1", str(shot)], capture_output=True)

    try:
        res = jira_push.push_bug(d, b, [p for p in (shot, clip) if p.exists()])
    except ValueError as e:
        raise HTTPException(400, str(e))
    b["jira_key"], b["jira_url"] = res["key"], res.get("url", "")
    _save_bugs(d, bugs)
    return res


# ------------------------------------------------------------ files & UI ---
@app.get("/api/sessions/{sid}/files/{name}")
def get_file(sid: str, name: str):
    p = _sdir(sid) / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/")
def index():
    return FileResponse(ROOT / "ui.html")


if __name__ == "__main__":
    import uvicorn
    print("QA Review: http://127.0.0.1:8756")
    uvicorn.run(app, host="127.0.0.1", port=8756, log_level="warning")
