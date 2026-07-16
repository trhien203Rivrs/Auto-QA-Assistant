"""Phân tích video gameplay bằng Gemini -> xuất bug report (JSON + Markdown).

Cài đặt:  pip install google-genai pydantic
API key:  set GEMINI_API_KEY=...   (Windows)  /  export GEMINI_API_KEY=...

Dùng:     python bug_report.py <video.mp4>
Xuất ra:  <video>.bugs.json  và  <video>.bugs.md
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

sys.stdout.reconfigure(encoding="utf-8")  # console Windows in được tiếng Việt

from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()  # đọc .env: GEMINI_API_KEY, GEMINI_MODEL
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-pro-preview")
FPS = float(os.environ.get("SAMPLE_FPS", "1"))  # số frame/giây gửi Gemini (voice là chính)
CHUNK_SEC = int(os.environ.get("CHUNK_SEC", "1200"))     # video dài hơn -> chia khúc 20 phút
ANALYZE_WORKERS = int(os.environ.get("ANALYZE_WORKERS", "3"))  # số khúc phân tích song song

SYSTEM = (
    "Bạn là 1 trợ lý AI của QA. Đây là video quay lại một người vừa chơi game "
    "vừa nói (voice) về lỗi trong game. Nhiệm vụ: chỉ ra CHÍNH XÁC thời điểm xuất "
    "hiện lỗi, viết bug report dựa trên CẢ hai nguồn: lời nói của người dùng và "
    "hình ảnh trong video. Chỉ báo cáo lỗi thực sự (bỏ qua bình luận không liên quan). "
    "Thời gian dạng MM:SS tính từ đầu video. start_time là BẮT BUỘC; end_time nếu có."
)


class Bug(BaseModel):
    name: str                      # Tên lỗi
    start_time: str                # MM:SS - bắt buộc
    end_time: str | None = None    # MM:SS - nếu có
    description: str               # Mô tả lỗi
    actual_result: str             # Kết quả thực tế
    expected_result: str           # Kết quả mong muốn


class Report(BaseModel):
    bugs: list[Bug]


def upload_video(client, video_path: str):
    print(f"Uploading {video_path} ...")
    f = client.files.upload(file=video_path)
    # File cần ở trạng thái ACTIVE trước khi dùng
    while f.state.name == "PROCESSING":
        time.sleep(2)
        f = client.files.get(name=f.name)
    if f.state.name != "ACTIVE":
        raise RuntimeError(f"Upload thất bại: state={f.state.name}")
    return f


def run_model(client, video_file, model: str):
    """Trả về (Report, response) - response chứa usage_metadata."""
    # Lấy mẫu FPS frame/giây + hạ media_resolution -> giảm token mạnh cho video dài.
    part = types.Part(
        file_data=types.FileData(file_uri=video_file.uri,
                                 mime_type=video_file.mime_type),
        video_metadata=types.VideoMetadata(fps=FPS),
    )
    resp = client.models.generate_content(
        model=model,
        contents=[part, "Hãy tìm và báo cáo tất cả các lỗi trong video này."],
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
    """Cộng offset (giây) vào thời gian MM:SS -> MM:SS (phút có thể >59)."""
    if not t:
        return t
    s = _secs(t) + offset
    return f"{s // 60:02d}:{s % 60:02d}"


def _analyze_chunk(client, path, offset: int, model: str) -> list[Bug]:
    f = upload_video(client, str(path))
    report, _ = run_model(client, f, model)
    for b in report.bugs:  # dời thời gian cục bộ của khúc về mốc toàn video
        b.start_time = _shift(b.start_time, offset)
        b.end_time = _shift(b.end_time, offset)
    return report.bugs


def analyze(video_path: str, model: str = MODEL) -> Report:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    if _ffprobe_dur(video_path) <= CHUNK_SEC:  # đủ ngắn -> 1 call như cũ
        f = upload_video(client, video_path)
        print("Analyzing...")
        report, _ = run_model(client, f, model)
        return report

    # dài -> cắt khúc (copy stream, nhanh) rồi phân tích song song
    tmp = Path(tempfile.mkdtemp(prefix="qa_chunks_"))
    try:
        subprocess.run(
            [find_ffmpeg(), "-y", "-i", video_path, "-c", "copy", "-map", "0",
             "-f", "segment", "-segment_time", str(CHUNK_SEC),
             "-reset_timestamps", "1", str(tmp / "chunk_%03d.mp4")],
            check=True, capture_output=True)
        chunks = sorted(tmp.glob("chunk_*.mp4"))
        offsets, acc = [], 0.0
        for c in chunks:  # offset thật theo độ dài từng khúc (cắt theo keyframe nên không đều)
            offsets.append(int(acc))
            acc += _ffprobe_dur(c)
        print(f"Chia {len(chunks)} khúc, phân tích {ANALYZE_WORKERS} luồng song song...")

        def work(arg):
            path, off = arg
            try:
                return _analyze_chunk(client, path, off, model)
            except Exception as e:  # ponytail: 1 khúc lỗi thì bỏ khúc đó, không fail cả session
                print(f"  ! khúc {path.name} lỗi, bỏ qua: {str(e)[:200]}")
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
            f"- **Thời điểm:** {t}",
            f"- **Mô tả:** {b.description}",
            f"- **Actual Result:** {b.actual_result}",
            f"- **Expected Result:** {b.expected_result}\n",
        ]
    return "\n".join(out)


def main():
    if len(sys.argv) != 2:
        sys.exit("Dùng: python bug_report.py <video>")
    video = sys.argv[1]
    report = analyze(video)

    base = Path(video)
    json_path = base.with_suffix(base.suffix + ".bugs.json")
    md_path = base.with_suffix(base.suffix + ".bugs.md")
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")

    print(f"\nTìm thấy {len(report.bugs)} lỗi.")
    print(f"  {json_path}\n  {md_path}")


if __name__ == "__main__":
    main()
