"""QA Recorder — app nhỏ chạy trọn pipeline: quay -> phân tích -> review.

Flow:
  1. "Chọn vùng quay": rê chuột, viền đỏ bám theo phần tử UI (như capture_tool).
     F8 = chọn, Esc = hủy. Chọn phần tử -> quay CỬA SỔ chứa nó bằng WGC
     (bám theo cửa sổ khi di chuyển, quay được GPU/Roblox, không sợ bị che);
     nếu phần tử nhỏ hơn cửa sổ thì crop đúng vùng đó bên trong cửa sổ.
  2. Chọn mic (dropdown, mặc định mic đầu tiên) — thanh VU rung là audio OK.
  3. Bắt đầu -> chơi + nói bug -> Dừng. Ghi ra 2 file trong sessions/<time>/:
        session.mp4     : bản gốc 30fps + audio thường (để xem lại)
        session.ai.mp4  : 1fps, 480p, mono (bản nhẹ gửi Gemini)
  4. "Phân tích": chạy bug_report.py trên bản AI (batch), rồi make_review.py
     trên bản gốc -> tự mở trang review (timeline + click nhảy tới bug).

Chạy: python qa_app.py
Cần:  pip install uiautomation sounddevice  (ngoài requirements sẵn có)
Env:  AI_FPS (mặc định 1), AI_HEIGHT (mặc định 480)
"""
import ctypes
import ctypes.wintypes
import datetime
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from dotenv import load_dotenv

load_dotenv()
ctypes.windll.shcore.SetProcessDpiAwareness(2)  # tọa độ thật trên màn hình scale
sys.stdout.reconfigure(encoding="utf-8")

from record import find_ffmpeg  # noqa: E402

AI_FPS = os.environ.get("AI_FPS", "1")
AI_HEIGHT = os.environ.get("AI_HEIGHT", "480")
SESSIONS = Path("sessions")
VK = {"F8": 0x77, "ESC": 0x1B}


def list_audio_devices(ff):
    out = subprocess.run(
        [ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True, errors="ignore",
    ).stderr
    devs = []
    for line in out.splitlines():
        m = re.search(r'"([^"]+)"\s*\(audio\)', line)
        if m:
            devs.append(m.group(1))
    return devs


# ---------------------------------------------------------------- picker ----
class RegionPicker:
    """Overlay viền đỏ theo phần tử UI dưới chuột. F8 chọn, Esc hủy.

    Gọi on_done(hwnd, screen_rect_or_None, label) — screen_rect=None nghĩa là
    phần tử chính là cả cửa sổ.
    """

    def __init__(self, root, on_done):
        import uiautomation as auto
        self.auto = auto
        self.on_done = on_done
        self.ctrl = None
        self.prev = {k: False for k in VK}
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True, "-transparentcolor", "black")
        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.top.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(self.top.winfo_id()) or self.top.winfo_id()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20)  # click-through
        self.poll()

    def poll(self):
        if not self.top.winfo_exists():
            return
        for name, vk in VK.items():
            down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not self.prev[name]:
                if name == "ESC":
                    return self.finish(None, None, None)
                return self.select()
            self.prev[name] = down
        self.track()
        self.top.after(60, self.poll)

    def track(self):
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        try:
            c = self.auto.ControlFromPoint(pt.x, pt.y)
            r = c.BoundingRectangle
        except Exception:
            return
        self.ctrl = c
        w, h = r.width(), r.height()
        if w <= 0 or h <= 0:
            return
        self.top.geometry(f"{w}x{h}+{r.left}+{r.top}")
        self.canvas.delete("all")
        self.canvas.create_rectangle(1, 1, w - 2, h - 2, outline="red", width=3)

    def select(self):
        if not self.ctrl:
            return self.finish(None, None, None)
        try:
            top = self.ctrl.GetTopLevelControl()
            hwnd = top.NativeWindowHandle
            er, tr = self.ctrl.BoundingRectangle, top.BoundingRectangle
        except Exception:
            return self.finish(None, None, None)
        # phần tử ~ cả cửa sổ (chênh <8px mỗi cạnh) -> quay cả cửa sổ, khỏi crop
        same = all(abs(a - b) < 8 for a, b in
                   [(er.left, tr.left), (er.top, tr.top),
                    (er.right, tr.right), (er.bottom, tr.bottom)])
        rect = None if same else (er.left, er.top, er.width(), er.height())
        label = (top.Name or "cửa sổ")[:60] + ("" if same else "  [vùng con]")
        self.finish(hwnd, rect, label)

    def finish(self, hwnd, rect, label):
        self.top.destroy()
        self.on_done(hwnd, rect, label)


