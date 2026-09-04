// 회의 콘솔 화면.
//
// **바깥으로 나가는 요청이 없다.** 서버로 가는 통신은 아래 req() 하나뿐이고,
// 경로가 "/"로 시작하지 않으면 던진다. 스킴·호스트를 붙인 주소를 만들 수 없으므로
// 회의 내용이 이 맥 밖으로 나갈 경로가 코드에 존재하지 않는다.
const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function req(path, body) {
  if (!path.startsWith('/')) throw new Error('같은 서버 경로만 부를 수 있습니다: ' + path);
  const headers = { 'X-Console-Token': TOKEN };
  let opt = { headers };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    opt = { method: 'POST', headers, body: JSON.stringify(body) };
  }
  const res = await fetch(path, opt);
  if (res.status === 401) { document.body.innerHTML = '<main><h1>토큰이 없습니다</h1>' +
    '<p>콘솔을 띄운 터미널에 찍힌 주소로 여세요.</p></main>'; throw new Error('401'); }
  return res.json();
}

const mmss = (s) => s == null ? '?' : `${Math.floor(s / 60)}분 ${String(s % 60).padStart(2, '0')}초`;
let STATE = null, CUR = null;

// ------------------------------------------------------------------ 제어판
function renderNow(s) {
  $('#clock').textContent = s.now;
  $('#waiting').textContent = `확인 필요 ${s.waiting}건`;
  const r = s.recording;
  if (r) {
    $('#now').innerHTML = `<div class="rec">🔴 녹음 중 · ${esc(r.title)}</div>
      <div>경과 ${mmss(r.elapsed)} · 남은 시간 ${r.remaining == null ? '(모름)' : mmss(r.remaining)}</div>
      <div class="meta">저장 위치 <code>${esc(r.rel)}</code></div>`;
  } else {
    const n = s.next_event;
    $('#now').innerHTML = `<div>녹음 중 아님</div>` + (n
      ? `<div>다음 예정: ${esc(n.when)} ${esc(n.title)} · ${n.record ? '<b>녹음 대상</b>' : '녹음 안 함' + (n.skip_reason ? ' (' + esc(n.skip_reason) + ')' : '')}</div>`
      : `<div class="meta">남은 예정 일정이 없습니다</div>`);
  }
  $('#btn-stop').disabled = !r;
  $('#btn-auto').textContent = s.autorecord ? '자동 녹음 끄기 (지금 켜짐)' : '자동 녹음 켜기 (지금 꺼짐)';
}

function renderToday(t) {
  if (t.error) { $('#today').innerHTML = `<div class="warn">캘린더 조회 실패: ${esc(t.error)}</div>`; return; }
  if (!t.events.length) { $('#today').innerHTML = '<div class="meta">오늘 일정이 없습니다</div>'; return; }
  $('#today').innerHTML = `<div class="meta">${t.events.length}건 · ${esc(t.fetched_at)} 조회</div><table>
    <tr><th>시각</th><th>일정</th><th>회의실</th><th>녹음</th><th>건너뛰는 사유</th></tr>` +
    t.events.map((e) => `<tr><td>${esc(e.when)}</td><td>${esc(e.title)}</td>
      <td class="meta">${esc(e.room || '')}</td>
      <td>${e.record ? '<span class="rec">녹음</span>' : '<span class="dim">안 함</span>'}</td>
      <td class="meta">${esc(e.skip_reason)}</td></tr>`).join('') + '</table>';
}

// 「확인 필요」만 따로 떼어 맨 위에 둔다 (스펙 8절 「검수 큐 표시 방식」).
// 나머지 단계는 아래 「처리 상태」 표로 간다. 이름이 「검수 큐」였을 때
// 확인 필요 0건인데 표에 6건이 떠 있어 "내가 검수할 것"으로 잘못 읽혔다.
function renderReviewQueue(s) {
  const rows = [];
  for (const label of (s.review_labels || ['확인 필요'])) {
    for (const m of (s.groups[label] || [])) rows.push(m);
  }
  const html = rows.length
    ? '<table><tr><th>회의</th><th>날짜</th><th>상태</th></tr>' + rows.map((m) =>
        `<tr class="click" data-folder="${esc(m.folder)}"><td>${esc(m.title)}</td>
         <td class="meta">${esc(m.date)}</td><td>${esc(m.label)}</td></tr>`).join('') + '</table>'
    : '<div class="meta">검수할 것이 없습니다</div>';
  for (const id of ['#queue-review', '#review-list']) {
    const box = $(id);
    if (!box) continue;
    box.innerHTML = html;
    box.querySelectorAll('tr.click').forEach((tr) =>
      tr.addEventListener('click', () => openDetail(tr.dataset.folder)));
  }
  $('#review-n').textContent = rows.length ? rows.length + '건' : '';
}

