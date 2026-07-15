"""Tạo trang review: video player + danh sách bug, click là video nhảy tới timestamp.

Dùng:  python make_review.py <video>       (cần <video>.bugs.json cạnh nó)
Ra:    <video>.review.html  -> double-click mở bằng trình duyệt.

Data bug nhúng thẳng vào HTML nên mở file:// được, không cần server.
"""
import sys
import json
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # console Windows in được tiếng Việt


def secs(t: str) -> int:
    s = 0
    for p in t.split(":"):
        s = s * 60 + int(p)
    return s


TEMPLATE = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bug Review — __VIDEO__</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 system-ui,Segoe UI,sans-serif;
         background:#0f1115; color:#e6e8eb; display:flex; height:100vh; }
  #left { flex:1 1 65%; display:flex; flex-direction:column; padding:16px; min-width:0; }
  video { width:100%; background:#000; border-radius:8px; }
  #bar { position:relative; height:10px; margin:10px 2px 0;
         background:#2a2e37; border-radius:5px; cursor:pointer; }
  #bar .mk { position:absolute; top:-3px; min-width:4px; height:16px;
             background:#ff5a5f; border-radius:2px; cursor:pointer; opacity:.85; }
  #bar .mk:hover { opacity:1; box-shadow:0 0 6px #ff5a5f; }
  #right { flex:1 1 35%; max-width:440px; overflow-y:auto; padding:16px;
           border-left:1px solid #23272f; }
  h2 { margin:0 0 12px; font-size:16px; color:#9aa4b2; }
  .bug { padding:10px 12px; margin-bottom:8px; background:#171a21;
         border:1px solid #23272f; border-radius:8px; cursor:pointer; }
  .bug:hover { border-color:#3a4150; }
  .bug.active { border-color:#ff5a5f; background:#1e1518; }
  .bug .t { color:#ff8a8d; font-variant-numeric:tabular-nums; font-size:13px; }
  .bug .n { font-weight:600; margin:2px 0 6px; }
  .bug dl { margin:6px 0 0; font-size:13px; color:#c2c8d0; }
  .bug dt { color:#7f8794; margin-top:6px; }
  .bug dd { margin:0; }
</style></head><body>
<div id="left">
  <video id="v" controls src="__VIDEO__"></video>
  <div id="bar"></div>
</div>
<div id="right">
  <h2 id="hd">Bugs</h2>
  <div id="list"></div>
</div>
<script>
const BUGS = __BUGS__;
const v = document.getElementById('v'), bar = document.getElementById('bar');
const list = document.getElementById('list');
document.getElementById('hd').textContent = `Bugs (${BUGS.length})`;

function fmt(s){ s=Math.floor(s); const m=Math.floor(s/60);
  return `${String(m).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`; }

const cards = BUGS.map((b,i) => {
  const el = document.createElement('div');
  el.className = 'bug';
  const rng = b.start_time + (b.end_time ? ' – ' + b.end_time : '');
  el.innerHTML = `<div class="t">${rng}</div><div class="n">${i+1}. ${b.name}</div>
    <dl><dd>${b.description}</dd>
    <dt>Actual</dt><dd>${b.actual_result}</dd>
    <dt>Expected</dt><dd>${b.expected_result}</dd></dl>`;
  el.onclick = () => { v.currentTime = b._start; v.play(); };
  list.appendChild(el);
  return el;
});

// marker trên timeline sau khi biết tổng thời lượng
v.addEventListener('loadedmetadata', () => {
  BUGS.forEach(b => {
    const mk = document.createElement('div');
    mk.className = 'mk';
    const end = b._end ?? b._start;              // không có end -> chấm điểm
    mk.style.left = (b._start / v.duration * 100) + '%';
    mk.style.width = ((end - b._start) / v.duration * 100) + '%';
    mk.title = b.name;
    mk.onclick = e => { e.stopPropagation();     // đừng để bar seek đè lên
                        v.currentTime = b._start; v.play(); };
    bar.appendChild(mk);
  });
});
// click thanh bar = seek
bar.addEventListener('click', e => {
  if (v.duration) v.currentTime = (e.offsetX / bar.clientWidth) * v.duration;
});
// highlight bug đang phát
v.addEventListener('timeupdate', () => {
  const t = v.currentTime;
  BUGS.forEach((b,i) => {
    const on = t >= b._start && t <= (b._end ?? b._start + 5);
    cards[i].classList.toggle('active', on);
  });
});
</script></body></html>
"""


def main():
    if len(sys.argv) != 2:
        sys.exit("Dùng: python make_review.py <video>")
    p = Path(sys.argv[1])
    bugs = json.loads(
        p.with_suffix(p.suffix + ".bugs.json").read_text(encoding="utf-8")
    )["bugs"]
    for b in bugs:
        b["_start"] = secs(b["start_time"])
        b["_end"] = secs(b["end_time"]) if b.get("end_time") else None

    out_html = (TEMPLATE
                .replace("__VIDEO__", p.name)
                .replace("__BUGS__", json.dumps(bugs, ensure_ascii=False)))
    out = p.with_suffix(p.suffix + ".review.html")
    out.write_text(out_html, encoding="utf-8")
    print("Mở trang này bằng trình duyệt:")
    print(" ", out.resolve())


if __name__ == "__main__":
    main()
