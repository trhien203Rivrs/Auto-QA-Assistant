"""Tạo trang review offline: video player + danh sách bug, click là video nhảy.

Dùng:  python make_review.py <video>       (cần <video>.bugs.json cạnh nó)
Ra:    <video>.review.html  -> double-click mở bằng trình duyệt.

Style lấy thẳng từ ui.html (đoạn <style>) nên bản offline luôn khớp web —
không duplicate CSS ở 2 nơi. Data bug nhúng thẳng vào HTML nên mở file:// được.
"""
import re
import sys
import json
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # console Windows in được tiếng Việt


def extract_css() -> str:
    """Lấy khối <style> của ui.html để dùng chung thiết kế."""
    css = (Path(__file__).resolve().parent / "ui.html").read_text(encoding="utf-8")
    m = re.search(r"<style>(.*?)</style>", css, re.S)
    return m.group(1) if m else ""


TEMPLATE = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Auto-QA · __VIDEO__</title>
<style>
__CSS__
</style></head><body>
<div id="main">
  <div class="stage">
    <video id="v" controls preload="metadata" src="__VIDEO__"></video>
    <div class="nowbar" id="nowbar"><span class="now-dot"></span><span id="now-txt">—:—</span><button id="lang-btn" class="lang-btn"></button></div>
    <div class="tlwrap"><div id="bar"><div class="track"></div></div><div id="tip"></div></div>
  </div>
  <div class="rail">
    <div class="rail-head">
      <h2>Bugs <span class="n" id="bug-count"></span></h2>
      <div class="chips">
        <span class="chip open" id="chip-open"></span>
        <span class="chip pushed" id="chip-pushed"></span>
      </div>
      <div class="timing" id="timing"></div>
    </div>
    <div id="list"></div>
  </div>
</div>
<script>
const L = {
  vi: {
    openChip: 'mở', pushedChip: 'đã push', openBadge: 'mở', pushBadge: 'push', langBtn: 'EN',
  },
  en: {
    openChip: 'open', pushedChip: 'pushed', openBadge: 'open', pushBadge: 'pushed', langBtn: 'VI',
  },
};
let lang = 'vi';
try { lang = localStorage.getItem('aq.lang') || 'vi'; } catch (e) {}
const t = k => (L[lang] && L[lang][k]) ?? L.vi[k] ?? k;
const setLang = l => { lang = l; try { localStorage.setItem('aq.lang', l); } catch (e) {} };
const timingTxt = d => lang === 'vi'
  ? `nén ${d.compress_s}s · Gemini ${d.gemini_s}s · bản AI ${d.ai_size_mb} MB`
  : `compress ${d.compress_s}s · Gemini ${d.gemini_s}s · AI cut ${d.ai_size_mb} MB`;
const BUGS = __BUGS__;
const timing = __TIMING__;
const $ = s => document.querySelector(s);
const secs = t => { let s = 0; for (const p of t.split(':')) s = s * 60 + +p; return s; };
const v = $('#v'), bar = $('#bar'), list = $('#list'), now = $('#nowbar'), tip = $('#tip');
const pushedN = BUGS.filter(b => b.jira_key).length;

const bugs = BUGS.map((b, i) => ({ ...b, i,
  _start: secs(b.start_time), _end: b.end_time ? secs(b.end_time) : null }));
let cards = [], markers = [], lastActive = -1;
const rngOf = b => b.start_time + (b.end_time ? ' – ' + b.end_time : '');

function buildList() {
  list.innerHTML = '';
  cards = [];
  bugs.forEach((b, i) => {
    const el = document.createElement('article');
    el.className = 'bug';
    const state = b.jira_key
      ? `<span class="bug-state"><span class="badge b-pushed">✓ ${t('pushBadge')}</span></span>`
      : `<span class="bug-state"><span class="badge b-open">${t('openBadge')}</span></span>`;
    el.innerHTML = `<div class="bug-top">
        <span class="bug-id">#${String(i + 1).padStart(2, '0')}</span>
        <span class="bug-time">${rngOf(b)}</span>${state}</div>
      <h3 class="n">${b.name}</h3>
      <p class="desc">${b.description}</p>
      <dl>
        <div><dt>Actual</dt><dd>${b.actual_result}</dd></div>
        <div><dt>Expected</dt><dd>${b.expected_result}</dd></div>
      </dl>`;
    el.onclick = () => { v.currentTime = b._start; v.play(); };
    list.appendChild(el);
    cards.push(el);
  });
  $('#bug-count').textContent = bugs.length;
  $('#chip-open').textContent = `${bugs.length - pushedN} ${t('openChip')}`;
  $('#chip-pushed').textContent = `${pushedN} ${t('pushedChip')}`;
  $('#timing').textContent = timing ? timingTxt(timing) : '';
}
buildList();