function renderQueue(s) {
  let html = '';
  for (const label of (s.status_order || s.order)) {
    const rows = s.groups[label] || [];
    if (!rows.length) continue;
    html += `<div class="group"><h3>${esc(label)} <span class="badge">${rows.length}</span></h3><table>
      <tr><th>날짜</th><th>폴더</th><th>회의 제목</th><th>녹음 길이</th><th>상태</th></tr>` +
      rows.map((m) => `<tr class="click" data-folder="${esc(m.folder)}">
        <td>${esc(m.date || '-')}</td><td><code>${esc(m.folder)}</code></td>
        <td>${esc(m.title)}${m.late_start ? ' <span class="no">앞부분 없음</span>' : ''}</td>
        <td>${m.recording_now ? '<span class="rec">녹음 중</span>'
             : m.duration_min == null ? '<span class="dim">-</span>' : m.duration_min + '분'}</td>
        <td>${esc(m.label)}${m.reason ? ` <span class="meta">${esc(m.reason)}</span>` : ''}
        ${m.state === 'failed' ? ' <button class="small regen" data-folder="' + esc(m.folder) + '">다시 생성</button>' : ''}</td>
      </tr>`).join('') + '</table></div>';
  }
  $('#queue').innerHTML = html || '<div class="meta">회의 폴더가 없습니다</div>';
  $('#queue').querySelectorAll('tr.click').forEach((tr) =>
    tr.addEventListener('click', (ev) => {
      if (ev.target.classList.contains('regen')) return;
      openDetail(tr.dataset.folder);
    }));
  $('#queue').querySelectorAll('button.regen').forEach((b) =>
    b.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      const r = await req('/api/regenerate', { folder: b.dataset.folder });
      alert(r.message || r.error); refresh();
    }));
}

function yn(v) { return v ? '<span class="ok">있음</span>' : '<span class="no">없음</span>'; }

function renderDiag(d) {
  $('#diag').innerHTML = `<table>
    <tr><th>마지막 캘린더 확인</th><td>${esc(d.last_calendar_check)}</td></tr>
    <tr><th>자동 녹음 launchd</th><td>${d.launchd_recorder ? '<span class="ok">등록됨</span>' : '<span class="no">등록 안 됨</span>'}</td></tr>
    <tr><th>초안 워처 launchd</th><td>${d.launchd_watcher ? '<span class="ok">등록됨</span>' : '<span class="dim">등록 안 됨 (콘솔에서 수동 실행 가능)</span>'}</td></tr>
    <tr><th>whisper</th><td>${yn(d.whisper)}</td></tr>
    <tr><th>화자 분리 모델</th><td>${yn(d.diarization_model)}</td></tr>
    <tr><th>claude (초안 생성)</th><td>${yn(d.claude)}</td></tr>
    <tr><th>ICS 주소</th><td>${yn(d.ics_url)}</td></tr>
    <tr><th>최근 실패</th><td>${d.recent_failures.length ? d.recent_failures.map((f) =>
      `<div><code>${esc(f.folder)}</code> ${esc(f.reason)} <span class="meta">${esc(f.log)}</span></div>`).join('') : '<span class="dim">없음</span>'}</td></tr>
  </table>`;
}

// ------------------------------------------------------------------ 검수 화면
// [?] 는 "확인 필요", (미기입) 은 "채우지 못한 칸" 표기다 (노트 템플릿 규칙).
// 검수에서 놓치면 그대로 확정되므로 개수와 위치를 초안 위에 띄운다 (스펙 5-2).
const MARKS = [{ key: '[?]', what: '확인 필요' }, { key: '(미기입)', what: '빈 칸' }];

function lineNo(text, pos) { return text.slice(0, pos).split('\n').length; }

function jumpTo(pos, len) {
  const ta = $('#d-draft');
  ta.focus();
  ta.setSelectionRange(pos, pos + len);
  const before = ta.value.slice(0, pos).split('\n').length - 1;
  const total = ta.value.split('\n').length || 1;
  ta.scrollTop = Math.max(0, (before / total) * ta.scrollHeight - ta.clientHeight / 2);
}

function renderMarks() {
  const text = $('#d-draft').value, box = $('#d-marks'), found = [];
  for (const m of MARKS) {
    for (let i = text.indexOf(m.key); i !== -1; i = text.indexOf(m.key, i + 1)) {
      found.push({ key: m.key, what: m.what, pos: i });
    }
  }
  if (!found.length) {
    box.className = 'marks';
    box.innerHTML = '<span class="ok">[?] · (미기입) 없음</span>';
    return;
  }
  found.sort((a, b) => a.pos - b.pos);
  const counts = MARKS.map((m) => `${m.key} ${found.filter((f) => f.key === m.key).length}곳`).join(' · ');
  box.className = 'marks warn';
  box.innerHTML = `<b>확인할 곳 ${found.length}</b><span>${esc(counts)}</span>` +
    found.map((f) => `<button class="small jump" data-pos="${f.pos}" data-len="${f.key.length}"
      title="${esc(f.what)}">${lineNo(text, f.pos)}행 ${esc(f.key)}</button>`).join('');
  box.querySelectorAll('button.jump').forEach((b) =>
    b.addEventListener('click', () => jumpTo(+b.dataset.pos, +b.dataset.len)));
}

