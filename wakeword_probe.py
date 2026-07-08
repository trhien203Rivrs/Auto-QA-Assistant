"""Phase 0 — kiểm tra wake-word có bắt được giọng bạn không.

Cài:   pip install vosk sounddevice
Model: tải model nhỏ, giải nén cạnh file này thành thư mục 'model':
       - English:    https://alphacephei.com/vosk/models  -> vosk-model-small-en-us-0.15
       - (tùy chọn)  vosk-model-small-... cho ngôn ngữ khác
Chạy:  python wakeword_probe.py
       -> nói "bug" (và cả nói chuyện linh tinh) vài lần.
          Dòng có [BUG] = bắt được. Ctrl+C để thoát.

Đổi từ khóa:  set WAKE_WORD=bug   (hoặc "bao bug", cách nhau bởi dấu cách)
"""
import os
import sys
import json
import queue

import sounddevice as sd
from vosk import Model, KaldiRecognizer

WAKE = os.environ.get("WAKE_WORD", "bug").lower().split()
MODEL_DIR = "model"
RATE = 16000

q: queue.Queue = queue.Queue()


def cb(indata, frames, time, status):
    if status:
        print(status, file=sys.stderr)
    q.put(bytes(indata))


def hit(text: str) -> bool:
    """True nếu câu vừa nghe chứa (chuỗi) từ khóa."""
    words = text.split()
    return all(w in words for w in WAKE)


def main():
    if not os.path.isdir(MODEL_DIR):
        sys.exit(f"Chưa có thư mục '{MODEL_DIR}/'. Tải model Vosk nhỏ, "
                 f"giải nén thành '{MODEL_DIR}'. Xem hướng dẫn ở đầu file.")
    rec = KaldiRecognizer(Model(MODEL_DIR), RATE)
    print(f"Nghe... nói '{' '.join(WAKE)}' để test. Ctrl+C thoát.\n")
    with sd.RawInputStream(samplerate=RATE, blocksize=8000, dtype="int16",
                           channels=1, callback=cb):
        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "")
                if text:
                    print(("[BUG] " if hit(text) else "      ") + text)


# ponytail: parse-only self-check; không test được mic/Vosk offline ở đây.
def _check():
    global WAKE
    WAKE = ["bug"]
    assert hit("there is a bug here")
    assert not hit("everything works fine")
    WAKE = ["bao", "bug"]
    assert hit("bao bug now")
    assert not hit("bug only")
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        _check()
    else:
        main()
