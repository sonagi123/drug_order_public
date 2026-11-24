// 공통 도우미
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmt(n, d) {
  if (n == null || isNaN(n)) return '—';
  // 자릿수를 안 주면 정수는 그대로, 반알 같은 소수는 한 자리까지 (예: 1012.5)
  if (d === undefined) return Number(n).toLocaleString('ko-KR', { maximumFractionDigits: 1 });
  return Number(n).toLocaleString('ko-KR', { maximumFractionDigits: d, minimumFractionDigits: d });
}
function won(n) { return n == null ? '—' : Math.round(n).toLocaleString('ko-KR') + '원'; }
async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  let j = {};
  try { j = await r.json(); } catch (e) { j = { ok: false, error: '서버 응답 오류' }; }
  if (!j.ok) toast(j.error || '저장하지 못했어요.');
  return j;
}
function toast(t) {
  const el = document.getElementById('toast');
  el.textContent = t; el.style.display = 'block';
  clearTimeout(window.__t); window.__t = setTimeout(() => el.style.display = 'none', 2600);
}
// 내부망(http)에서는 navigator.clipboard 가 막혀 있어서 예전 방식도 같이 씀
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => true, () => fallbackCopy(text));
  }
  return Promise.resolve(fallbackCopy(text));
}
function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.top = '-1000px';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}
const CHECK = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const XMARK = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
function daysBadge(d) {
  if (d == null) return '';
  const cls = d < 13 ? 'low' : d < 17 ? 'mid' : 'ok';
  return `<span class="badge ${cls}">${Math.round(d)}일분</span>`;
}
// 체크하는 사람 기억(태블릿)
function savedStaff() { try { return localStorage.getItem('staff') || ''; } catch (e) { return ''; } }
function saveStaff(n) { try { localStorage.setItem('staff', n); } catch (e) {} }

// 폴더 고르기 (서버가 돌아가는 PC의 폴더)
async function pickFolder(inputId) {
  const inp = document.getElementById(inputId);
  let cur = inp.value.trim();
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:20;display:flex;align-items:center;justify-content:center;padding:16px';
  box.innerHTML = `<div class="card" style="width:min(640px,100%);max-height:85vh;display:flex;flex-direction:column;overflow:hidden">
    <div class="row between pad" style="border-bottom:1px solid var(--line)"><h2>폴더 고르기</h2><button data-x style="border:0" aria-label="닫기">${XMARK}</button></div>
    <div class="pad" style="padding-bottom:8px"><div class="row" data-roots style="gap:6px"></div>
      <div data-path style="margin-top:10px;font-family:monospace;font-size:13px;word-break:break-all;color:var(--g2)"></div></div>
    <div data-list style="overflow-y:auto;flex:1;border-top:1px solid var(--line2)"></div>
    <div class="row between pad" style="border-top:1px solid var(--line)">
      <button data-new>+ 새 폴더</button>
      <div class="row"><button data-x>취소</button><button class="primary" data-ok>이 폴더 선택</button></div></div></div>`;
  document.body.appendChild(box);
  const q = s => box.querySelector(s);
  async function go(path) {
    const r = await fetch('/api/dirs?path=' + encodeURIComponent(path || '')).then(x => x.json());
    if (!r.ok) { toast(r.error); if (path) return go(''); return; }
    cur = r.path;
    q('[data-roots]').innerHTML = r.roots.map(x => `<button class="chip" style="font-size:13px" data-go="${esc(x.path)}">${esc(x.label)}</button>`).join('');
    q('[data-path]').textContent = cur ? '지금 위치: ' + cur : '위에서 시작할 곳을 고르세요.';
    q('[data-ok]').disabled = !cur; q('[data-new]').disabled = !cur;
    q('[data-list]').innerHTML = (r.parent ? `<button data-go="${esc(r.parent)}" style="width:100%;justify-content:flex-start;border:0;border-bottom:1px solid var(--line2);border-radius:0">⬑ 위 폴더로</button>` : '') +
      r.dirs.map(d => `<button data-go="${esc((cur.endsWith('\\') || cur.endsWith('/') ? cur : cur + (cur.includes('\\') ? '\\' : '/')) + d)}" style="width:100%;justify-content:flex-start;border:0;border-bottom:1px solid var(--line2);border-radius:0">📁 ${esc(d)}</button>`).join('') +
      (cur && !r.dirs.length ? '<p class="muted pad" style="margin:0">하위 폴더가 없어요. 이 폴더를 고르거나 새 폴더를 만드세요.</p>' : '');
    box.querySelectorAll('[data-go]').forEach(b => b.onclick = () => go(b.dataset.go));
  }
  box.querySelectorAll('[data-x]').forEach(b => b.onclick = () => box.remove());
  q('[data-ok]').onclick = () => { inp.value = cur; inp.dispatchEvent(new Event('change')); box.remove(); toast('폴더를 골랐어요. [저장]을 눌러야 적용돼요.'); };
  q('[data-new]').onclick = async () => {
    const name = prompt('새 폴더 이름'); if (!name) return;
    const r = await post('/api/dirs/mkdir', { path: cur, name }); if (r.ok) go(r.path);
  };
  go(cur);
}

// 정렬: 빈 값은 항상 아래, 글자는 가나다순
function sortBy_(arr, get, dir) {
  return arr.slice().sort((a, b) => {
    const x = get(a), y = get(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (typeof x === 'string' ? x.localeCompare(y, 'ko') : x - y) * dir;
  });
}
// 입고 체크하는 사람 고르기 칩
function peopleHtml(staff, who, onPick, label) {
  return `<span class="muted">${label || '체크하는 사람'}</span>` + (staff.length
    ? staff.map(n => `<button class="chip ${who === n ? 'on' : ''}" onclick="${onPick}(${esc(JSON.stringify(n))})">${esc(n)}</button>`).join('')
    : '<a class="btn" href="/settings#staff">직원 이름 등록하기</a>');
}
