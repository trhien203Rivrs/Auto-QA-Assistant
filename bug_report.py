"""Analyze gameplay video with Gemini -> output bug report (JSON + Markdown).

Install:  pip install google-genai pydantic
API key:  set GEMINI_API_KEY=...   (Windows)  /  export GEMINI_API_KEY=...

Usage:    python bug_report.py <video.mp4>
Outputs:  <video>.bugs.json  and  <video>.bugs.md
"""
import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
import time
import json
from pathlib import Path

from record import find_ffmpeg

sys.stdout.reconfigure(encoding="utf-8")  # Windows console can print non-ASCII

from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()  # reads .env: GEMINI_API_KEY, GEMINI_MODEL
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-pro-preview")
FPS = float(os.environ.get("SAMPLE_FPS", "1"))  # frames/sec sent to Gemini (voice is primary)
CHUNK_SEC = int(os.environ.get("CHUNK_SEC", "1200"))     # longer videos -> split into 20-min chunks
ANALYZE_WORKERS = int(os.environ.get("ANALYZE_WORKERS", "3"))  # chunks analyzed in parallel

SYSTEM = (
    "You are a QA AI assistant. This is a video of someone playing a game "
    "while narrating (voice) bugs they notice. Task: pinpoint the EXACT time "
    "each bug occurs and write a bug report based on BOTH sources: the user's "
    "narration and the video's visuals. Only report real bugs (ignore unrelated "
    "commentary). Time format MM:SS from the start of the video. start_time is "
    "REQUIRED; end_time if available."
)


class Bug(BaseModel):
    name: str                      # Bug name
    start_time: str                # MM:SS - required
    end_time: str | None = None    # MM:SS - if available
    description: str               # Bug description
    actual_result: str             # Actual result
    expected_result: str           # Expected result


class Report(BaseModel):
    bugs: list[Bug]


def upload_video(client, video_path: str):
    print(f"Uploading {video_path} ...")
    f = client.files.upload(file=video_path)
    # File must reach ACTIVE state before use
    while f.state.name == "PROCESSING":
        time.sleep(2)
        f = client.files.get(name=f.name)
    if f.state.name != "ACTIVE":
        raise RuntimeError(f"Upload failed: state={f.state.name}")
    return f


def run_model(client, video_file, model: str):
    """Returns (Report, response) - response holds usage_metadata."""
    # Sample at FPS frames/sec + lower media_resolution -> big token savings for long videos.
    part = types.Part(
        file_data=types.FileData(file_uri=video_file.uri,
                                 mime_type=video_file.mime_type),
        video_metadata=types.VideoMetadata(fps=FPS),
    )
    resp = client.models.generate_content(
        model=model,
        contents=[part, "Find and report all bugs in this video."],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=Report,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        ),
    )
    return Report.model_validate_json(resp.text), resp


def _ffprobe_dur(path) -> float:
    ff = find_ffmpeg()
    ffprobe = str(Path(ff).with_name(Path(ff).name.replace("ffmpeg", "ffprobe")))
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


def _secs(t: str) -> int:
    s = 0
    for p in t.split(":"):
        s = s * 60 + int(p)
    return s


def _shift(t: str | None, offset: int) -> str | None:
    """Add offset (seconds) to a MM:SS time -> MM:SS (minutes may exceed 59)."""
    if not t:
        return t
    s = _secs(t) + offset
    return f"{s // 60:02d}:{s % 60:02d}"


def _analyze_chunk(client, path, offset: int, model: str) -> list[Bug]:
    f = upload_video(client, str(path))
    report, _ = run_model(client, f, model)
    for b in report.bugs:  # shift chunk-local times back to full-video timeline
        b.start_time = _shift(b.start_time, offset)
        b.end_time = _shift(b.end_time, offset)
    return report.bugs


def analyze(video_path: str, model: str = MODEL) -> Report:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    if _ffprobe_dur(video_path) <= CHUNK_SEC:  # short enough -> single call as before
        f = upload_video(client, video_path)
        print("Analyzing...")
        report, _ = run_model(client, f, model)
        return report

    # long video -> split into chunks (stream copy, fast) then analyze in parallel
    tmp = Path(tempfile.mkdtemp(prefix="qa_chunks_"))
    try:
        subprocess.run(
            [find_ffmpeg(), "-y", "-i", video_path, "-c", "copy", "-map", "0",
             "-f", "segment", "-segment_time", str(CHUNK_SEC),
             "-reset_timestamps", "1", str(tmp / "chunk_%03d.mp4")],
            check=True, capture_output=True)
        chunks = sorted(tmp.glob("chunk_*.mp4"))
        offsets, acc = [], 0.0
        for c in chunks:  # real offset from each chunk's duration (keyframe cuts are uneven)
            offsets.append(int(acc))
            acc += _ffprobe_dur(c)
        print(f"Split into {len(chunks)} chunks, analyzing with {ANALYZE_WORKERS} parallel workers...")

        def work(arg):
            path, off = arg
            try:
                return _analyze_chunk(client, path, off, model)
            except Exception as e:  # ponytail: a failing chunk is skipped, not the whole session
                print(f"  ! chunk {path.name} failed, skipping: {str(e)[:200]}")
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=ANALYZE_WORKERS) as ex:
            results = list(ex.map(work, zip(chunks, offsets)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bugs = [b for r in results for b in r]
    bugs.sort(key=lambda b: _secs(b.start_time) if b.start_time else 0)
    return Report(bugs=bugs)


def to_markdown(report: Report) -> str:
    out = ["# Bug Report\n"]
    for i, b in enumerate(report.bugs, 1):
        t = b.start_time + (f" – {b.end_time}" if b.end_time else "")
        out += [
            f"## {i}. {b.name}",
            f"- **Time:** {t}",
            f"- **Description:** {b.description}",
            f"- **Actual Result:** {b.actual_result}",
            f"- **Expected Result:** {b.expected_result}\n",
        ]
    return "\n".join(out)


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python bug_report.py <video>")
    video = sys.argv[1]
    report = analyze(video)

    base = Path(video)
    json_path = base.with_suffix(base.suffix + ".bugs.json")
    md_path = base.with_suffix(base.suffix + ".bugs.md")
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")

    print(f"\nFound {len(report.bugs)} bugs.")
    print(f"  {json_path}\n  {md_path}")


if __name__ == "__main__":
    main()
