"""Phân tích 1 video CÓ SẴN (bỏ qua bước quay) theo đúng luồng mới của qa_app.

Dùng:  python analyze_file.py <video.mp4>
Làm:   1) nén bản AI (fps thấp, 480p, mono) cạnh video gốc
       2) bug_report.py trên bản AI  3) make_review.py trên bản gốc
       4) mở trang review trong trình duyệt.
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
        sys.exit("Dùng: python analyze_file.py <video.mp4>")
    full = Path(sys.argv[1])
    ai = full.with_suffix(".ai.mp4")
    ff = find_ffmpeg()

    print(f"1/3 Nén bản AI ({AI_FPS}fps, {AI_HEIGHT}p, mono) -> {ai.name}")
    subprocess.run([ff, "-y", "-i", str(full),
                    "-vf", f"fps={AI_FPS},scale=-2:{AI_HEIGHT}",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-ac", "1", "-c:a", "aac", "-b:a", "48k",
                    str(ai)], check=True, capture_output=True)

    print("2/3 Gemini phân tích...")
    subprocess.run([sys.executable, "bug_report.py", str(ai)], check=True)

    print("3/3 Tạo trang review...")
    shutil.copy(f"{ai}.bugs.json", f"{full}.bugs.json")  # chung timeline
    subprocess.run([sys.executable, "make_review.py", str(full)], check=True)
    html = Path(f"{full}.review.html").resolve()
    webbrowser.open(html.as_uri())
    print(f"Đã mở: {html}")


if __name__ == "__main__":
    main()
