# region-capture

Hover chuột lên bất kỳ element nào (window, pane, khung chat, frame trong browser...) — viền đỏ tự bao quanh element đó.

- **F8** — chụp vùng đang highlight → `captures/shot_*.png`
- **F9** — bắt đầu / dừng quay → `captures/rec_*.mp4`
- **Esc** — thoát

## Chạy

```
pip install uiautomation
python capture_tool.py
```

Cần ffmpeg (`winget install Gyan.FFmpeg`) — tool tự tìm trong PATH hoặc thư mục winget.

Lưu ý: độ chi tiết nhận diện bên trong trang web phụ thuộc accessibility tree của browser (Chrome/Edge OK với đa số iframe/khung lớn; muốn chính xác từng div thì cần browser extension).