async function openDetail(folder) {
  CUR = await req('/api/meeting?folder=' + encodeURIComponent(folder));
  // 캘린더에서 폴더 없는 일정을 보고 온 경우를 되돌린다 (같은 오버레이를 쓴다)
  $('#d-cal').classList.add('hidden');
  ['#detail-body', '#index-row', '#detail-foot'].forEach((id) => $(id).classList.remove('hidden'));
  $('#d-title').textContent = CUR.title;
  $('#d-meta').innerHTML = `<code>${esc(CUR.folder)}</code> · ${esc(CUR.label)}` +
    (CUR.reason ? ` · ${esc(CUR.reason)}` : '');
  let warn = '';
  if (CUR.late_start) warn += `<div class="warn">회의 앞부분이 녹음에 없습니다. ${esc(CUR.late_note.split('\n')[0])}</div>`;
  if (CUR.state === 'suspect') warn += `<div class="warn">회의인지 확인입니다. 초안을 만들지 않았습니다. 사유: ${esc(CUR.reason)}</div>`;
  if (CUR.state === 'failed') warn += `<div class="warn">초안 생성 실패. ${esc(CUR.reason)} (로그 <code>${esc(CUR.log_path)}</code>)</div>`;
  if (CUR.state === 'approved') warn += `<div class="warn">이미 확정된 회의입니다. 아래는 확정본입니다.</div>`;
  if (CUR.speaker_hint) warn += `<div class="warn">${esc(CUR.speaker_hint)}</div>`;
  $('#d-warn').innerHTML = warn;
  $('#d-draft').value = CUR.state === 'approved' ? CUR.notes_text : CUR.draft_text;
  $('#t-speakers').innerHTML = `<pre>${esc(CUR.speakers || '(화자 분리본 없음)')}</pre>`;
  $('#spk-line').innerHTML = '';
  closeDrawer();
  $('#t-attendees pre').textContent = CUR.attendees || '(참석자 기록 없음)';
  $('#t-log pre').textContent = CUR.log_tail || '(로그 없음)';
  const row = CUR.index_row || {};
  ['종류', '주제', '관련 프로젝트', '핵심 결정'].forEach((k, i) => { $('#ir-' + i).value = row[k] || ''; });
  renderMarks();
  $('#b-approve').disabled = !$('#d-draft').value.trim() || CUR.state === 'approved';
  $('#x-folder').textContent = CUR.folder;
  $('#x-confirm').value = ''; $('#x-del').checked = false;
  $('#x-confirm-box').classList.add('hidden');
  $('#exclude-box').classList.add('hidden');
  checkConfirm();   // 제외 상자를 초기화했으니 버튼 상태도 이 폴더 기준으로 다시 계산한다
  $('#d-msg').textContent = '';
  $('#q-hits').innerHTML = ''; $('#q').value = '';
  $('#overlay').classList.remove('hidden');
  // 분리본이 있으면 라벨을 칩으로 바꾸고 화자 수 확인 줄을 채운다 (구간 자르기가 있어 뒤에 붙인다)
  if (CUR.has_speakers) loadSpeakers(CUR.folder);
}

function closeDetail() {
  $('#overlay').classList.add('hidden');
  CUR = null;
  clearInterval(SPK_TIMER);        // 폴링만 멈춘다. 분리 작업 자체는 서버에서 계속 돈다
}

function indexRow() {
  return { '종류': $('#ir-0').value, '주제': $('#ir-1').value,
           '관련 프로젝트': $('#ir-2').value, '핵심 결정': $('#ir-3').value };
}

// ------------------------------------------------------------------ 화자 등록 (검수 화면 안 서랍)
// 별도 등록 페이지는 없앴다 (스펙 3-3). 회의가 끝난 뒤 사람이 여는 화면은 검수 화면이고,
// 등록은 그 도중에 필요해지는 일이라 다른 페이지에 두면 도달하지 않는다.
let SPK = null;          // 지금 검수 중인 회의의 /api/speakers 응답
let SPK_TIMER = null;

async function loadSpeakers(folder, keepDrawer) {
  const open = keepDrawer ? (SPK && SPK._open) : null;
  SPK = await req('/api/speakers?folder=' + encodeURIComponent(folder));
  if (!SPK.ok) {
    $('#t-speakers').innerHTML = '<pre>' + esc(CUR ? CUR.speakers : '') + '</pre>' +
      '<div class="meta">' + esc(SPK.error) + '</div>';
    $('#spk-line').innerHTML = '';
    return;
  }
  SPK._open = open;
  renderSpeakerPane();
  renderSpeakerLine();
  if (open) openDrawer(open); else closeDrawer();
  if ((SPK.enroll || {}).state === 'running') pollJob(folder);
}

// 분리본 텍스트에서 라벨을 칩으로 바꾼다. 칩을 누르면 서랍이 열린다.
function renderSpeakerPane() {
  const text = (CUR && CUR.speakers) || '';
  const labels = SPK.labels.map((l) => l.label).filter((l) => l !== '(불명)');
  labels.sort((a, b) => b.length - a.length);   // 긴 라벨부터 (SPEAKER_1 이 SPEAKER_10 을 갉지 않게)
  let html = esc(text);
  // 두 번에 나눠 치환한다. 칩 HTML 안에 라벨 문자열이 다시 들어가므로 한 번에 하면 겹쳐 먹는다.
  labels.forEach((label, i) => { html = html.split(esc(label)).join('@@CHIP' + i + '@@'); });
  labels.forEach((label, i) => {
    const l = SPK.labels.find((x) => x.label === label);
    const cls = 'chip' + (l.named ? ' named' : '') + (l.enrollable ? '' : ' locked');
    // 중복 라벨은 본문에서도 클러스터 수를 붙여 드러낸다. 합쳐 보이면 왜 잠겼는지 알 수 없다.
    const face = l.duplicate ? esc(l.shown) : esc(label);
    const chip = '<button class="' + cls + '" data-label="' + esc(label) + '" title="' +
      esc(l.locked_reason || '') + '">' + face + '</button>';
    html = html.split('@@CHIP' + i + '@@').join(chip);
  });
  $('#t-speakers').innerHTML = '<div class="hint">이름을 붙이려면 <b>SPEAKER_NN</b> 을 누르세요. ' +
    '<button class="small" id="go-registry">등록부 관리</button></div><pre>' + html + '</pre>';
  $('#t-speakers').querySelectorAll('button.chip').forEach((b) =>
    b.addEventListener('click', () => openDrawer(b.dataset.label)));
  $('#go-registry').addEventListener('click', gotoRegistry);
}

