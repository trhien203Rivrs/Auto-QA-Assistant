"""Quay cửa sổ (hoặc màn hình) + mic thành mp4 để phân tích bug.

Dùng WGC (Windows Graphics Capture) qua windows-capture: quay được nội dung GPU
(Roblox...), BÁM THEO cửa sổ khi di chuyển, chụp cả khi bị cửa sổ khác che.
Frame WGC được pipe sang ffmpeg để encode H.264 + ghép tiếng mic (WGC không có audio).

Cần: pip install windows-capture ; và ffmpeg.
Chạy:  python record.py [tên_file.mp4]      (mặc định session.mp4)
Dừng:  Ctrl+C  (Python bắt tín hiệu, đóng ffmpeg sạch -> file mp4 hoàn chỉnh).
Mic khác:  đặt AUDIO_DEVICE=<tên dshow> trong .env; không thì tự lấy mic đầu tiên.
"""
import os
import re
import sys
import queue
import shutil
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")  # console Windows in được tiếng Việt


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    hits = list(root.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
    return str(hits[0]) if hits else None


def list_windows():
    """[(hwnd, title)] các cửa sổ đang hiện (top-level, có title)."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    wins = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value.strip()
                if t and t not in [w[1] for w in wins]:
                    wins.append((hwnd, t))
        return True

    u.EnumWindows(proc(cb), 0)
    return wins


def first_audio_device(ff):
    """Tên thiết bị audio dshow đầu tiên từ '-list_devices'."""
    out = subprocess.run(
        [ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True, errors="ignore",
    ).stderr
    for line in out.splitlines():
        m = re.search(r'"([^"]+)"\s*\(audio\)', line)
        if m:
            return m.group(1)
    return None


def record_wgc(ff, out, audio, hwnd=None, monitor=None, crop=None):
    """Bắt frame WGC -> pipe sang ffmpeg (encode + ghép mic). Ctrl+C để dừng.

    crop=(x, y, w, h) trong tọa độ frame: chỉ lấy vùng đó (vd viewport Studio).
    """
    from windows_capture import WindowsCapture

    cap = WindowsCapture(cursor_capture=True, window_hwnd=hwnd, monitor_index=monitor)
    # queue nhỏ: frame chờ lâu sẽ bị wallclock đóng dấu trễ -> hình trôi so với tiếng
    q = queue.Queue(maxsize=20)
    dims = {}
    dropped = [0]
    state = {"next_t": 0.0}

    @cap.event
    def on_frame_arrived(frame, ctrl):
        # WGC bắn theo refresh màn hình; chỉ giữ ~30fps cho khớp -r 30 ở dưới
        now = time.monotonic()
        if now < state["next_t"]:
            return
        state["next_t"] = now + 1.0 / 30
        fb = frame.frame_buffer
        if crop:
            x, y, w, h = crop
            fb = fb[y:y + h, x:x + w]            # cắt vùng viewport (BGRA)
        if not dims:
            dims["w"], dims["h"] = fb.shape[1], fb.shape[0]
        try:
            q.put_nowait(fb.tobytes())           # bgra, w*h*4 byte
        except queue.Full:
            dropped[0] += 1                       # encode không kịp -> bỏ frame

    @cap.event
    def on_closed():
        q.put(None)

    ctl = cap.start_free_threaded()
    first = q.get()                  # chờ frame đầu để biết kích thước
    if first is None:
        sys.exit("Không nhận được frame nào (cửa sổ minimize?).")
    w, h = dims["w"], dims["h"]
    print(f"Khung: {w}x{h}  |  Mic: {audio}  |  Ghi ra: {out}")
    print("Đang quay... nhấn Ctrl+C để DỪNG.\n")

    # rawvideo bgra vào từ pipe; wallclock timestamp giữ đồng bộ tiếng/hình khi quay lâu.
    # crop về kích thước chẵn vì libx264 yuv420p cần chia hết cho 2.
    cmd = [
        ff, "-y",
        "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}",
        "-thread_queue_size", "32",
        "-use_wallclock_as_timestamps", "1", "-i", "pipe:0",
    ]
    if audio:
        # rtbufsize lớn: encode nghẽn thì audio CHỜ thay vì bị vứt (mặc định ~3MB=17s,
        # tràn là mất mẫu -> giọng đứt đoạn, trôi sớm). wallclock: cùng đồng hồ với video.
        cmd += ["-f", "dshow", "-rtbufsize", "512M", "-thread_queue_size", "4096",
                "-use_wallclock_as_timestamps", "1", "-i", f"audio={audio}"]
    cmd += [
        "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2",
        "-r", "30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if audio:
        # aresample=async=1: nếu vẫn mất mẫu thì lấp im lặng đúng vị trí theo timestamp
        cmd += ["-af", "aresample=async=1", "-c:a", "aac", "-shortest"]
    cmd += [out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        proc.stdin.write(first)
        while True:
            try:
                buf = q.get(timeout=0.5)   # timeout để Ctrl+C được xử lý kịp
            except queue.Empty:
                continue
            if buf is None:
                break
            proc.stdin.write(buf)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ctl.stop()
        except Exception:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()
    if dropped[0]:
        print(f"(đã bỏ {dropped[0]} frame do encode không kịp — hạ độ phân giải nếu nhiều)")
    print(f"\nXong: {out}")


CROPS_FILE = Path(".crops.json")   # nhớ vùng crop theo tên cửa sổ


def _load_crops():
    try:
        import json
        return json.loads(CROPS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_crop(title, roi):
    import json
    d = _load_crops()
    d[title] = list(roi)
    CROPS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def grab_one_frame(hwnd):
    """Lấy 1 frame WGC (numpy BGRA) rồi dừng — để chọn vùng crop."""
    from windows_capture import WindowsCapture
    q = queue.Queue()
    cap = WindowsCapture(cursor_capture=False, window_hwnd=hwnd)

    @cap.event
    def on_frame_arrived(frame, ctrl):
        q.put(frame.frame_buffer.copy())
        ctrl.stop()

    @cap.event
    def on_closed():
        pass

    ctl = cap.start_free_threaded()
    buf = q.get()
    try:
        ctl.stop()
    except Exception:
        pass
    return buf


def select_roi(hwnd):
    """Kéo chuột chọn vùng ngay trên frame -> (x, y, w, h) hoặc None nếu hủy."""
    import cv2
    frame = grab_one_frame(hwnd)
    print("Kéo chuột chọn vùng viewport -> Enter/Space để OK, 'c' để hủy.")
    x, y, w, h = cv2.selectROI("Chon vung (Enter=OK, C=huy)", frame[:, :, :3],
                               showCrosshair=False)
    cv2.destroyAllWindows()
    return (int(x), int(y), int(w), int(h)) if w and h else None


def choose_crop(hwnd, title):
    """Quyết định vùng crop: dùng vùng đã lưu / chọn mới / cả cửa sổ."""
    saved = _load_crops().get(title)
    if saved:
        a = input(f"Vùng đã lưu {saved}. Enter=dùng, 's'=chọn lại, 'f'=cả cửa sổ: ").strip()
        if a == "f":
            return None
        if a != "s":
            return tuple(saved)
    elif "Roblox Studio" not in title:
        # cửa sổ thường (Roblox Player...): mặc định cả cửa sổ
        if input("Enter=cả cửa sổ, 'c'=chọn vùng crop: ").strip() != "c":
            return None
    # Studio chưa lưu, hoặc người dùng chọn 's'/'c' -> mở chọn vùng
    roi = select_roi(hwnd)
    if roi:
        _save_crop(title, roi)
    return roi


def main():
    ff = find_ffmpeg()
    if not ff:
        sys.exit("Không tìm thấy ffmpeg. Cài: winget install Gyan.FFmpeg")

    out = sys.argv[1] if len(sys.argv) > 1 else "session.mp4"
    audio = os.environ.get("AUDIO_DEVICE") or first_audio_device(ff)
    if not audio:
        print("CẢNH BÁO: không thấy mic (BT chưa kết nối?). Quay VIDEO-ONLY, "
              "không có tiếng -> AI sẽ thiếu ngữ cảnh voice. Kết nối mic rồi chạy lại.\n")

    wins = list_windows()
    print("\n 0. [Toàn màn hình]")
    for i, (_, t) in enumerate(wins, 1):
        print(f"{i:>2}. {t}")
    c = input("\nChọn cửa sổ để quay (số, Enter = toàn màn hình): ").strip()
    if c and c != "0":
        hwnd, title = wins[int(c) - 1]
        print(f"Cửa sổ: {title}  (WGC — bám theo cửa sổ, chụp cả khi bị che)")
        crop = choose_crop(hwnd, title)
        if crop:
            print(f"Chỉ quay vùng crop: {crop}")
        record_wgc(ff, out, audio, hwnd=hwnd, crop=crop)
    else:
        print("Toàn màn hình (màn chính)")
        record_wgc(ff, out, audio, monitor=1)


if __name__ == "__main__":
    main()
