// 설치 마법사 화면 (스펙 5-5, 수용 기준 43~49).
//
// 바깥으로 나가는 요청이 없다. 서버로 가는 통신은 req() 하나뿐이고 경로가 "/"로 시작해야 한다.
// ICS 주소는 입력칸에만 있고 화면에 다시 그리지 않는다 (기준 45·42).
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
  return res.json();
}

let SETUP = null;
const mark = (v) => v === 'pass' ? '통과' : v === 'fail' ? '미통과' : v === 'skip' ? '건너뜀' : '아직';

function renderSteps() {
  const st = SETUP.state.steps || {};
  $('#steps').innerHTML = SETUP.steps.map(([n, name]) => {
    const s = (st[String(n)] || {}).status;
    return `<button class="toptab ${s === 'pass' ? 'ok' : s === 'fail' ? 'no' : ''}" data-go="${n}">
      ${n}. ${esc(name)} <span class="meta">${mark(s)}</span></button>`;
  }).join('');
  $('#steps').querySelectorAll('[data-go]').forEach((b) =>
    b.addEventListener('click', () => show(Number(b.dataset.go))));
  $('#mdir').textContent = SETUP.meetings_dir + '/';
  $('#mic-app').textContent = SETUP.app;
}

function show(n) {
  for (let i = 1; i <= 7; i++) $('#step-' + i).style.display = i === n ? '' : 'none';
  location.hash = 'step-' + n;
  if (n === 2) loadSystem();
  if (n === 3) renderPlan();
  if (n === 4) renderModels();
  if (n === 6) renderHowto();
  if (n === 7) loadFinish();
}

function firstUndone() {
  const st = SETUP.state.steps || {};
  for (let i = 1; i <= 7; i++) {
    const s = (st[String(i)] || {}).status;
    if (s !== 'pass' && s !== 'skip') return i;
  }
  return 7;
}

async function pass(n, status) {
  const r = await req('/api/setup/step', { step: n, status: status || 'pass' });
  if (r.state) SETUP.state = r.state;
  renderSteps();
  if ((status || 'pass') !== 'fail') show(Math.min(n + 1, 7));
}

// ---------------------------------------------------------------- 2단계
async function loadSystem() {
  $('#sys').textContent = '확인 중';
  const d = await req('/api/setup/system');
  $('#sys').innerHTML = '<table><tr><th>항목</th><th>결과</th><th>해결 방법</th></tr>' +
    d.rows.map((r) => `<tr><td>${esc(r.name)}</td>
      <td>${r.ok ? '<span class="ok">통과</span>' : '<span class="no">미통과</span>'} <span class="meta">${esc(r.got)}</span></td>
      <td>${r.ok ? '' : '<code>' + esc(r.fix) + '</code>'}</td></tr>`).join('') + '</table>' +
    (d.blocked ? `<div class="warn">${esc(d.blocked_reason)}</div>` : '') +
    (!d.ok && !d.blocked ? '<div class="meta">미통과가 있어도 3단계에서 설치할 수 있습니다.</div>' : '');
  $('#sys-next').disabled = d.blocked;
}

// ---------------------------------------------------------------- 3단계
function renderPlan() {
  const plan = SETUP.plan || [];
  $('#plan').innerHTML = plan.length
    ? '<table><tr><th>없는 것</th><th>명령</th><th>비고</th></tr>' + plan.map((p) =>
      `<tr><td>${esc(p.name)}</td><td><code>${esc(p.cmd)}</code></td>
       <td class="meta">${p.auto ? '마법사가 설치합니다' : esc(p.why)}</td></tr>`).join('') + '</table>'
    : '<div class="ok">필요한 도구가 모두 있습니다.</div>';
  $('#do-install').disabled = !plan.some((p) => p.auto);
}

