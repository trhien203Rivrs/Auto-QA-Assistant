"""Analyze one session: compress AI copy (low fps/480p/mono) -> Gemini -> bugs.json.

Shared by qa_app.py (Analyze button) and server.py (POST /analyze).
bugs.json = {"bugs": [{...Bug, "jira_key": "", "jira_url": ""}]}
"""
import json
import os
import subprocess
import time
from pathlib import Path

import bug_report
from record import find_ffmpeg

AI_FPS = os.environ.get("AI_FPS", "1")
AI_HEIGHT = os.environ.get("AI_HEIGHT", "480")


def make_ai_copy(full: Path, ai: Path):
    subprocess.run(
        [find_ffmpeg(), "-y", "-i", str(full),
         "-vf", f"fps={AI_FPS},scale=-2:{AI_HEIGHT}",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-ac", "1", "-c:a", "aac", "-b:a", "48k", str(ai)],
        check=True, capture_output=True)


def analyze_session(session_dir: Path) -> dict:
    """Compress (if missing) + Gemini. Writes and returns bugs.json content."""
    full, ai = session_dir / "session.mp4", session_dir / "session.ai.mp4"
    if not full.exists():
        raise FileNotFoundError(f"Not found: {full}")
    t0 = time.time()
    if not ai.exists():
        make_ai_copy(full, ai)
    t1 = time.time()
    report = bug_report.analyze(str(ai))
    data = {"bugs": [{**b.model_dump(), "jira_key": "", "jira_url": ""}
                     for b in report.bugs],
            "timing": {"compress_s": round(t1 - t0, 1),
                       "gemini_s": round(time.time() - t1, 1),
                       "ai_size_mb": round(ai.stat().st_size / 1e6, 1)}}
    (session_dir / "bugs.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
