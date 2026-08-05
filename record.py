"""Record a window (or screen) + mic to mp4 for bug analysis.

Uses WGC (Windows Graphics Capture) via windows-capture: captures GPU content
(Roblox...), TRACKS the window as it moves, captures it even when covered.
WGC frames are piped to ffmpeg to encode H.264 + mux in mic audio (WGC has no audio).

Needs: pip install windows-capture ; and ffmpeg.
Run:   python record.py [file_name.mp4]      (default: session.mp4)
Stop:  Ctrl+C  (Python catches the signal, closes ffmpeg cleanly -> a complete mp4).
Other mic: set AUDIO_DEVICE=<dshow name> in .env; otherwise the first mic is used.
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
sys.stdout.reconfigure(encoding="utf-8")  # Windows console can print non-ASCII


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    hits = list(root.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
    return str(hits[0]) if hits else None


def list_windows():
    """[(hwnd, title)] of currently visible windows (top-level, with a title)."""
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
    """First dshow audio device name from '-list_devices'."""
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
    """Capture WGC frames -> pipe to ffmpeg (encode + mux mic). Ctrl+C to stop.

    crop=(x, y, w, h) in frame coordinates: only that region is kept (e.g. Studio viewport).
    """
    from windows_capture import WindowsCapture

    cap = WindowsCapture(cursor_capture=True, window_hwnd=hwnd, monitor_index=monitor)
    # small queue: a frame stuck waiting gets a late wallclock stamp -> video drifts from audio
    q = queue.Queue(maxsize=20)
    dims = {}
    dropped = [0]
    state = {"next_t": 0.0}

    @cap.event
    def on_frame_arrived(frame, ctrl):
        # WGC fires at the screen's refresh rate; throttle to ~30fps to match -r 30 below
        now = time.monotonic()
        if now < state["next_t"]:
            return
        state["next_t"] = now + 1.0 / 30
        fb = frame.frame_buffer
        if crop:
            x, y, w, h = crop
            fb = fb[y:y + h, x:x + w]            # crop the viewport region (BGRA)
        if not dims:
            dims["w"], dims["h"] = fb.shape[1], fb.shape[0]
        try:
            q.put_nowait(fb.tobytes())           # bgra, w*h*4 bytes
        except queue.Full:
            dropped[0] += 1                       # encode can't keep up -> drop the frame

    @cap.event
    def on_closed():
        q.put(None)

    ctl = cap.start_free_threaded()
    first = q.get()                  # wait for the first frame to learn its size
    if first is None:
        sys.exit("No frames received (window minimized?).")
    w, h = dims["w"], dims["h"]
    print(f"Frame: {w}x{h}  |  Mic: {audio}  |  Output: {out}")
    print("Recording... press Ctrl+C to STOP.\n")

    # rawvideo bgra coming in via pipe; wallclock timestamps keep audio/video in sync for long recordings.
    # crop to even dimensions since libx264 yuv420p needs sizes divisible by 2.
    cmd = [
        ff, "-y",
        "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}",
        "-thread_queue_size", "32",
        "-use_wallclock_as_timestamps", "1", "-i", "pipe:0",
    ]
    if audio:
        # large rtbufsize: if encoding stalls, audio WAITS instead of being dropped (default
        # ~3MB=17s; overflow loses samples -> choppy audio, drifts early). wallclock: same clock as video.
        cmd += ["-f", "dshow", "-rtbufsize", "512M", "-thread_queue_size", "4096",
                "-use_wallclock_as_timestamps", "1", "-i", f"audio={audio}"]
    cmd += [
        "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2",
        "-r", "30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if audio:
        # aresample=async=1: if samples are still lost, fill silence at the right timestamp
        # NO -shortest: Bluetooth mic warmup is delayed; short clips would lose their audio.
        cmd += ["-af", "aresample=async=1", "-c:a", "aac"]
    cmd += [out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        proc.stdin.write(first)
        while True:
            try:
                buf = q.get(timeout=0.5)   # timeout so Ctrl+C is handled promptly
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
        print(f"(dropped {dropped[0]} frames because encoding couldn't keep up — lower resolution if this is high)")
    print(f"\nDone: {out}")


CROPS_FILE = Path(".crops.json")   # remembers the crop region per window title


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
    """Grab a single WGC frame (numpy BGRA) then stop — used to pick the crop region."""
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
    """Drag-select a region right on the frame -> (x, y, w, h) or None if canceled."""
    import cv2
    frame = grab_one_frame(hwnd)
    print("Drag to select the viewport region -> Enter/Space to confirm, 'c' to cancel.")
    x, y, w, h = cv2.selectROI("Select region (Enter=OK, C=cancel)", frame[:, :, :3],
                               showCrosshair=False)
    cv2.destroyAllWindows()
    return (int(x), int(y), int(w), int(h)) if w and h else None


def choose_crop(hwnd, title):
    """Decide the crop region: use a saved one / pick a new one / whole window."""
    saved = _load_crops().get(title)
    if saved:
        a = input(f"Saved region {saved}. Enter=use it, 's'=pick again, 'f'=whole window: ").strip()
        if a == "f":
            return None
        if a != "s":
            return tuple(saved)
    elif "Roblox Studio" not in title:
        # a regular window (Roblox Player...): whole window by default
        if input("Enter=whole window, 'c'=pick a crop region: ").strip() != "c":
            return None
    # Studio with nothing saved yet, or the user chose 's'/'c' -> open the picker
    roi = select_roi(hwnd)
    if roi:
        _save_crop(title, roi)
    return roi


def main():
    ff = find_ffmpeg()
    if not ff:
        sys.exit("ffmpeg not found. Install: winget install Gyan.FFmpeg")

    out = sys.argv[1] if len(sys.argv) > 1 else "session.mp4"
    audio = os.environ.get("AUDIO_DEVICE") or first_audio_device(ff)
    if not audio:
        print("WARNING: no mic found (Bluetooth not connected?). Recording VIDEO-ONLY, "
              "no audio -> the AI will lack voice context. Connect a mic and rerun.\n")

    wins = list_windows()
    print("\n 0. [Full screen]")
    for i, (_, t) in enumerate(wins, 1):
        print(f"{i:>2}. {t}")
    c = input("\nPick a window to record (number, Enter = full screen): ").strip()
    if c and c != "0":
        hwnd, title = wins[int(c) - 1]
        print(f"Window: {title}  (WGC — tracks the window, captures it even when covered)")
        crop = choose_crop(hwnd, title)
        if crop:
            print(f"Recording only the crop region: {crop}")
        record_wgc(ff, out, audio, hwnd=hwnd, crop=crop)
    else:
        print("Full screen (primary monitor)")
        record_wgc(ff, out, audio, monitor=1)


if __name__ == "__main__":
    main()
