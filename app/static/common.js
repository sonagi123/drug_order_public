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

// ---- 바코드 스캐너 (키보드처럼 글자를 쳐 주는 방식) ----
// 아주 빠르게 들어온 글자 + Enter 이고 의약품 바코드(01 + 14자리)로 시작하면 window.onBarcode(raw) 호출.
// 사람이 이 속도로 칠 수는 없어서 손으로 치는 입력과 헷갈리지 않음.
(function () {
  let buf = '', last = 0, start = 0;
  document.addEventListener('keydown', e => {
    if (!window.onBarcode) return;
    const now = performance.now();
    if (now - last > 80) { buf = ''; start = now; }
    last = now;
    if (e.key === 'Enter') {
      const raw = buf; buf = '';
      if (raw.length >= 16 && /^(\][A-Za-z]\d)?\(?01\)?\d{14}/.test(raw) && (now - start) / raw.length < 40) {
        e.preventDefault(); e.stopPropagation();
        const el = document.activeElement, plain = raw.replace(/\x1d/g, '');
        if (el && typeof el.value === 'string' && el.value.endsWith(plain)) el.value = el.value.slice(0, -plain.length);
        window.onBarcode(raw);
      }
      return;
    }
    let ch = e.key;
    if (e.ctrlKey && (e.key === ']' || e.code === 'BracketRight')) ch = '\x1d';  // 구분 글자(GS)
    else if (ch === 'Process' && /^Key[A-Z]$/.test(e.code)) ch = e.code.slice(3);  // 한글 입력 상태여도 영문으로
    else if (ch === 'Process' && /^Digit\d$/.test(e.code)) ch = e.code.slice(5);
    if (ch && ch.length === 1) buf += ch;
  }, true);
})();
const fmtExp = s => s ? s.replace(/-/g, '.') : '기한 모름';
// 찍은 결과를 1초쯤 보여주는 작은 팝업. undo가 있으면 [취소] 버튼과 함께 조금 더 오래
function scanPop(html, kind, undo) {
  let el = document.getElementById('scanPop');
  if (!el) { el = document.createElement('div'); el.id = 'scanPop'; el.setAttribute('role', 'status'); document.body.appendChild(el); }
  el.className = 'scan-pop ' + (kind || 'ok');
  el.innerHTML = `<span>${html}</span>${undo ? '<button type="button">취소</button>' : ''}`;
  if (undo) el.querySelector('button').onclick = () => { el.style.display = 'none'; undo(); };
  el.style.display = 'flex';
  clearTimeout(window.__sp); window.__sp = setTimeout(() => el.style.display = 'none', kind === 'err' ? 3500 : undo ? 2500 : 1300);
}
// 찍은 바코드의 약 정하기: 연결된 바코드면 바로, 처음이면 "이 약이 맞나요?", 못 찾으면 검색해서 고르기
function scanPick(p) {
  if (p.drug && p.linked) return Promise.resolve(p.drug);
  return new Promise(resolve => {
    const box = document.createElement('div');
    box.className = 'sheet-bg'; box.style.zIndex = 60;
    const info = `<div class="scan-info">사용기한 <b>${fmtExp(p.exp)}</b>${p.lot ? ' · 제조번호 ' + esc(p.lot) : ''}<br><span class="muted small">표준코드 ${esc(p.gtin.replace(/^0/, ''))}</span></div>`;
    const done = async d => {
      if (d) { const j = await post('/api/barcode/link', { gtin: p.gtin, code: d.code }); if (!j.ok) return; }
      box.remove(); resolve(d);
    };
    const search = () => {
      box.innerHTML = `<div class="sheet card pad" style="max-width:520px;display:flex;flex-direction:column;gap:12px" role="dialog" aria-label="어떤 약인가요">
        <h2>어떤 약인가요?</h2>${info}
        <input type="search" data-q placeholder="약 이름으로 찾기" aria-label="약 이름으로 찾기">
        <div data-res class="scan-res"><span class="muted small">이름 일부를 적으면 목록이 나와요. 없는 약은 약품 관리에서 먼저 추가해 주세요.</span></div>
        <button data-x>닫기 (이 상자는 건너뛰기)</button></div>`;
      const q = box.querySelector('[data-q]'), res = box.querySelector('[data-res]');
      let items = [];
      q.oninput = async () => {
        const r = await fetch('/api/barcode/search?q=' + encodeURIComponent(q.value.trim())).then(r => r.json());
        items = r.items || [];
        res.innerHTML = items.map((d, k) => `<button data-k="${k}"><b>${esc(d.name)}</b><span class="muted small">${esc(d.label)}</span></button>`).join('') || '<span class="muted small">찾는 약이 없어요.</span>';
        res.querySelectorAll('[data-k]').forEach(b => b.onclick = () => done(items[+b.dataset.k]));
      };
      box.querySelector('[data-x]').onclick = () => { box.remove(); resolve(null); };
      setTimeout(() => q.focus(), 50);
    };
    if (p.drug) {
      box.innerHTML = `<div class="sheet card pad" style="max-width:520px;display:flex;flex-direction:column;gap:12px" role="dialog" aria-label="이 약이 맞나요">
        <span class="small" style="color:var(--g);font-weight:700">처음 찍는 바코드</span><h2>이 약이 맞나요?</h2>
        <div class="scan-drug"><b>${esc(p.drug.name)}</b><span class="muted small">${esc(p.drug.label)}</span></div>${info}
        <button class="primary big" data-y>맞아요</button><button data-n>다른 약이에요</button>
        <p class="muted small" style="margin:0">한 번 맞다고 하면 다음부터 이 바코드는 묻지 않아요.</p></div>`;
      box.querySelector('[data-y]').onclick = () => done(p.drug);
      box.querySelector('[data-n]').onclick = search;
      document.body.appendChild(box); box.querySelector('[data-y]').focus();
    } else { document.body.appendChild(box); search(); }
  });
}
