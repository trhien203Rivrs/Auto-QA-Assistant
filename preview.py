"""Preview lỗi từ bug report cùng với video.

Yêu cầu: ffplay (đi kèm ffmpeg) có trong PATH.  https://ffmpeg.org

Liệt kê lỗi:      python preview.py <video>
Xem 1 lỗi (nhảy tới timestamp trong video):
                  python preview.py <video> <số thứ tự lỗi>
"""
import os
import sys
import json
import shutil
import subprocess
from pathlib import Path


def find_ffplay() -> str | None:
    """Tìm ffplay: PATH trước, rồi tới thư mục cài winget."""
    p = shutil.which("ffplay")
    if p:
        return p
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    hits = list(root.glob("Gyan.FFmpeg*/**/bin/ffplay.exe"))
    return str(hits[0]) if hits else None


def secs(mmss: str) -> int:
    """'MM:SS' hoặc 'HH:MM:SS' -> giây."""
    parts = [int(p) for p in mmss.strip().split(":")]
    s = 0
    for p in parts:
        s = s * 60 + p
    return s


def load(video: str) -> list[dict]:
    p = Path(video)
    jp = p.with_suffix(p.suffix + ".bugs.json")
    return json.loads(jp.read_text(encoding="utf-8"))["bugs"]


def main():
    if len(sys.argv) < 2:
        sys.exit("Dùng: python preview.py <video> [số thứ tự lỗi]")
    video = sys.argv[1]
    bugs = load(video)

    if len(sys.argv) == 2:
        for i, b in enumerate(bugs, 1):
            t = b["start_time"] + (f"-{b['end_time']}" if b.get("end_time") else "")
            print(f"{i:>2}. [{t:>12}] {b['name']}")
        print("\nXem 1 lỗi:  python preview.py <video> <số>")
        return

    idx = int(sys.argv[2]) - 1
    b = bugs[idx]
    start = secs(b["start_time"])
    print(f"\n#{idx+1} {b['name']}  @ {b['start_time']}")
    print(f"  Mô tả:    {b['description']}")
    print(f"  Actual:   {b['actual_result']}")
    print(f"  Expected: {b['expected_result']}\n")

    # ffplay nhảy tới timestamp; lùi 2s cho có ngữ cảnh. ESC/q để đóng.
    ss = max(0, start - 2)
    cmd = ["-ss", str(ss)]
    if b.get("end_time"):  # tự dừng ở end_time (+2s đệm), nếu có
        cmd += ["-t", str(secs(b["end_time"]) + 2 - ss)]
    ffplay = find_ffplay()
    if ffplay:
        subprocess.run([ffplay, *cmd, "-autoexit", video])
    else:
        # ponytail: không có ffmpeg -> mở player mặc định, tua tay tới timestamp.
        print(f"(ffplay chưa cài) Mở player mặc định — hãy tua tới {b['start_time']}.")
        print("  Cài ffmpeg để tự nhảy timestamp:  winget install Gyan.FFmpeg")
        os.startfile(os.path.abspath(video))


if __name__ == "__main__":
    main()