function gotoRegistry() {
  closeDetail();
  loadRegistry();
  $('#reg-body').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// 화자 수 확인 줄. 판정 재료만 한 줄에 모은다 (스펙 3-5). 자동으로 다시 분리하지 않는다.
function renderSpeakerLine() {
  const e = SPK.enroll || {};
  const running = e.state === 'running';
  const low = (SPK.low_talkers || []).map((x) => esc(x.shown || x.label) + ' ' + x.minutes + '분').join(' · ');
  $('#spk-line').innerHTML = '<div class="spk-line' + (SPK.suggest_resplit ? ' warn' : '') + '">' +
    '<b>분리 결과 ' + SPK.speaker_count + '명</b> ' +
    '<span>참석 수락 ' + (SPK.accepted == null ? '기록 없음' : SPK.accepted + '명') + '</span> ' +
    (low ? '<span>발화 1분 미만: ' + low + '</span> ' : '') +
    ((SPK.duplicates || []).length ? '<span class="warn">같은 이름이 붙은 화자: ' +
      SPK.duplicates.map(esc).join(' · ') + '</span> ' : '') +
    '<span>실제 몇 명이었나요?</span> ' +
    '<input id="rs-n" type="number" min="1" max="20" value="' + SPK.speaker_count + '"' +
    (running ? ' disabled' : '') + '> ' +
    '<button id="rs-go" class="small"' + (running ? ' disabled' : '') + '>이 수로 다시 분리</button> ' +
    '<span class="meta" id="rs-msg">' + esc(e.message || '') + '</span>' +
    (SPK.resplit_reasons || []).map((r) => '<div class="meta">' + esc(r) + '</div>').join('') +
    '<div class="meta">다시 분리하면 이름이 붙어 있던 라벨을 잃을 수 있습니다 (클러스터가 달라지면 ' +
    '등록부 대조 결과가 바뀝니다). 실패하면 이전 분리본으로 되돌립니다. 수 분 걸립니다.</div></div>';
  if (running) return;
  $('#rs-go').addEventListener('click', async () => {
    const n = +$('#rs-n').value;
    if (!confirm('화자 ' + n + '명으로 다시 분리합니다. 이름이 붙은 라벨을 잃을 수 있습니다. 계속할까요?')) return;
    const r = await req('/api/resplit', { folder: CUR.folder, speakers: n });
    $('#rs-msg').textContent = r.message || r.error;
    if (r.ok) { $('#rs-go').disabled = true; pollJob(CUR.folder); }
  });
}

function closeDrawer() { $('#drawer').classList.add('hidden'); if (SPK) SPK._open = null; }

function openDrawer(label) {
  const l = SPK.labels.find((x) => x.label === label);
  if (!l) return;
  SPK._open = label;
  const list = (SPK.registry || []).map((n) => '<option value="' + esc(n) + '">').join('');
  const running = (SPK.enroll || {}).state === 'running';
  const clips = l.clips.map((c) => '<div class="clip"><span class="meta">' + esc(c.at) +
    ' (' + c.seconds + '초)</span><audio controls preload="none" src="' + esc(c.url) + '"></audio>' +
    '<div class="meta">' + esc(c.text) + '</div></div>').join('');
  $('#drawer').innerHTML = '<div class="drawer-head"><b>' + esc(l.shown || l.label) + '</b> ' +
    '<span class="meta">발화 ' + l.seconds + '초</span> <button id="dr-close" class="small">닫기</button></div>' +
    '<div class="clips">' + (clips || '<div class="meta">재생할 구간이 없습니다</div>') + '</div>' +
    (l.named ? '<div class="meta">등록부 대조로 자동 인식된 이름입니다 (유사도 0.5 기준이라 틀릴 수 있습니다)</div>' : '') +
    (!l.enrollable ? '<div class="warn">' + esc(l.locked_reason) + '</div>' +
      (l.duplicate ? '<button id="dr-resplit" class="small">화자 수 다시 넣기</button>' : '') :
      (l.named ? '<label class="chk"><input type="checkbox" id="dr-edit"> 이름 수정</label>' : '') +
      '<label>이름 <input id="dr-name" list="voices" value="' + esc(l.named ? l.label : '') + '"' +
      (l.named ? ' disabled' : '') + ' placeholder="예: 홍길동"></label>' +
      '<datalist id="voices">' + list + '</datalist>' +
      '<label class="chk"><input type="checkbox" id="dr-save" checked> 등록부에 저장 ' +
      '<span class="meta">(끄면 이 회의에만 이름이 붙고 다음 회의에는 붙지 않습니다. ' +
      '이 회의에서 다시 분리를 돌리면 화자 수가 같을 때 자동으로 다시 붙입니다)</span></label>' +
      '<button id="dr-go" class="primary"' + (running ? ' disabled' : '') + '>저장</button>' +
      '<div class="meta" id="dr-msg">' + esc((SPK.enroll || {}).message || '') + '</div>' +
      '<div class="meta">등록부에 저장하면 화자 분리를 두 번 다시 돌립니다 (수 분). 저장하지 않으면 ' +
      '분리본과 초안의 라벨만 바로 바꿉니다. 초안은 다시 만들지 않습니다.</div>' +
      '<button id="dr-registry" class="small">등록부 관리</button>');
  $('#drawer').classList.remove('hidden');
  $('#dr-close').addEventListener('click', closeDrawer);
  if ($('#dr-resplit')) {
    $('#dr-resplit').addEventListener('click', () => {
      $('#spk-line').scrollIntoView({ behavior: 'smooth', block: 'center' });
      if ($('#rs-n')) $('#rs-n').focus();
    });
  }
  if (!l.enrollable) return;
  if ($('#dr-edit')) {
    $('#dr-edit').addEventListener('change', (ev) => {
      $('#dr-name').disabled = !ev.target.checked;
      if (ev.target.checked) $('#dr-name').focus();
    });
  }
  $('#dr-registry').addEventListener('click', gotoRegistry);
  $('#dr-go').addEventListener('click', async () => {
    const v = $('#dr-name').value.trim();
    if (!v) { $('#dr-msg').textContent = '이름을 넣으세요'; return; }
    if (l.named && v === l.label) { $('#dr-msg').textContent = '이름이 그대로입니다'; return; }
    const names = {};
    names[l.label] = v;
    const save = $('#dr-save').checked;
    const r = await req('/api/enroll', { folder: CUR.folder, names, save_to_registry: save });
    $('#dr-msg').textContent = r.message || r.error;
    if (!r.ok) return;
    if (save) { $('#dr-go').disabled = true; pollJob(CUR.folder); }
    else { await reloadDetail(); loadRegistry(); }     // 치환만 하므로 바로 끝난다
  });
}

// 등록·재분리는 서버 스레드에서 돈다. 화면을 닫아도 계속되고 다시 열면 상태가 이어진다.
// 서버가 도중에 꺼지면 다음 기동 때 직전 분리본으로 되돌리고 "중단됨"으로 남는다 (스펙 7절).
function pollJob(folder) {
  clearInterval(SPK_TIMER);
  SPK_TIMER = setInterval(async () => {
    if (!CUR || CUR.folder !== folder) { clearInterval(SPK_TIMER); return; }
    const r = await req('/api/speakers?folder=' + encodeURIComponent(folder));
    const e = r.enroll || {};
    if ($('#rs-msg')) $('#rs-msg').textContent = e.message || '';
    if ($('#dr-msg')) $('#dr-msg').textContent = e.message || '';
    if (e.state !== 'running') {
      clearInterval(SPK_TIMER);
      await reloadDetail();
      loadRegistry();
      if ($('#rs-msg')) $('#rs-msg').textContent = e.message || '';
    }
  }, 5000);
}

async function reloadDetail() {
  const folder = CUR.folder;
  CUR = await req('/api/meeting?folder=' + encodeURIComponent(folder));
  if (CUR.state !== 'approved') $('#d-draft').value = CUR.draft_text;
  renderMarks();
  await loadSpeakers(folder, true);
}

// ------------------------------------------------------------------ 등록부 관리 (스펙 5-4)
async function loadRegistry() {
  const reg = await req('/api/registry');
  const locked = (reg.busy || []).length > 0;
  const tail = '<div id="reg-form"></div><div class="meta">등록부: <code>' + esc(reg.path) +
    '</code> · 고치기 전 사본을 <code>' + esc(reg.backup_dir) + '</code> 에 남깁니다. ' +
    '지운 항목은 다음 분리부터 반영됩니다.</div>';
  if (!reg.items.length) {
    $('#reg-body').innerHTML = '<div class="meta">등록된 목소리가 없습니다</div>' + tail;
    return;
  }
  $('#reg-body').innerHTML = (locked ? '<div class="warn">' + esc(reg.busy_reason) + '</div>' : '') +
    '<table><tr><th>이름</th><th>등록 분량</th><th>등록 출처 회의</th><th>마지막으로 붙은 회의</th><th></th></tr>' +
    reg.items.map((it) => '<tr><td><b>' + esc(it.name) + '</b></td><td>' +
      (it.seconds == null ? '<span class="dim">-</span>' : it.seconds + '초') + '</td>' +
      '<td><code>' + esc(it.source || '-') + '</code></td>' +
      '<td><code>' + esc(it.last_seen || '-') + '</code></td>' +
      '<td><button class="small reg-rn" data-name="' + esc(it.name) + '"' + (locked ? ' disabled' : '') +
      '>이름 변경</button> <button class="small danger reg-del" data-name="' + esc(it.name) + '"' +
      (locked ? ' disabled' : '') + '>삭제</button></td></tr>').join('') + '</table>' + tail;
  $('#reg-body').querySelectorAll('.reg-rn').forEach((b) =>
    b.addEventListener('click', () => renameForm(b.dataset.name)));
  $('#reg-body').querySelectorAll('.reg-del').forEach((b) =>
    b.addEventListener('click', () => deleteForm(b.dataset.name)));
}

function renameForm(name) {
  $('#reg-form').innerHTML = '<div class="group"><b>' + esc(name) + '</b> 이름 변경 ' +
    '<input id="rg-new" placeholder="새 이름" value="' + esc(name) + '"> ' +
    '<button id="rg-go" class="small">저장</button> <span class="meta" id="rg-msg"></span></div>';
  $('#rg-go').addEventListener('click', async () => {
    const r = await req('/api/registry', { action: 'rename', name, new_name: $('#rg-new').value });
    if (r.ok) { await loadRegistry(); $('#reg-form').innerHTML = '<div class="meta">' + esc(r.message) + '</div>'; }
    else $('#rg-msg').textContent = r.error;
  });
}

function deleteForm(name) {
  // 삭제는 되돌릴 수 없으므로 이름을 그대로 타이핑해야 버튼이 열린다 (수용 기준 33)
  $('#reg-form').innerHTML = '<div class="group"><b>' + esc(name) + '</b> 삭제' +
    '<div class="meta">지우려면 이름을 그대로 입력하세요: <code>' + esc(name) + '</code></div>' +
    '<input id="rg-confirm" autocomplete="off"> ' +
    '<button id="rg-del" class="small danger" disabled>삭제 실행</button> ' +
    '<span class="meta" id="rg-msg"></span></div>';
  $('#rg-confirm').addEventListener('input', () => {
    $('#rg-del').disabled = $('#rg-confirm').value !== name;
  });
  $('#rg-del').addEventListener('click', async () => {
    const r = await req('/api/registry', { action: 'delete', name, confirm: $('#rg-confirm').value });
    if (r.ok) { await loadRegistry(); $('#reg-form').innerHTML = '<div class="meta">' + esc(r.message) + '</div>'; }
    else $('#rg-msg').textContent = r.error;
  });
}

// ------------------------------------------------------------------ 캘린더 페이지 (스펙 5-2)
// 일정과 폴더는 slugify 로 이어진다 (스펙 3-6). 매핑이 결정적이라 별도 인덱스가 없다.
//
// **ICS 조회를 기다리지 않는다.** 먼저 캐시와 폴더 정보로 그리드를 그리고(첫 표시 0.06초),
// 안 받은 날은 /api/week-fetch 한 번으로 채운다. 서버가 ICS 를 1회 내려받아 임시 파일에
// 두고 7일을 펼치므로 한 주 최초 로딩이 약 4초다. 하루씩 받던 방식은 같은 1.5MB 를 7번
// 내려받아 13~16초였다 (2026-09-04 결정, 기준 41).
let WEEK = null;              // 지금 보고 있는 주의 /api/week 응답
let WEEK_START = '';          // 그 주의 월요일
let WEEK_SEQ = 0;             // 주를 옮기면 진행 중인 조회 결과를 버리려고 센다

function fmtDay(d) { return d.slice(5).replace('-', '/'); }

async function loadWeek(start, force) {
  const seq = ++WEEK_SEQ;
  WEEK_START = start || '';
  const qs = (WEEK_START ? 'start=' + encodeURIComponent(WEEK_START) : '') + (force ? '&force=1' : '');
  WEEK = await req('/api/week' + (qs ? '?' + qs : ''));
  WEEK_START = WEEK.start;
  renderWeek();
  // 안 받은 날이 있으면 한 번에 채운다. 서버가 ICS 를 1회만 내려받아 7일을 펼친다.
  const pending = WEEK.days.filter((d) => d.needs_fetch || force);
  if (!pending.length) return;
  pending.forEach((d) => { d.loading = true; });
  renderWeek();
  let got;
  try {
    got = await req('/api/week-fetch?start=' + encodeURIComponent(WEEK_START) + (force ? '&force=1' : ''));
  } catch (err) {
    pending.forEach((d) => { d.loading = false; });
    renderWeek();
    return;
  }
  if (seq !== WEEK_SEQ) return;                     // 주를 옮겼으면 버린다
  WEEK = got;
  renderWeek();
}

function weekCounts() {
  const c = { target: 0, approved: 0, review: 0, missing: 0 };
  for (const d of WEEK.days) for (const e of d.events) {
    if (e.state === 'not-target') continue;
    c.target++;
    if (e.state === 'approved') c.approved++;
    // 「확인 필요」 배지 정의와 맞춘다: 회의인지 확인은 세지 않는다 (2026-09-04 결정)
    if (e.state === 'review-wait' && e.folder_label === '확인 필요') c.review++;
    if (e.state === 'missing') c.missing++;
  }
  return c;
}

function renderWeek() {
  if (!WEEK) return;
  $('#wk-range').textContent = `${fmtDay(WEEK.start)} ~ ${fmtDay(WEEK.end)}` +
    (WEEK.this_week ? ' (이번 주)' : '');
  $('#wk-this').disabled = WEEK.this_week;
  const c = weekCounts();
  $('#wk-sum').innerHTML = `대상 ${c.target}건 중 확정 ${c.approved} · 확인 필요 ${c.review} · ` +
    (c.missing ? `<b class="miss">놓침 ${c.missing}</b>` : '놓침 0');

  // ICS 조회 실패는 그리드를 비우지 않는다. 사유와 재시도 버튼을 위에 적고
  // 폴더에서 나온 정보는 그대로 보여준다 (수용 기준 42).
  const errs = WEEK.days.filter((d) => d.error);
  $('#wk-err').innerHTML = (WEEK.cw_error ? `<div class="warn">${esc(WEEK.cw_error)}</div>` : '') +
    (errs.length ? `<div class="warn">캘린더 조회 실패 (${errs.length}일): ${esc(errs[0].error)}
      <button id="wk-retry" class="small">다시 시도</button>
      <div class="meta">아래 칸의 회의 폴더 정보는 조회와 무관하게 그대로 보입니다.</div></div>` : '');
  if ($('#wk-retry')) $('#wk-retry').addEventListener('click', () => loadWeek(WEEK_START, true));

  // 일정 칸에 이미 붙은 폴더는 「일정 없음」으로 또 그리지 않는다.
  // orphans 는 /api/week 응답 시점(하루씩 받기 전) 기준이라 그대로 쓰면 두 번 나온다.
  const taken = new Set();
  for (const d of WEEK.days) for (const e of d.events) if (e.exists) taken.add(e.folder);
  const orphans = (WEEK.orphans || []).filter((o) => !taken.has(o.folder));

  $('#wk-grid').innerHTML = '<div class="wk">' + WEEK.days.map((d) => {
    const cells = d.events.map((e) => cellHtml(e, d.day)).join('') +
      orphans.filter((o) => o.date === d.day).map(orphanHtml).join('');
    return `<div class="wkday${d.is_today ? ' today' : ''}">
      <div class="wkhead">${d.weekday} <span class="meta">${fmtDay(d.day)}</span></div>
      ${d.loading ? '<div class="meta">조회 중…</div>' : ''}
      ${cells || (d.loading ? '' : '<div class="meta dim">-</div>')}</div>`;
  }).join('') + '</div>';

  $('#wk-grid').querySelectorAll('.cell').forEach((el) =>
    el.addEventListener('click', () => {
      if (el.dataset.exists === '1') openDetail(el.dataset.folder);
      else openEventDetail(el.dataset.day, el.dataset.folder, el.dataset.title);
    }));
}

function cellHtml(e, day) {
  const sub = e.folder_label && e.folder_label !== e.label ? ' · ' + esc(e.folder_label) : '';
  return `<div class="cell st-${e.state}${e.record ? '' : ' dimcell'}"
      data-day="${esc(day)}" data-folder="${esc(e.folder || '')}"
      data-title="${esc(e.title)}" data-exists="${e.exists ? 1 : 0}">
    <div class="cwhen">${esc(e.when)}</div>
    <div class="ctitle">${esc(e.title)}</div>
    <div class="cbadge"><span class="badge b-${e.state}">${esc(e.label)}</span>${sub}</div>
    ${e.room ? `<div class="meta">${esc(e.room)}</div>` : ''}
    ${e.note ? `<div class="meta">${esc(e.note)}</div>` : ''}
  </div>`;
}

function orphanHtml(o) {
  // 일정에 붙지 않은 회의 폴더 (캘린더에서 지웠거나 손으로 만든 것)
  return `<div class="cell st-${o.state}" data-day="${esc(o.date)}" data-folder="${esc(o.folder)}"
      data-title="${esc(o.title)}" data-exists="1">
    <div class="cwhen meta">일정 없음</div>
    <div class="ctitle">${esc(o.title)}</div>
    <div class="cbadge"><span class="badge b-${o.state}">${esc(o.label)}</span> · ${esc(o.folder_label)}</div>
  </div>`;
}

async function openEventDetail(day, folder, title) {
  // 폴더가 없는 일정. 캘린더 정보와 상태 사유, 예상 폴더명만 보여준다 (수용 기준 38)
  const e = await req(`/api/event?day=${encodeURIComponent(day)}&folder=${encodeURIComponent(folder)}` +
                      `&title=${encodeURIComponent(title)}`);
  CUR = null;
  $('#d-title').textContent = e.title || title;
  $('#d-meta').innerHTML = e.error ? '' : `${esc(e.when)} · ${esc(e.label)}`;
  $('#d-warn').innerHTML = e.error ? `<div class="warn">${esc(e.error)}</div>`
    : (e.state === 'missing'
        ? '<div class="warn">녹음이 없습니다. 이 시각에 녹음 폴더가 만들어지지 않았습니다.</div>' : '');
  $('#spk-line').innerHTML = '';
  $('#d-cal').innerHTML = e.error ? '' : `<table>
    <tr><th>시각</th><td>${esc(e.when)}</td></tr>
    <tr><th>회의실</th><td>${esc(e.room || '(없음)')}</td></tr>
    <tr><th>녹음 대상</th><td>${e.record ? '예' : '아니오' + (e.skip_reason ? ' · ' + esc(e.skip_reason) : '')}</td></tr>
    <tr><th>상태</th><td>${esc(e.label)}${e.note ? ' · ' + esc(e.note) : ''}</td></tr>
    <tr><th>예상 폴더명</th><td><code>${esc(e.expected_folder || '')}</code></td></tr>
    <tr><th>참석자 응답</th><td>${(e.attendees || []).length
      ? (e.attendees || []).map((a) => `${esc(a.status)} ${esc(a.name || '(미확인)')}`).join('<br>')
      : '<span class="meta">기록 없음</span>'}</td></tr>
  </table>
  <p class="hint">폴더명은 <code>slugify(제목, 시작시각)</code> 으로 계산한 것입니다.
    녹음이 되면 이 이름으로 <code>docs/meetings/</code> 아래에 생깁니다.</p>`;
  $('#d-cal').classList.remove('hidden');
  $('#detail-body').classList.add('hidden');
  $('#index-row').classList.add('hidden');
  $('#detail-foot').classList.add('hidden');
  $('#exclude-box').classList.add('hidden');
  $('#overlay').classList.remove('hidden');
}

function showPanel(name) {
  document.querySelectorAll('.toptab').forEach((b) =>
    b.classList.toggle('on', b.dataset.panel === name));
  $('#panel-calendar').classList.toggle('hidden', name !== 'calendar');
  $('#panel-control').classList.toggle('hidden', name !== 'control');
}

// ------------------------------------------------------------------ 배선
async function refresh() {
  STATE = await req('/api/state');
  renderNow(STATE); renderToday(STATE.today); renderReviewQueue(STATE);
  renderQueue(STATE); renderDiag(STATE.diagnostics);
}

$('#btn-stop').addEventListener('click', async () => {
  $('#btn-stop').disabled = true;
  const r = await req('/api/recording/stop', {});
  alert(r.message || r.error); refresh();
});
$('#btn-auto').addEventListener('click', async () => {
  const r = await req('/api/autorecord', { on: !STATE.autorecord });
  if (!r.ok) alert('실패: ' + (r.error || ''));
  refresh();
});
$('#btn-today').addEventListener('click', async () => { renderToday(await req('/api/today/refresh', {})); });
$('#d-close').addEventListener('click', closeDetail);
$('#b-later').addEventListener('click', closeDetail);
$('#overlay').addEventListener('click', (e) => { if (e.target.id === 'overlay') closeDetail(); });
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach((x) => x.classList.remove('on'));
  t.classList.add('on');
  document.querySelectorAll('.tabbody').forEach((b) => b.classList.add('hidden'));
  $('#t-' + t.dataset.tab).classList.remove('hidden');
}));
$('#q').addEventListener('keydown', async (e) => {
  if (e.key !== 'Enter') return;
  const r = await req(`/api/transcript?folder=${encodeURIComponent(CUR.folder)}&q=${encodeURIComponent($('#q').value)}`);
  $('#q-hits').innerHTML = r.hits.length
    ? r.hits.map((h) => `<div class="meta">${h.line}행</div><div>${esc(h.text)}</div>`).join('')
    : '<div class="meta">찾은 것이 없습니다</div>';
});
$('#d-draft').addEventListener('input', renderMarks);
$('#b-approve').addEventListener('click', async () => {
  const r = await req('/api/approve', { folder: CUR.folder, text: $('#d-draft').value, index_row: indexRow() });
  $('#d-msg').textContent = r.message || r.error;
  if (r.ok) { await refresh(); closeDetail(); }
});
$('#b-regen').addEventListener('click', async () => {
  const r = await req('/api/regenerate', { folder: CUR.folder });
  $('#d-msg').textContent = r.message || r.error;
  refresh();
});
$('#b-exclude').addEventListener('click', () => $('#exclude-box').classList.toggle('hidden'));
$('#x-del').addEventListener('change', () => {
  $('#x-confirm-box').classList.toggle('hidden', !$('#x-del').checked);
  checkConfirm();
});
$('#x-confirm').addEventListener('input', checkConfirm);
function checkConfirm() {
  // 삭제를 고르면 폴더명을 정확히 타이핑해야만 버튼이 눌린다. 오디오 삭제는 되돌릴 수 없다.
  $('#x-go').disabled = $('#x-del').checked && $('#x-confirm').value !== (CUR ? CUR.folder : '');
  $('#x-go').textContent = $('#x-del').checked ? '제외하고 폴더 삭제' : '제외 실행';
}
$('#x-go').addEventListener('click', async () => {
  const r = await req('/api/exclude', { folder: CUR.folder, reason: $('#x-reason').value,
                                        delete: $('#x-del').checked, confirm: $('#x-confirm').value });
  $('#d-msg').textContent = r.message || r.error;
  if (r.ok) { await refresh(); closeDetail(); }
});

// 첫 화면은 캘린더 (스펙 8절 결정 4). 메뉴바의 「확인 필요 N건」은 #review 로 들어온다
document.querySelectorAll('.toptab').forEach((b) =>
  b.addEventListener('click', () => showPanel(b.dataset.panel)));
$('#wk-prev').addEventListener('click', () => loadWeek(shiftWeek(-7)));
$('#wk-next').addEventListener('click', () => loadWeek(shiftWeek(7)));
$('#wk-this').addEventListener('click', () => loadWeek(''));
$('#wk-reload').addEventListener('click', () => loadWeek(WEEK_START, true));
function shiftWeek(days) {
  const d = new Date(WEEK_START + 'T00:00:00');
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}
if (location.hash === '#queue') showPanel('control');       // 1단계 목적지 (하위 호환)
else {
  showPanel('calendar');
  if (location.hash === '#review') $('#card-review').scrollIntoView({ block: 'start' });
}

refresh();
loadWeek('');
loadRegistry();
$('#reg-reload').addEventListener('click', loadRegistry);
setInterval(refresh, 5000);
