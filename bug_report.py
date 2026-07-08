"""Phân tích video gameplay bằng Gemini -> xuất bug report (JSON + Markdown).

Cài đặt:  pip install google-genai pydantic
API key:  set GEMINI_API_KEY=...   (Windows)  /  export GEMINI_API_KEY=...

Dùng:     python bug_report.py <video.mp4>
Xuất ra:  <video>.bugs.json  và  <video>.bugs.md
"""
import os
import sys
import time
import json
from pathlib import Path

from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()  # đọc .env: GEMINI_API_KEY, GEMINI_MODEL
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-pro-preview")

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
    resp = client.models.generate_content(
        model=model,
        contents=[video_file, "Hãy tìm và báo cáo tất cả các lỗi trong video này."],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=Report,
        ),
    )
    return Report.model_validate_json(resp.text), resp


def analyze(video_path: str, model: str = MODEL) -> Report:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    f = upload_video(client, video_path)
    print("Analyzing (có thể mất vài phút với video dài)...")
    report, _ = run_model(client, f, model)
    return report


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
