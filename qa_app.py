"""QA Recorder — app nhỏ chạy trọn pipeline: quay -> phân tích -> review.

Flow:
  1. "Chọn vùng quay": rê chuột, viền đỏ bám theo phần tử UI (như capture_tool).
     F8 = chọn, Esc = hủy. Chọn phần tử -> quay CỬA SỔ chứa nó bằng WGC
     (bám theo cửa sổ khi di chuyển, quay được GPU/Roblox, không sợ bị che);
     nếu phần tử nhỏ hơn cửa sổ thì crop đúng vùng đó bên trong cửa sổ.
  2. Chọn mic (dropdown, mặc định mic đầu tiên) — thanh VU rung là audio OK.
  3. Bắt đầu -> chơi + nói bug -> Dừng. Ghi session.mp4 vào sessions/<time>/.
     (bản nhẹ session.ai.mp4 gửi Gemini được nén lúc bấm Phân tích — pipeline.py)
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
import time
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from dotenv import load_dotenv

load_dotenv()
ctypes.windll.shcore.SetProcessDpiAwareness(2)  # tọa độ thật trên màn hình scale
sys.stdout.reconfigure(encoding="utf-8")

from record import find_ffmpeg  # noqa: E402

REC_CRF = os.environ.get("REC_CRF", "28")  # nén bản gốc: cao hơn = nhẹ hơn (18–30 hợp lý)
REC_FPS = os.environ.get("REC_FPS", "20")  # fps bản gốc (bản AI: AI_FPS trong pipeline.py)
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
        self.canvas.create_rectangle(1, 1, w - 2, h - 2, outline="#f2a33c", width=2)

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
def _finalize_mp4(ff, path):
    """Remux frag mp4 -> mp4 chuẩn +faststart: mvhd có duration thật.

    File fragmented có mvhd duration=0 nên mỗi player tự đoán độ dài một kiểu
    (web vs Media Player kết thúc khác nhau). Remux copy-only, chạy ~1 giây.
    """
    tmp = path.with_suffix(".fix.mp4")
    r = subprocess.run([ff, "-y", "-i", str(path), "-c", "copy",
                        "-movflags", "+faststart", str(tmp)], capture_output=True)
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(path)
    else:
        tmp.unlink(missing_ok=True)  # remux hỏng thì giữ bản frag còn xem được


def record_session(ff, hwnd, screen_rect, audio, outdir, stop_evt, status):
    """WGC -> ffmpeg, chỉ ghi bản gốc. Bản AI nén sau, lúc bấm Phân tích
    (pipeline.make_ai_copy) — lúc quay encode càng nhẹ càng ít lệch tiếng/hình."""
    from windows_capture import WindowsCapture

    full = outdir / "session.mp4"
    cap = WindowsCapture(cursor_capture=True, window_hwnd=hwnd,
                         monitor_index=None if hwnd else 1)
    # queue nhỏ: frame nằm chờ lâu sẽ bị wallclock đóng dấu trễ -> hình trôi so với tiếng
    q = queue.Queue(maxsize=20)
    dims, state = {}, {}
    frame_gap = 1.0 / float(REC_FPS)

    @cap.event
    def on_frame_arrived(frame, ctrl):
        # WGC bắn theo refresh màn hình (60Hz+); chỉ giữ ~REC_FPS để encode nhẹ đi 3-8 lần
        now = time.monotonic()
        if now < state.get("next_t", 0.0):
            return
        state["next_t"] = now + frame_gap
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
           "-thread_queue_size", "32",
           "-use_wallclock_as_timestamps", "1", "-i", "pipe:0"]
    if audio:
        # rtbufsize lớn + thread_queue lớn: encode nghẽn thì audio CHỜ thay vì bị vứt
        # (buffer mặc định ~3MB = 17s; tràn là mất mẫu -> giọng đứt đoạn, trôi sớm dần).
        # wallclock: audio cùng đồng hồ với video -> mux thẳng hàng.
        cmd += ["-f", "dshow", "-rtbufsize", "512M", "-thread_queue_size", "4096",
                "-use_wallclock_as_timestamps", "1", "-i", f"audio={audio}"]
    # output 1: bản gốc để xem lại
    # movflags frag: ghi file dạng fragmented -> app/ffmpeg chết giữa chừng vẫn xem được
    cmd += ["-map", "0:v"] + amap + [
        "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2", "-r", REC_FPS,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", REC_CRF,
        "-pix_fmt", "yuv420p",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
    if audio:
        # aresample=async=1: nếu vẫn mất mẫu thì lấp im lặng ĐÚNG VỊ TRÍ theo timestamp,
        # thay vì để timeline audio co ngắn lại (giọng nói trôi sớm so với hình)
        # KHÔNG dùng -shortest: mic Bluetooth vào trễ (warmup) không cố định; nếu
        # clip ngắn hơn warmup, -shortest cắt mất track audio -> "lúc có lúc không".
        # aresample=async=1 đã lo sync; để ffmpeg xả nốt buffer audio khi đóng.
        cmd += ["-af", "aresample=async=1", "-c:a", "aac"]
    cmd += [str(full)]

    # stderr ra file log: thấy được cảnh báo 'real-time buffer too full' nếu còn mất audio
    log = open(outdir / "ffmpeg.log", "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
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
        log.close()
    _finalize_mp4(ff, full)
    status(f"Đã lưu {full.name} — bấm Phân tích khi sẵn sàng (bản AI nén lúc đó).")


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
        self.rec_start = 0.0
        self.meter = MicMeter()

        r = self.root = tk.Tk()
        r.title("Auto-QA Recorder")
        r.attributes("-topmost", True)
        r.resizable(False, False)
        r.configure(bg="#16181d")
        try:
            import sv_ttk
            sv_ttk.set_theme("dark")  # ponytail: theme Win11, không có thì dùng ttk mặc định
        except ImportError:
            pass

        style = ttk.Style()
        C = {"accent": "#f2a33c", "accent_hi": "#ffc46b", "accent_ink": "#241a06",
             "danger": "#ff6b6b", "danger_hi": "#ff8a8a", "danger_ink": "#2a0d0d",
             "green": "#5fd39a", "muted": "#98a0ad", "dim": "#6c7482", "text": "#e9ebef"}
        self.C = C
        # nút chính hổ phách / nút danger lúc đang quay (dựa trên Accent.TButton của sv-ttk)
        style.configure("Accent.TButton", background=C["accent"], foreground=C["accent_ink"])
        style.map("Accent.TButton",
                  background=[("pressed", C["accent_hi"]), ("active", C["accent_hi"])])
        style.configure("Danger.TButton", background=C["danger"], foreground=C["danger_ink"])
        style.map("Danger.TButton",
                  background=[("pressed", C["danger_hi"]), ("active", C["danger_hi"])])
        style.configure("Section.TLabel", foreground=C["dim"],
                        font=("Segoe UI", 9, "bold"))
        style.configure("Muted.TLabel", foreground=C["muted"])
        style.configure("Dim.TLabel", foreground=C["dim"])
        style.configure("Mono.TLabel", foreground=C["text"], font=("Consolas", 11))

        pad = {"padx": 14, "pady": 4}
        box = {"padx": 14, "pady": 3}

        # ---- header ----
        top = ttk.Frame(r)
        top.pack(fill="x", padx=14, pady=(12, 2))
        ttk.Label(top, text="●", foreground=C["accent"],
                  font=("Segoe UI", 11)).pack(side="left")
        ttk.Label(top, text="Auto-QA Recorder",
                  font=("Segoe UI", 13, "bold")).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="quay · phân tích · push Jira",
                  font=("Segoe UI", 9), foreground=C["dim"]).pack(
            side="left", padx=(12, 0), pady=(3, 0))

        body = ttk.Frame(r)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        body.columnconfigure(0, weight=1)

        # ---- 1 · vùng quay ----
        ttk.Label(body, text="1 · VÙNG QUAY",
                  style="Section.TLabel").grid(row=0, column=0, sticky="w", **pad)
        self.region_lbl = tk.Label(body, text="Toàn màn hình (chưa chọn)",
                                   font=("Consolas", 10), fg=C["muted"], bg="#20242c",
                                   anchor="w", padx=10, pady=6, relief="flat")
        self.region_lbl.grid(row=1, column=0, sticky="ew", **box)
        ttk.Button(body, text="Chọn vùng  (F8 chọn · Esc hủy)",
                   command=self.pick).grid(row=1, column=1, sticky="e", **box)

        # ---- 2 · mic ----
        ttk.Label(body, text="2 · MIC",
                  style="Section.TLabel").grid(row=2, column=0, sticky="w", **pad)
        self.mics = list_audio_devices(self.ff)
        self.mic_var = tk.StringVar(value=self.mics[0] if self.mics else "(không có mic)")
        microw = ttk.Frame(body)
        microw.grid(row=3, column=0, columnspan=2, sticky="ew", **box)
        cb = ttk.Combobox(microw, textvariable=self.mic_var, values=self.mics,
                          state="readonly")
        cb.pack(side="left", fill="x", expand=True)
        cb.bind("<<ComboboxSelected>>", lambda e: self.start_meter())
        # VU bar vẽ bằng canvas — sv-ttk dựng Progressbar bằng ảnh sprite cố định,
        # không đổi màu fill được nên tự vẽ, đổi màu theo mức trong tick()
        self.vu = tk.Canvas(microw, width=140, height=16, bg="#14161b",
                            highlightthickness=1, highlightbackground="#2a2f3a", bd=0)
        self.vu.pack(side="left", fill="x", expand=True, padx=(8, 0))

        # ---- actions ----
        self.rec_btn = ttk.Button(body, text="●  Bắt đầu quay", style="Accent.TButton",
                                  command=self.toggle)
        self.rec_btn.grid(row=4, column=0, columnspan=2, sticky="ew",
                          padx=14, pady=(12, 2))
        self.an_btn = ttk.Button(body, text="Phân tích (Gemini) + mở review",
                                 command=self.analyze, state="disabled")
        self.an_btn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=14, pady=2)
        ttk.Button(body, text="Xem sessions (review + push Jira)",
                   command=self.open_review).grid(
            row=6, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 4))

        ttk.Separator(body).grid(row=7, column=0, columnspan=2, sticky="ew",
                                 padx=10, pady=(6, 2))

        # ---- status + thời gian quay ----
        statrow = ttk.Frame(body)
        statrow.grid(row=8, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 4))
        self.status_var = tk.StringVar(value="Sẵn sàng.")
        self.status_color = C["muted"]
        self.status_lbl = ttk.Label(statrow, textvariable=self.status_var,
                                    style="Muted.TLabel", wraplength=520)
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.elapsed_var = tk.StringVar(value="—:—")
        ttk.Label(statrow, textvariable=self.elapsed_var,
                  style="Mono.TLabel").pack(side="right")

        if self.mics:
            self.start_meter()
        else:
            self.status("CẢNH BÁO: không thấy mic — sẽ quay video-only.", color=C["accent"])
        self.tick()
        r.protocol("WM_DELETE_WINDOW", self.quit)

    # -- helpers --
    def status(self, msg, color=None):
        self.status_color = color or "#98a0ad"
        self.root.after(0, self.status_var.set, msg)
        self.root.after(0, lambda: self.status_lbl.configure(foreground=self.status_color))

    def tick(self):
        lvl = min(100, self.meter.level * 600)
        w = self.vu.winfo_width() - 2
        col = (self.C["danger"] if lvl >= 85 else
               self.C["accent"] if lvl >= 55 else self.C["green"])
        self.vu.delete("all")
        self.vu.create_rectangle(1, 1, max(2, 1 + int((w - 2) * lvl / 100)), 15,
                                 fill=col, width=0)
        if self.stop_evt:  # đang quay -> đếm giờ
            el = int(time.monotonic() - self.rec_start)
            self.elapsed_var.set(f"{el // 60:02d}:{el % 60:02d}")
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
                self.region_lbl.config(text=label, fg="#f2a33c")
            else:
                self.hwnd = self.screen_rect = None
                self.region_lbl.config(text="Toàn màn hình", fg="#98a0ad")

        RegionPicker(self.root, done)

    # -- record --
    def toggle(self):
        if self.stop_evt:  # đang quay -> dừng
            self.stop_evt.set()
            self.stop_evt = None
            self.rec_btn.config(text="●  Bắt đầu quay", style="Accent.TButton")
            self.elapsed_var.set("—:—")
            self.an_btn.config(state="normal")
            return
        self.outdir = SESSIONS / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.outdir.mkdir(parents=True, exist_ok=True)
        mic = self.mic_var.get()
        audio = mic if mic and "không có" not in mic else None
        self.meter.stop()  # nhả mic cho ffmpeg
        self.stop_evt = threading.Event()
        self.rec_start = time.monotonic()
        threading.Thread(target=record_session,
                         args=(self.ff, self.hwnd, self.screen_rect, audio,
                               self.outdir, self.stop_evt, self.status),
                         daemon=True).start()
        self.rec_btn.config(text="■  Dừng quay", style="Danger.TButton")
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
            self.status("Đang upload + phân tích bằng Gemini (vài phút)...",
                        color="#6ea8ff")
            try:
                data = pipeline.analyze_session(outdir)
            except Exception as e:
                self.status(f"Phân tích lỗi: {str(e)[-300:]}", color="#ff6b6b")
                self.root.after(0, self.an_btn.config, {"state": "normal"})
                return
            self.root.after(0, self.open_review, outdir.name)
            self.status(f"Xong — {len(data['bugs'])} bug. Review + push Jira trong trình duyệt.",
                        color="#5fd39a")
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
