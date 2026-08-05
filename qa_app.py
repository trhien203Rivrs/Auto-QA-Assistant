"""QA Recorder — a small app running the whole pipeline: record -> analyze -> review.

Flow:
  1. "Pick area": move the mouse, a red outline tracks the UI element under it
     (like capture_tool). F8 = select, Esc = cancel. Picking an element records
     the WINDOW that contains it via WGC (tracks the window as it moves,
     captures GPU/Roblox content, unaffected by other windows covering it);
     if the element is smaller than the window, it crops to that area inside it.
  2. Pick a mic (dropdown, defaults to the first one) — a moving VU bar means audio is OK.
  3. Start -> play + narrate the bug -> Stop. Writes session.mp4 to sessions/<time>/.
     (the lightweight session.ai.mp4 sent to Gemini is compressed when you click
     Analyze — pipeline.py)
  4. "Analyze": runs bug_report.py on the AI copy (batched), then make_review.py
     on the original -> auto-opens the review page (timeline + click jumps to a bug).

Run: python qa_app.py
Needs: pip install uiautomation sounddevice  (on top of requirements.txt)
Env:  AI_FPS (default 1), AI_HEIGHT (default 480)
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
ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real coordinates on a scaled display
sys.stdout.reconfigure(encoding="utf-8")

from record import find_ffmpeg  # noqa: E402

REC_CRF = os.environ.get("REC_CRF", "28")  # original-copy compression: higher = lighter (18–30 reasonable)
REC_FPS = os.environ.get("REC_FPS", "20")  # original-copy fps (AI copy uses AI_FPS in pipeline.py)
SESSIONS = Path("sessions")
VK = {"F8": 0x77, "ESC": 0x1B}

# ---------------------------------------------------------------- i18n -----
# Defaults to English (AQA_LANG=vi to default to Vietnamese); the EN/VI button in the header switches instantly.
L = {
    "en": {
        "tagline": "record · analyze · push Jira",
        "region_title": "1 · RECORD AREA",
        "region_none": "Full screen (not selected)",
        "pick_btn": "Pick area  (F8 select · Esc cancel)",
        "mic_title": "2 · MIC",
        "mic_none": "(no mic)",
        "rec_start": "●  Start recording",
        "rec_stop": "■  Stop recording",
        "analyze": "Analyze (Gemini) + open review",
        "sessions": "View sessions (review + push Jira)",
        "ready": "Ready.",
        "no_mic_warn": "WARNING: no mic found — will record video only.",
        "mic_ok": "Mic OK — speak now, the VU bar should move.",
        "mic_fail": "Cannot open this mic for level (recording still works).",
        "full_screen": "Full screen",
        "no_frame": "No frames received (window minimized?)",
        "recording": "Recording {w}x{h} -> {name}/ ...",
        "saved": "Saved {name} — click Analyze when ready (AI copy is compressed then).",
        "analyzing": "Uploading + analyzing with Gemini (a few minutes)...",
        "analyze_err": "Analyze failed: {err}",
        "done": "Done — {n} bugs. Review + push Jira in the browser.",
        "ffmpeg_missing": "ffmpeg not found. Install: winget install Gyan.FFmpeg",
        "win": "window",
        "sub": "  [sub-area]",
        "lang_btn": "VI",
    },
    "vi": {
        "tagline": "quay · phân tích · push Jira",
        "region_title": "1 · VÙNG QUAY",
        "region_none": "Toàn màn hình (chưa chọn)",
        "pick_btn": "Chọn vùng  (F8 chọn · Esc hủy)",
        "mic_title": "2 · MIC",
        "mic_none": "(không có mic)",
        "rec_start": "●  Bắt đầu quay",
        "rec_stop": "■  Dừng quay",
        "analyze": "Phân tích (Gemini) + mở review",
        "sessions": "Xem sessions (review + push Jira)",
        "ready": "Sẵn sàng.",
        "no_mic_warn": "CẢNH BÁO: không thấy mic — sẽ quay video-only.",
        "mic_ok": "Mic OK — nói thử, thanh VU phải rung.",
        "mic_fail": "Không mở được mic này để đo mức (vẫn quay được).",
        "full_screen": "Toàn màn hình",
        "no_frame": "Không nhận được frame (cửa sổ minimize?)",
        "recording": "Đang quay {w}x{h} -> {name}/ ...",
        "saved": "Đã lưu {name} — bấm Phân tích khi sẵn sàng (bản AI nén lúc đó).",
        "analyzing": "Đang upload + phân tích bằng Gemini (vài phút)...",
        "analyze_err": "Phân tích lỗi: {err}",
        "done": "Xong — {n} bug. Review + push Jira trong trình duyệt.",
        "ffmpeg_missing": "Không tìm thấy ffmpeg. Cài: winget install Gyan.FFmpeg",
        "win": "cửa sổ",
        "sub": "  [vùng con]",
        "lang_btn": "EN",
    },
}
lang = os.environ.get("AQA_LANG", "en").lower()


def t(k, **kw):
    """Look up key k in the current language; if **kw is given, format {placeholders}."""
    d = L.get(lang, L["en"])
    s = d.get(k) or L["en"].get(k) or k
    return s.format(**kw) if kw else s


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
    """Red-outline overlay tracking the UI element under the mouse. F8 selects, Esc cancels.

    Calls on_done(hwnd, screen_rect_or_None, label) — screen_rect=None means
    the element itself is the whole window.
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
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20)  # click-through overlay
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
        self.canvas.create_rectangle(1, 1, w - 2, h - 2, outline="#ff0000", width=3)

    def select(self):
        if not self.ctrl:
            return self.finish(None, None, None)
        try:
            top = self.ctrl.GetTopLevelControl()
            hwnd = top.NativeWindowHandle
            er, tr = self.ctrl.BoundingRectangle, top.BoundingRectangle
        except Exception:
            return self.finish(None, None, None)
        # element ~ whole window (< 8px off on each edge) -> record the whole window, no crop
        same = all(abs(a - b) < 8 for a, b in
                   [(er.left, tr.left), (er.top, tr.top),
                    (er.right, tr.right), (er.bottom, tr.bottom)])
        rect = None if same else (er.left, er.top, er.width(), er.height())
        label = (top.Name or t("win"))[:60] + ("" if same else t("sub"))
        self.finish(hwnd, rect, label)

    def finish(self, hwnd, rect, label):
        self.top.destroy()
        self.on_done(hwnd, rect, label)


