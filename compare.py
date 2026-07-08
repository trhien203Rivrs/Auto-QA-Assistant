"""So sánh chất lượng bug report giữa các model trên CHÍNH video của bạn.

Upload video 1 lần, chạy qua nhiều model, in: số lỗi, thời gian, token dùng,
và lưu report mỗi model ra <video>.<model>.md để bạn tự đọc & so.

Dùng:  python compare.py <video>
"""
import os
import sys
import time
from pathlib import Path

from google import genai
from dotenv import load_dotenv

from bug_report import upload_video, run_model, to_markdown

load_dotenv()

MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-pro-preview",
]


def main():
    if len(sys.argv) != 2:
        sys.exit("Dùng: python compare.py <video>")
    video = sys.argv[1]
    base = Path(video)

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    f = upload_video(client, video)  # upload 1 lần, dùng chung

    print(f"\n{'model':28} {'bugs':>5} {'giây':>6} {'in_tok':>9} {'out_tok':>8}")
    print("-" * 62)
    for m in MODELS:
        try:
            t0 = time.time()
            report, resp = run_model(client, f, m)
            dt = time.time() - t0
            u = resp.usage_metadata
            print(f"{m:28} {len(report.bugs):>5} {dt:>6.1f} "
                  f"{u.prompt_token_count:>9} {u.candidates_token_count:>8}")
            out = base.with_suffix(base.suffix + f".{m}.md")
            out.write_text(to_markdown(report), encoding="utf-8")
        except Exception as e:
            print(f"{m:28}  LỖI: {e}")

    print("\nĐọc & so nội dung ở các file .md vừa tạo để tự đánh giá chất lượng.")


if __name__ == "__main__":
    main()