v.addEventListener('loadedmetadata', () => {
  const dur = v.duration || 1;
  bugs.forEach(b => {
    const mk = document.createElement('div');
    mk.className = 'mk' + (b.jira_key ? ' pushed' : '');
    mk.style.left = (b._start / dur * 100) + '%';
    mk.style.width = Math.max(0.5, ((b._end ?? b._start + 4) - b._start) / dur * 100) + '%';
    mk.onclick = e => { e.stopPropagation(); v.currentTime = b._start; v.play(); };
    mk.addEventListener('mouseenter', e => showTip(e, b));
    mk.addEventListener('mousemove', moveTip);
    mk.addEventListener('mouseleave', hideTip);
    bar.appendChild(mk);
    markers.push(mk);
  });
  const step = Math.max(30, Math.round(dur / 8 / 30) * 30);
  for (let t = 0; t < dur; t += step) {
    const tick = document.createElement('div');
    tick.className = 'tick';
    tick.style.left = (t / dur * 100) + '%';
    tick.textContent = `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
    bar.appendChild(tick);
  }
});

function showTip(e, b) {
  tip.innerHTML = `<span class="tt">#${String(b.i + 1).padStart(2, '0')}</span> ${b.name} · <span class="tt">${rngOf(b)}</span>`;
  tip.classList.add('show'); moveTip(e);
}
function moveTip(e) {
  const r = bar.getBoundingClientRect();
  tip.style.left = Math.min(Math.max(0, e.clientX - r.left - 40), r.width - 120) + 'px';
  tip.style.top = '-8px';
}
function hideTip() { tip.classList.remove('show'); }

bar.addEventListener('click', e => {
  if (e.target !== bar && !e.target.classList.contains('track')) return;
  const r = bar.getBoundingClientRect();
  if (v.duration) v.currentTime = (e.clientX - r.left) / r.width * v.duration;
});

const ph = document.createElement('div');
ph.className = 'ph'; bar.appendChild(ph);
v.addEventListener('timeupdate', () => {
  const t = v.currentTime;
  if (v.duration) ph.style.left = (t / v.duration * 100) + '%';
  let act = -1;
  for (const b of bugs) if (t >= b._start && t <= (b._end ?? b._start + 5)) { act = b.i; break; }
  const nowTxt = $('#now-txt');
  if (act >= 0) {
    now.classList.add('on');
    nowTxt.innerHTML = `bug <span id="now-name">#${String(act + 1).padStart(2, '0')}</span>
      <span style="color:var(--muted)">· ${bugs[act].start_time}${bugs[act].end_time ? '–' + bugs[act].end_time : ''} · ${bugs[act].name}</span>`;
    markers.forEach((m, i) => m.classList.toggle('active', i === act));
    if (act !== lastActive) { cards[act].scrollIntoView({ block: 'nearest', behavior: 'smooth' }); lastActive = act; }
  } else {
    now.classList.remove('on');
    nowTxt.textContent = '—:—';
    markers.forEach(m => m.classList.remove('active'));
  }
});

document.addEventListener('keydown', e => {
  const tag = (e.target.tagName || '').toLowerCase();
  if (['input','select','textarea','video'].includes(tag) || e.target.isContentEditable) return;
  if (e.code === 'Space') { e.preventDefault(); v.paused ? v.play() : v.pause(); }
});

$('#lang-btn').textContent = t('langBtn');
$('#lang-btn').onclick = () => { setLang(lang === 'vi' ? 'en' : 'vi'); $('#lang-btn').textContent = t('langBtn'); buildList(); };
</script></body></html>
"""


def main():
    if len(sys.argv) != 2:
        sys.exit("Dùng: python make_review.py <video>")
    p = Path(sys.argv[1])
    data = json.loads(
        p.with_suffix(p.suffix + ".bugs.json").read_text(encoding="utf-8"))
    bugs = data["bugs"]
    timing = data.get("timing")

    out_html = (TEMPLATE
                .replace("__CSS__", extract_css())
                .replace("__VIDEO__", p.name)
                .replace("__BUGS__", json.dumps(bugs, ensure_ascii=False))
                .replace("__TIMING__", json.dumps(timing) if timing else "null"))
    out = p.with_suffix(p.suffix + ".review.html")
    out.write_text(out_html, encoding="utf-8")
    print("Mở trang này bằng trình duyệt:")
    print(" ", out.resolve())


if __name__ == "__main__":
    main()