def screen_to_frame_crop(hwnd, screen_rect, fw, fh):
    """Convert a screen rect -> a rect in the window's WGC frame (the frame may be
    the client area or the whole window depending on the system — compare sizes to find the origin)."""
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
        ox, oy = wr.left, wr.top     # frame = whole window
    x, y, w, h = screen_rect
    x, y = max(0, x - ox), max(0, y - oy)
    w, h = min(w, fw - x), min(h, fh - y)
    return (x, y, w, h) if w > 15 and h > 15 else None


# -------------------------------------------------------------- recorder ----
def _finalize_mp4(ff, path):
    """Remux fragmented mp4 -> standard +faststart mp4: mvhd gets a real duration.

    A fragmented file has mvhd duration=0 so each player guesses the length
    differently (web vs Media Player end differently). Copy-only remux, takes ~1 second.
    """
    tmp = path.with_suffix(".fix.mp4")
    r = subprocess.run([ff, "-y", "-i", str(path), "-c", "copy",
                        "-movflags", "+faststart", str(tmp)], capture_output=True)
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(path)
    else:
        tmp.unlink(missing_ok=True)  # remux failed -> keep the still-playable fragmented copy


def record_session(ff, hwnd, screen_rect, audio, outdir, stop_evt, status):
    """WGC -> ffmpeg, writes only the original copy. The AI copy is compressed later,
    when Analyze is clicked (pipeline.make_ai_copy) — the lighter the encode while
    recording, the less audio/video drift."""
    from windows_capture import WindowsCapture

    full = outdir / "session.mp4"
    cap = WindowsCapture(cursor_capture=True, window_hwnd=hwnd,
                         monitor_index=None if hwnd else 1)
    # small queue: a frame stuck waiting gets a late wallclock stamp -> video drifts from audio
    q = queue.Queue(maxsize=20)
    dims, state = {}, {}
    frame_gap = 1.0 / float(REC_FPS)

    @cap.event
    def on_frame_arrived(frame, ctrl):
        # WGC fires at the screen's refresh rate (60Hz+); throttle to ~REC_FPS, 3-8x lighter to encode
        now = time.monotonic()
        if now < state.get("next_t", 0.0):
            return
        state["next_t"] = now + frame_gap
        fb = frame.frame_buffer
        if "crop" not in state:  # compute the crop once, once the frame size is known
            if screen_rect and hwnd:
                state["crop"] = screen_to_frame_crop(hwnd, screen_rect,
                                                     fb.shape[1], fb.shape[0])
            elif screen_rect:    # recording the screen: screen coords = frame coords
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
            pass  # encode can't keep up -> drop the frame

    @cap.event
    def on_closed():
        q.put(None)

    ctl = cap.start_free_threaded()
    first = q.get()
    if first is None:
        status(t("no_frame"))
        return
    w, h = dims["w"], dims["h"]
    status(t("recording", w=w, h=h, name=outdir.name))

    amap = ["-map", "1:a"] if audio else []
    cmd = [ff, "-y",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}",
           "-thread_queue_size", "32",
           "-use_wallclock_as_timestamps", "1", "-i", "pipe:0"]
    if audio:
        # large rtbufsize + thread_queue: if encoding stalls, audio WAITS instead of being dropped
        # (default buffer ~3MB = 17s; overflow loses samples -> choppy audio, drifting earlier over time).
        # wallclock: audio shares video's clock -> mux stays aligned.
        cmd += ["-f", "dshow", "-rtbufsize", "512M", "-thread_queue_size", "4096",
                "-use_wallclock_as_timestamps", "1", "-i", f"audio={audio}"]
    # output 1: the original copy for review
    # movflags frag: writes a fragmented file -> still playable if the app/ffmpeg dies mid-recording
    cmd += ["-map", "0:v"] + amap + [
        "-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2", "-r", REC_FPS,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", REC_CRF,
        "-pix_fmt", "yuv420p",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
    if audio:
        # aresample=async=1: if samples are still lost, fill silence at the RIGHT TIMESTAMP,
        # instead of letting the audio timeline shrink (voice drifting earlier than video)
        # NO -shortest: Bluetooth mic startup delay (warmup) isn't fixed; if the clip is
        # shorter than the warmup, -shortest cuts the audio track -> "sometimes there, sometimes not".
        # aresample=async=1 already handles sync; let ffmpeg flush the remaining audio buffer on close.
        cmd += ["-af", "aresample=async=1", "-c:a", "aac"]
    cmd += [str(full)]

    # stderr goes to a log file: shows 'real-time buffer too full' warnings if audio is still being lost
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
        pass  # ffmpeg died mid-recording
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
    status(t("saved", name=full.name))


# -------------------------------------------------------------- mic meter ---
class MicMeter:
    """Measures mic level to drive the VU bar. Matches dshow <-> sounddevice names by prefix."""

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
            sys.exit(t("ffmpeg_missing"))
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
            sv_ttk.set_theme("dark")  # ponytail: Win11 theme, falls back to default ttk if missing
        except ImportError:
            pass

        style = ttk.Style()
        C = {"accent": "#f2a33c", "accent_hi": "#ffc46b", "accent_ink": "#241a06",
             "danger": "#ff6b6b", "danger_hi": "#ff8a8a", "danger_ink": "#2a0d0d",
             "green": "#5fd39a", "muted": "#98a0ad", "dim": "#6c7482", "text": "#e9ebef"}
        self.C = C
        # amber primary button / danger button while recording (built on sv-ttk's Accent.TButton)
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
        self.lbl_tagline = ttk.Label(top, font=("Segoe UI", 9), foreground=C["dim"])
        self.lbl_tagline.pack(side="left", padx=(12, 0), pady=(3, 0))
        self.lang_btn = ttk.Button(top, width=4, command=self.toggle_lang)
        self.lang_btn.pack(side="right")

        body = ttk.Frame(r)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        body.columnconfigure(0, weight=1)

        # ---- 1 · record area ----
        self.lbl_region_title = ttk.Label(body, style="Section.TLabel")
        self.lbl_region_title.grid(row=0, column=0, sticky="w", **pad)
        self.region_lbl = tk.Label(body, font=("Consolas", 10), fg=C["muted"],
                                   bg="#20242c", anchor="w", padx=10, pady=6, relief="flat")
        self.region_lbl.grid(row=1, column=0, sticky="ew", **box)
        self.pick_btn = ttk.Button(body, command=self.pick)
        self.pick_btn.grid(row=1, column=1, sticky="e", **box)

        # ---- 2 · mic ----
        self.lbl_mic_title = ttk.Label(body, style="Section.TLabel")
        self.lbl_mic_title.grid(row=2, column=0, sticky="w", **pad)
        self.mics = list_audio_devices(self.ff)
        self.mic_none = t("mic_none")
        self.mic_var = tk.StringVar(value=self.mics[0] if self.mics else self.mic_none)
        microw = ttk.Frame(body)
        microw.grid(row=3, column=0, columnspan=2, sticky="ew", **box)
        cb = ttk.Combobox(microw, textvariable=self.mic_var, values=self.mics,
                          state="readonly")
        cb.pack(side="left", fill="x", expand=True)
        cb.bind("<<ComboboxSelected>>", lambda e: self.start_meter())
        # VU bar drawn on a canvas — sv-ttk builds Progressbar from a fixed sprite image,
        # its fill color can't be changed, so it's hand-drawn here, recolored by level in tick()
        self.vu = tk.Canvas(microw, width=140, height=16, bg="#14161b",
                            highlightthickness=1, highlightbackground="#2a2f3a", bd=0)
        self.vu.pack(side="left", fill="x", expand=True, padx=(8, 0))

        # ---- actions ----
        self.rec_btn = ttk.Button(body, style="Accent.TButton", command=self.toggle)
        self.rec_btn.grid(row=4, column=0, columnspan=2, sticky="ew",
                          padx=14, pady=(12, 2))
        self.an_btn = ttk.Button(body, command=self.analyze, state="disabled")
        self.an_btn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=14, pady=2)
        self.sess_btn = ttk.Button(body, command=self.open_review)
        self.sess_btn.grid(row=6, column=0, columnspan=2, sticky="ew",
                           padx=14, pady=(2, 4))

        ttk.Separator(body).grid(row=7, column=0, columnspan=2, sticky="ew",
                                 padx=10, pady=(6, 2))

        # ---- status + recording time ----
        statrow = ttk.Frame(body)
        statrow.grid(row=8, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 4))
        self.status_var = tk.StringVar(value="")
        self.status_color = C["muted"]
        self.status_lbl = ttk.Label(statrow, textvariable=self.status_var,
                                    style="Muted.TLabel", wraplength=520)
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.elapsed_var = tk.StringVar(value="—:—")
        ttk.Label(statrow, textvariable=self.elapsed_var,
                  style="Mono.TLabel").pack(side="right")

        self.render_lang()

        if self.mics:
            self.start_meter()
        else:
            self.status(t("no_mic_warn"), color=C["accent"])
        self.tick()
        r.protocol("WM_DELETE_WINDOW", self.quit)

    # -- helpers --
    def status(self, msg, color=None):
        self.status_color = color or "#98a0ad"
        self.root.after(0, self.status_var.set, msg)
        self.root.after(0, lambda: self.status_lbl.configure(foreground=self.status_color))

    def render_lang(self):
        """Refresh every widget's text for the current language."""
        self.lang_btn.config(text=t("lang_btn"))
        self.lbl_tagline.config(text=t("tagline"))
        self.lbl_region_title.config(text=t("region_title"))
        self.lbl_mic_title.config(text=t("mic_title"))
        self.pick_btn.config(text=t("pick_btn"))
        self.rec_btn.config(text=t("rec_stop" if self.stop_evt else "rec_start"))
        self.an_btn.config(text=t("analyze"))
        self.sess_btn.config(text=t("sessions"))
        if not self.hwnd:
            self.region_lbl.config(text=t("region_none"))
        if self.mic_var.get() == self.mic_none:  # placeholder currently shown -> switch language
            self.mic_none = t("mic_none")
            self.mic_var.set(self.mic_none)
        self.status(t("ready"))

    def toggle_lang(self):
        global lang
        lang = "vi" if lang == "en" else "en"
        self.render_lang()

    def tick(self):
        lvl = min(100, self.meter.level * 600)
        w = self.vu.winfo_width() - 2
        col = (self.C["danger"] if lvl >= 85 else
               self.C["accent"] if lvl >= 55 else self.C["green"])
        self.vu.delete("all")
        self.vu.create_rectangle(1, 1, max(2, 1 + int((w - 2) * lvl / 100)), 15,
                                 fill=col, width=0)
        if self.stop_evt:  # recording -> count elapsed time
            el = int(time.monotonic() - self.rec_start)
            self.elapsed_var.set(f"{el // 60:02d}:{el % 60:02d}")
        self.root.after(50, self.tick)

    def start_meter(self):
        mic = self.mic_var.get()
        if mic and mic != self.mic_none:
            ok = self.meter.start(mic)
            self.status(t("mic_ok") if ok else t("mic_fail"))

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
                self.region_lbl.config(text=t("full_screen"), fg="#98a0ad")

        RegionPicker(self.root, done)

    # -- record --
    def toggle(self):
        if self.stop_evt:  # recording -> stop
            self.stop_evt.set()
            self.stop_evt = None
            self.rec_btn.config(text=t("rec_start"), style="Accent.TButton")
            self.elapsed_var.set("—:—")
            self.an_btn.config(state="normal")
            return
        self.outdir = SESSIONS / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.outdir.mkdir(parents=True, exist_ok=True)
        mic = self.mic_var.get()
        audio = mic if mic and mic != self.mic_none else None
        self.meter.stop()  # release the mic for ffmpeg
        self.stop_evt = threading.Event()
        self.rec_start = time.monotonic()
        threading.Thread(target=record_session,
                         args=(self.ff, self.hwnd, self.screen_rect, audio,
                               self.outdir, self.stop_evt, self.status),
                         daemon=True).start()
        self.rec_btn.config(text=t("rec_stop"), style="Danger.TButton")
        self.an_btn.config(state="disabled")

    # -- review server (server.py runs in the background inside the app) --
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
            self.status(t("analyzing"), color="#6ea8ff")
            try:
                data = pipeline.analyze_session(outdir)
            except Exception as e:
                self.status(t("analyze_err", err=str(e)[-300:]), color="#ff6b6b")
                self.root.after(0, self.an_btn.config, {"state": "normal"})
                return
            self.root.after(0, self.open_review, outdir.name)
            self.status(t("done", n=len(data["bugs"])), color="#5fd39a")
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
