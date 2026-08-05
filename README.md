# Auto-QA

Quay màn hình game (Windows) → Gemini tìm bug → review trên web → đẩy lên Jira.

## Cài đặt

```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
winget install Gyan.FFmpeg          # cần ffmpeg trong PATH
```

Tạo `.env`:

```ini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
AI_FPS=1                # fps bản nén gửi AI
AI_HEIGHT=480

# Jira (tùy chọn — thiếu thì chạy MOCK, ghi ra pushed_issues.json)
JIRA_BASE_URL=https://xxx.atlassian.net
JIRA_EMAIL=...
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=QA
JIRA_LABEL=auto-qa
```

## Cách chạy

| Việc | Lệnh |
|---|---|
| App chính (quay → phân tích → review) | `python qa_app.py` |
| Phân tích video có sẵn | `python analyze_file.py video.mp4` |
| Chỉ mở web review sessions | `python server.py` → http://127.0.0.1:8756 |
| Chỉ quay | `python record.py out.mp4` (Ctrl+C để dừng) |

Luồng trong `qa_app.py`: **Chọn vùng quay** (rê chuột, `F8` chọn / `Esc` hủy) → chọn mic → **Bắt đầu** → chơi và nói bug ra miệng → **Dừng** → **Phân tích** → trang review tự mở.

## Tính năng chính

- **Quay bằng WGC** — bắt được nội dung GPU (Roblox), bám theo cửa sổ khi di chuyển, quay được cả khi bị che. Crop đúng vùng UI đã chọn.
- **Ghi mic kèm VU meter** để biết audio có vào không; lời thoại mô tả bug được AI đọc cùng hình.
- **Bản nén riêng cho AI** (`session.ai.mp4`, 1fps/480p/mono) — gửi Gemini nhanh và rẻ, review vẫn xem bản gốc.
- **Trang review** có timeline, click bug là video nhảy tới đúng giây. Bản offline (`make_review.py`) dùng chung CSS với web nên trông giống hệt.
- **Push Jira** từng bug một, chọn project ngay trong UI. Thiếu credential → MOCK mode, không gửi thật.

## Cấu trúc

```
qa_app.py       App tkinter, điều phối cả pipeline
record.py       Quay WGC + mic → mp4
pipeline.py     Nén bản AI + gọi Gemini → bugs.json
bug_report.py   Gemini: video → danh sách bug (schema pydantic)
server.py       FastAPI: list/analyze/push sessions
ui.html         Web review
make_review.py  Xuất trang review offline
jira_push.py    Jira Cloud REST v3
sessions/<time>/session.mp4, session.ai.mp4, bugs.json
```
