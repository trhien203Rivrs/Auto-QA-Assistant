# region-capture: hover any UI element (window pane, browser frame, chat box...)
# -> red border highlights it -> F8 screenshot / F9 record toggle / Esc quit.
# UIA (ElementFromPoint) finds the element rect, ffmpeg gdigrab does the capture.
import ctypes
import datetime
import glob
import os
import shutil
import subprocess
import sys
import tkinter as tk

import uiautomation as auto

# --- DPI aware, otherwise coords are wrong on scaled displays ---
ctypes.windll.shcore.SetProcessDpiAwareness(2)

VK = {"F8": 0x77, "F9": 0x78, "ESC": 0x1B}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
os.makedirs(OUT_DIR, exist_ok=True)


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    hits = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"))
    if hits:
        return hits[0]
    sys.exit("ffmpeg not found. Install: winget install Gyan.FFmpeg")


FFMPEG = find_ffmpeg()


def key_down(vk):
    return ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True, "-transparentcolor", "black")
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.root.update_idletasks()
        self._make_clickthrough()
        self.rect = None          # current highlighted rect (x, y, w, h)
        self.rec_proc = None      # ffmpeg process while recording
        self.prev_keys = {k: False for k in VK}
        self.status("hover + F8=shot  F9=rec  Esc=quit")

    def _make_clickthrough(self):
        # WS_EX_TRANSPARENT so ElementFromPoint sees through our overlay
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        GWL_EXSTYLE = -20
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | 0x80000 | 0x20)

    def status(self, msg):
        print(msg, flush=True)

    def poll(self):
        for name, vk in VK.items():
            down = bool(key_down(vk))
            if down and not self.prev_keys[name]:
                getattr(self, "on_" + name.lower())()
            self.prev_keys[name] = down

        if self.rec_proc is None:  # follow cursor only when not recording
            self.track_cursor()
        self.root.after(60, self.poll)

    def track_cursor(self):
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        try:
            ctrl = auto.ControlFromPoint(pt.x, pt.y)
            r = ctrl.BoundingRectangle
            self.rect = (r.left, r.top, r.width(), r.height())
        except Exception:
            return
        x, y, w, h = self.rect
        if w <= 0 or h <= 0:
            return
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.canvas.delete("all")
        self.canvas.create_rectangle(1, 1, w - 2, h - 2, outline="red", width=3)

    def on_f8(self):
        if not self.rect or self.rec_proc:
            return
        x, y, w, h = self.rect
        out = os.path.join(OUT_DIR, f"shot_{stamp()}.png")
        self.root.withdraw()          # hide red border from the shot
        self.root.update()
        subprocess.run([FFMPEG, "-y", "-f", "gdigrab", "-offset_x", str(x),
                        "-offset_y", str(y), "-video_size", f"{w}x{h}",
                        "-i", "desktop", "-frames:v", "1", out],
                       capture_output=True)
        self.root.deiconify()
        self.status(f"saved {out}")

    def on_f9(self):
        if self.rec_proc:  # stop
            self.rec_proc.stdin.write(b"q")
            self.rec_proc.stdin.flush()
            self.rec_proc.wait(timeout=10)
            self.rec_proc = None
            self.root.deiconify()
            self.status("recording stopped")
            return
        if not self.rect:
            return
        x, y, w, h = self.rect
        w, h = w - w % 2, h - h % 2   # libx264 needs even dims
        out = os.path.join(OUT_DIR, f"rec_{stamp()}.mp4")
        self.root.withdraw()
        self.root.update()
        self.rec_proc = subprocess.Popen(
            [FFMPEG, "-y", "-f", "gdigrab", "-framerate", "30",
             "-offset_x", str(x), "-offset_y", str(y),
             "-video_size", f"{w}x{h}", "-i", "desktop",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.status(f"recording -> {out}  (F9 to stop)")

    def on_esc(self):
        if self.rec_proc:
            self.on_f9()
        self.root.destroy()

    def run(self):
        self.root.after(60, self.poll)
        self.root.mainloop()


if __name__ == "__main__":
    import ctypes.wintypes  # noqa: E402 (used in track_cursor)
    App().run()