async function pollJob(name, box) {
  for (;;) {
    const j = await req('/api/setup/job?name=' + encodeURIComponent(name));
    box.textContent = (j.log || []).join('\n');
    box.scrollTop = box.scrollHeight;
    if (!j.running) return j;
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// ---------------------------------------------------------------- 4단계
function renderModels() {
  const m = SETUP.models;
  const row = (n, ok, size) => `<tr><td>${esc(n)}</td><td>${ok ? '<span class="ok">있음 (건너뜀)</span>' : '<span class="no">없음</span>'}</td><td class="meta">${esc(size)}</td></tr>`;
  $('#models').innerHTML = '<table><tr><th>모델</th><th>상태</th><th>용량</th></tr>' +
    row('whisper large-v3-turbo', m.whisper, m.whisper_size) +
    row('화자 분할 (segmentation)', m.segmentation, m.diar_size) +
    row('화자 임베딩 (embedding)', m.embedding, '') + '</table>';
  $('#do-models').disabled = m.whisper && m.segmentation && m.embedding;
}

// ---------------------------------------------------------------- 6단계
function renderHowto() {
  $('#ics-howto').innerHTML = (SETUP.howto || []).map((g, i) =>
    `<details${i === 0 ? ' open' : ''}><summary>${esc(g.name)}</summary><ol>` +
    (g.steps || []).map((h) => `<li>${esc(h)}</li>`).join('') + `</ol></details>`).join('');
}

// ---------------------------------------------------------------- 7단계
async function loadFinish() {
  const d = await req('/api/setup/finish', {});
  $('#finish').innerHTML = '<table>' +
    `<tr><th>자동 녹음 launchd</th><td>${d.autorecord ? '<span class="ok">등록됨</span>' : '<span class="no">등록 안 됨</span>'}</td></tr>` +
    `<tr><th>메뉴바 로그인 항목</th><td>${d.login_item ? '등록됨' : '등록 안 됨'}</td></tr>` +
    `<tr><th>다음 녹음 예정</th><td>${d.next ? esc(d.next.when + ' ' + d.next.title) : '<span class="meta">오늘 남은 대상이 없습니다</span>'}</td></tr>` +
    '</table>' + (d.warnings || []).map((w) => `<div class="warn">${esc(w)}</div>`).join('');
}

// ---------------------------------------------------------------- 묶기
async function boot() {
  SETUP = await req('/api/setup/state');
  renderSteps();
  const want = Number((location.hash.match(/step-(\d)/) || [])[1] || 0);
  show(want || firstUndone());

  document.querySelectorAll('[data-pass]').forEach((b) =>
    b.addEventListener('click', () => pass(Number(b.dataset.pass))));
  document.querySelectorAll('[data-skip]').forEach((b) =>
    b.addEventListener('click', () => pass(Number(b.dataset.skip), 'skip')));
  $('#sys-again').addEventListener('click', loadSystem);

  $('#do-install').addEventListener('click', async () => {
    $('#do-install').disabled = true;
    await req('/api/setup/install', {});
    const j = await pollJob('install', $('#install-log'));
    SETUP = await req('/api/setup/state');
    renderPlan(); renderSteps();
    $('#do-install').disabled = false;
    if (j.ok) await pass(3);
  });

  $('#do-models').addEventListener('click', async () => {
    $('#do-models').disabled = true;
    await req('/api/setup/models', {});
    const j = await pollJob('models', $('#models-log'));
    SETUP = await req('/api/setup/state');
    renderModels(); renderSteps();
    if (j.ok) await pass(4);
  });

  $('#mic-pref').addEventListener('click', () => { location.href = SETUP.deeplink; });
  $('#do-mic').addEventListener('click', async () => {
    $('#do-mic').disabled = true;
    $('#mic').innerHTML = '<div class="meta">3초 녹음 중 (포그라운드 → launchd)</div>';
    const d = await req('/api/setup/mic', {});
    const line = (r) => r ? `<div>${r.ok ? '<span class="ok">통과</span>' : '<span class="no">미통과</span>'}
      ${esc(r.where)} ${r.sec ? '· 길이 ' + r.sec.toFixed(1) + '초' : ''} ${r.mean != null ? '· 평균 음량 ' + r.mean + 'dB' : ''}
      ${r.reason ? '<div class="warn">' + esc(r.reason) + '</div>' : ''}</div>` : '';
    // launchd 확인은 임시 plist 를 등록했다 걷어낸다. 걷혔는지를 눈에 보이게 적는다
    const c = d.launchd && d.launchd.cleanup;
    const cleanup = !c ? '' : (c.ok
      ? '<div class="meta">임시 등록 걷어냄</div>'
      : `<div class="warn">임시 등록이 남았습니다. <code>launchctl bootout gui/$(id -u)/${esc(c.label || 'mictest')}</code> 로 걷어내세요.</div>`);
    $('#mic').innerHTML = line(d.foreground) + line(d.launchd) + cleanup +
      (d.ok ? '' : `<div class="warn">시스템 설정 > 개인정보 보호 > 마이크 에서 <b>${esc(d.app)}</b> 를 켜고 다시 확인하세요.
        <button id="mic-pref2" class="small">시스템 설정 열기</button></div>`);
    if ($('#mic-pref2')) $('#mic-pref2').addEventListener('click', () => { location.href = SETUP.deeplink; });
    SETUP = await req('/api/setup/state'); renderSteps();
    $('#do-mic').disabled = false;
    if (d.ok) await pass(5);
  });

  $('#do-ics').addEventListener('click', async () => {
    const url = $('#ics').value.trim();
    $('#ics-out').innerHTML = '<div class="meta">받아서 확인하는 중</div>';
    const d = await req('/api/setup/ics', { url });
    $('#ics').value = '';                               // 주소를 화면에 남기지 않는다
    if (!d.ok) { $('#ics-out').innerHTML = `<div class="warn">저장하지 않았습니다: ${esc(d.reason)}</div>`; return; }
    $('#ics-out').innerHTML = `<div class="ok">저장했습니다: <code>${esc(d.saved_to)}</code></div>
      <div>오늘 일정 ${d.count}건</div>` + (d.count ? '<table><tr><th>시각</th><th>일정</th><th>회의실</th><th>녹음</th></tr>' +
      d.events.map((e) => `<tr><td>${esc(e.when)}</td><td>${esc(e.title)}</td><td class="meta">${esc(e.room || '')}</td>
        <td>${e.record ? '<span class="rec">녹음</span>' : '<span class="dim">안 함</span> <span class="meta">' + esc(e.skip_reason || '') + '</span>'}</td></tr>`).join('') + '</table>' : '');
    SETUP = await req('/api/setup/state'); renderSteps();
    await pass(6);
  });

  $('#do-menubar').addEventListener('click', async () => {
    const d = await req('/api/setup/menubar', {});
    $('#menubar-out').innerHTML = d.ok ? `<div class="ok">${esc(d.message)}</div>`
      : `<div class="warn">${esc(d.reason)}</div>`;
  });
  const setLogin = async (on) => {
    const d = await req('/api/setup/login-item', { on });
    $('#menubar-out').innerHTML = `<div class="${d.ok ? 'ok' : 'warn'}">${esc(d.message)}</div>`;
    loadFinish();
  };
  $('#do-login-on').addEventListener('click', () => setLogin(true));
  $('#do-login-off').addEventListener('click', () => setLogin(false));
  $('#go-console').addEventListener('click', async () => {
    await req('/api/setup/step', { step: 7, status: 'pass' });
    location.href = '/?t=' + encodeURIComponent(TOKEN);
  });
  $('#reset').addEventListener('click', async () => {
    if (!confirm('통과 기록을 지웁니다. 설치된 것은 그대로입니다.')) return;
    const r = await req('/api/setup/reset', {});
    SETUP.state = r.state; renderSteps(); show(1);
  });
}

boot();