def screen_to_frame_crop(hwnd, screen_rect, fw, fh):
    """Đổi rect màn hình -> rect trong frame WGC của cửa sổ (frame có thể là
    client area hoặc cả cửa sổ tùy hệ — so kích thước để biết gốc tọa độ)."""
    u = ctypes.windll.user32
    wr = ctypes.wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(wr))
    cr = ctypes.wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(cr))
    pt = ctypes.wintypes.POINT(0, 0)
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    if abs(fw - cr.right) <= 2 and abs(fh - cr.bottom) <= 2:
        ox, oy = pt.x, pt.y          # frame = client area
    else:
        ox, oy = wr.left, wr.top     # frame = cả cửa sổ
    x, y, w, h = screen_rect
    x, y = max(0, x - ox), max(0, y - oy)
    w, h = min(w, fw - x), min(h, fh - y)
    return (x, y, w, h) if w > 15 and h > 15 else None


# -------------------------------------------------------------- recorder ----
def record_session(ff, hwnd, screen_rect, audio, outdir, stop_evt, status):
    """WGC -> 1 ffmpeg, 2 output: bản gốc 30fps + bản AI nhẹ. Chạy trong thread."""
    from windows_capture import WindowsCapture

    full, ai = outdir / "session.mp4", outdir / "session.ai.mp4"
    cap = WindowsCapture(cursor_capture=True, window_hwnd=hwnd,
                         monitor_index=None if hwnd else 1)
    q = queue.Queue(maxsize=60)
    dims, state = {}, {}

    @cap.event
    def on_frame_arrived(frame, ctrl):
        fb = frame.frame_buffer
        if "crop" not in state:  # tính crop 1 lần khi biết kích thước frame
            if screen_rect and hwnd:
                state["crop"] = screen_to_frame_crop(hwnd, screen_rect,
                                                     fb.shape[1], fb.shape[0])
            elif screen_rect:    # quay màn hình: screen coords = frame coords
                x, y, w, h = screen_rect
                state["crop"] = (max(0, x), max(0, y),
                                 min(w, fb.shape[1] - x), min(h, fb.shape[0] - y))
            else:
                state["crop"] = None
        c = state["crop"]
        if c:
            x, y, w, h = c
            fb = fb[y:y + h, x:x + w]
        if not dims:
            dims["w"], dims["h"] = fb.shape[1], fb.shape[0]
        try:
            q.put_nowait(fb.tobytes())
        except queue.Full:
            pass  # encode không kịp -> bỏ frame

    @cap.event
    def on_closed():
        q.put(None)

    ctl = cap.start_free_threaded()
    first = q.get()
    if first is None:
        status("Không nhận được frame (cửa sổ minimize?)")
        return
    w, h = dims["w"], dims["h"]
    status(f"Đang quay {w}x{h} -> {outdir.name}/ ...")

    amap = ["-map", "1:a"] if audio else []
    cmd = [ff, "-y",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}",
           "-use_wallclock_as_timestamps", "1", "-i", "pipe:0"]
    if audio:
        cmd += ["-f", "dshow", "-i", f"audio={audio}"]
    # output 1: bản gốc để xem lại
    cmd += ["-map", "0:v"] + amap + [
        "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2", "-r", "30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(full)]
    # output 2: bản nhẹ gửi AI (fps thấp, 480p, mono)
    cmd += ["-map", "0:v"] + amap + [
        "-vf", f"fps={AI_FPS},scale=-2:{AI_HEIGHT}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-ac", "1", "-c:a", "aac", "-b:a", "48k", "-shortest"]
    cmd += [str(ai)]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        proc.stdin.write(first)
        while not stop_evt.is_set():
            try:
                buf = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if buf is None:
                break
            proc.stdin.write(buf)
    except OSError:
        pass  # ffmpeg chết giữa chừng
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
    status(f"Đã lưu {full.name} + {ai.name} — bấm Phân tích khi sẵn sàng.")


# -------------------------------------------------------------- mic meter ---
class MicMeter:
    """Đo mức mic để hiện thanh VU. Khớp tên dshow <-> sounddevice theo prefix."""

    def __init__(self):
        self.level = 0.0
        self.stream = None

    def start(self, dshow_name):
        self.stop()
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            return False
        idx, low = None, dshow_name.lower()
        for i, d in enumerate(sd.query_devices()):
            n = d["name"].lower()
            if d["max_input_channels"] > 0 and (n[:28] in low or low[:28] in n):
                idx = i
                break

        def cb(indata, *_):
            self.level = float(np.abs(indata).mean())

        try:
            self.stream = sd.InputStream(device=idx, channels=1, callback=cb)
            self.stream.start()
            return True
        except Exception:
            self.stream = None
            return False

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.level = 0.0


# -------------------------------------------------------------------- app ---
class App:
    def __init__(self):
        self.ff = find_ffmpeg()
        if not self.ff:
            sys.exit("Không tìm thấy ffmpeg. Cài: winget install Gyan.FFmpeg")
        self.hwnd = None
        self.screen_rect = None
        self.stop_evt = None
        self.outdir = None
        self.meter = MicMeter()

        r = self.root = tk.Tk()
        r.title("QA Recorder")
        r.attributes("-topmost", True)
        r.resizable(False, False)
        pad = {"padx": 10, "pady": 4}

        ttk.Label(r, text="1. Vùng quay").grid(row=0, column=0, sticky="w", **pad)
        self.region_lbl = ttk.Label(r, text="Toàn màn hình (chưa chọn)", width=44)
        self.region_lbl.grid(row=0, column=1, sticky="w", **pad)
        ttk.Button(r, text="Chọn (F8 chọn / Esc hủy)",
                   command=self.pick).grid(row=0, column=2, **pad)

        ttk.Label(r, text="2. Mic").grid(row=1, column=0, sticky="w", **pad)
        self.mics = list_audio_devices(self.ff)
        self.mic_var = tk.StringVar(value=self.mics[0] if self.mics else "(không có mic)")
        cb = ttk.Combobox(r, textvariable=self.mic_var, values=self.mics,
                          state="readonly", width=42)
        cb.grid(row=1, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: self.start_meter())
        self.vu = ttk.Progressbar(r, maximum=100, length=150)
        self.vu.grid(row=1, column=2, **pad)

        self.rec_btn = ttk.Button(r, text="●  Bắt đầu quay", command=self.toggle)
        self.rec_btn.grid(row=2, column=0, columnspan=3, sticky="ew", padx=10, pady=8)

        self.an_btn = ttk.Button(r, text="Phân tích (Gemini) + mở review",
                                 command=self.analyze, state="disabled")
        self.an_btn.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10)

        ttk.Button(r, text="Xem sessions (review + push Jira)",
                   command=self.open_review).grid(row=5, column=0, columnspan=3,
                                                  sticky="ew", padx=10, pady=(0, 4))

        self.status_var = tk.StringVar(value="Sẵn sàng.")
        ttk.Label(r, textvariable=self.status_var, foreground="#666",
                  wraplength=560).grid(row=4, column=0, columnspan=3, sticky="w", **pad)

        if self.mics:
            self.start_meter()
        else:
            self.status("CẢNH BÁO: không thấy mic — sẽ quay video-only.")
        self.tick()
        r.protocol("WM_DELETE_WINDOW", self.quit)

    # -- helpers --
    def status(self, msg):
        self.root.after(0, self.status_var.set, msg)

    def tick(self):
        self.vu["value"] = min(100, self.meter.level * 600)
        self.root.after(50, self.tick)

    def start_meter(self):
        mic = self.mic_var.get()
        if mic and "không có" not in mic:
            ok = self.meter.start(mic)
            self.status("Mic OK — nói thử, thanh VU phải rung." if ok
                        else "Không mở được mic này để đo mức (vẫn quay được).")

    # -- pick region --
    def pick(self):
        self.root.withdraw()

        def done(hwnd, rect, label):
            self.root.deiconify()
            if hwnd:
                self.hwnd, self.screen_rect = hwnd, rect
                self.region_lbl.config(text=label)
            else:
                self.hwnd = self.screen_rect = None
                self.region_lbl.config(text="Toàn màn hình")

        RegionPicker(self.root, done)

    # -- record --
    def toggle(self):
        if self.stop_evt:  # đang quay -> dừng
            self.stop_evt.set()
            self.stop_evt = None
            self.rec_btn.config(text="●  Bắt đầu quay")
            self.an_btn.config(state="normal")
            return
        self.outdir = SESSIONS / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.outdir.mkdir(parents=True, exist_ok=True)
        mic = self.mic_var.get()
        audio = mic if mic and "không có" not in mic else None
        self.meter.stop()  # nhả mic cho ffmpeg
        self.stop_evt = threading.Event()
        threading.Thread(target=record_session,
                         args=(self.ff, self.hwnd, self.screen_rect, audio,
                               self.outdir, self.stop_evt, self.status),
                         daemon=True).start()
        self.rec_btn.config(text="■  Dừng")
        self.an_btn.config(state="disabled")

    # -- review server (server.py chạy nền trong app) --
    def ensure_server(self):
        if getattr(self, "_server", None):
            return
        import uvicorn
        import server
        cfg = uvicorn.Config(server.app, host="127.0.0.1", port=8756,
                             log_level="warning")
        self._server = uvicorn.Server(cfg)
        threading.Thread(target=self._server.run, daemon=True).start()

    def open_review(self, session_id=None):
        self.ensure_server()
        url = "http://127.0.0.1:8756/" + (f"#{session_id}" if session_id else "")
        webbrowser.open(url)

    # -- analyze --
    def analyze(self):
        outdir = self.outdir
        if not outdir:
            return
        self.an_btn.config(state="disabled")

        def work():
            import pipeline
            self.status("Đang upload + phân tích bằng Gemini (vài phút)...")
            try:
                data = pipeline.analyze_session(outdir)
            except Exception as e:
                self.status(f"Phân tích lỗi: {str(e)[-300:]}")
                self.root.after(0, self.an_btn.config, {"state": "normal"})
                return
            self.root.after(0, self.open_review, outdir.name)
            self.status(f"Xong — {len(data['bugs'])} bug. Review + push Jira trong trình duyệt.")
            self.root.after(0, self.an_btn.config, {"state": "normal"})

        threading.Thread(target=work, daemon=True).start()

    def quit(self):
        if self.stop_evt:
            self.stop_evt.set()
        self.meter.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
