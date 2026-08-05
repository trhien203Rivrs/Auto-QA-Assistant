"""Analyze an EXISTING video (skip the recording step), same flow as qa_app.

Usage:  python analyze_file.py <video.mp4>
Does:   1) compress an AI copy (low fps, 480p, mono) next to the original
        2) bug_report.py on the AI copy  3) make_review.py on the original
        4) open the review page in the browser.
"""
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from record import find_ffmpeg

AI_FPS = os.environ.get("AI_FPS", "1")
AI_HEIGHT = os.environ.get("AI_HEIGHT", "480")


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python analyze_file.py <video.mp4>")
    full = Path(sys.argv[1])
    ai = full.with_suffix(".ai.mp4")
    ff = find_ffmpeg()

    print(f"1/3 Compressing AI copy ({AI_FPS}fps, {AI_HEIGHT}p, mono) -> {ai.name}")
    subprocess.run([ff, "-y", "-i", str(full),
                    "-vf", f"fps={AI_FPS},scale=-2:{AI_HEIGHT}",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-ac", "1", "-c:a", "aac", "-b:a", "48k",
                    str(ai)], check=True, capture_output=True)

    print("2/3 Gemini analyzing...")
    subprocess.run([sys.executable, "bug_report.py", str(ai)], check=True)

    print("3/3 Building review page...")
    shutil.copy(f"{ai}.bugs.json", f"{full}.bugs.json")  # shared timeline
    subprocess.run([sys.executable, "make_review.py", str(full)], check=True)
    html = Path(f"{full}.review.html").resolve()
    webbrowser.open(html.as_uri())
    print(f"Opened: {html}")


if __name__ == "__main__":
    main()
