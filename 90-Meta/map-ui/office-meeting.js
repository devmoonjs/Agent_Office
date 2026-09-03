'use strict';
// ─────────────────────────────────────────────────────────────
// 미팅 기록자 (meeting-recorder) — ● 녹음을 누르면 즉시 녹음이 시작되고 이 에이전트 화면으로 넘어온다.
// 대화 안의 폼 카드에서 회의 정보(프로젝트 · 참석자 · 회의명 · 일시(자동) · 장소(기본 회사))를 채우고,
// 녹음 종료 → "녹음을 저장하시겠습니까?" → 필수값 검사 → review-ui에 meta+오디오 저장 →
// /meeting/save 로 서버 파이프라인(전사 → /ingest 회의록·관계 검토) 시작. 진행은 이벤트 시스템 라인으로 보인다.
// office.html 전역( $, esc, add, scrollEnd, post, showModal, closeModal, open, setMode, renderHeader,
// renderRows, renderMain, AG, ORDER, sel, mode, drawerOn, RUNS, evHist )을 사용한다.
// ─────────────────────────────────────────────────────────────
var MR = (function () {
  var KEY = 'meeting-recorder', API = '/graph-api/';
  var R = { mr: null, chunks: [], t0: 0, timer: null, blob: null, stopped: false, saving: false,
            meta: { project: '', attendees: [], title: '', location: '회사', locationOther: '', context: '' },
            persons: [], groups: null, dept: null, startedAt: null, formEl: null, actEl: null, lastId: null,
            ac: null, analyser: null, waveRaf: null, stream: null,
            // 실시간 보조 — 원본 녹음과 별개로 도는 두 번째 레코더. sid는 서버 세션 키
            live: { assist: false, interject: false, sid: null, seq: 0, n: 0, mr: null, segTimer: null, poll: null, sync: null } };
  var LIVE_MS = 20000;   // 조각 길이 — 짧으면 문맥이 끊기고 길면 참견이 늦는다
  function isMe() { return sel === KEY; }
  function recording() { return !!(R.mr && R.mr.state === 'recording'); }
  function pad(n) { return String(n).padStart(2, '0'); }
  function fmtDT(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()); }
  function elapsed() { var s = Math.floor((Date.now() - R.t0) / 1000); return pad(Math.floor(s / 60)) + ':' + pad(s % 60); }
  function api(path, opt) { return fetch(API + path, opt || { cache: 'no-store' }).then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || j.detail || r.status); return j; }); }); }

  function ensureAgent() { if (!AG[KEY]) { AG[KEY] = { key: KEY, name: '미팅 기록자', color: '#d98a8a', hair: '#4a3030', kind: 'rec' }; ORDER.push(KEY); } }

  // ── 인물 · 본부 그룹 (graph Person.team_path 의 '…본부/연구소' 세그먼트, 없으면 기타) ──
  function deptOf(path, team) { var segs = String(path || team || '').split('>').map(function (x) { return x.trim(); }).filter(Boolean);
    for (var i = 0; i < segs.length; i++) if (/(본부|연구소)$/.test(segs[i])) return segs[i];
    return '기타'; }
  function loadPeople() {
    return api('graph').catch(function () { return { nodes: [] }; }).then(function (g) {
      var ps = (g.nodes || []).filter(function (n) { return n.label === 'Person'; }).map(function (n) { return { name: n.key, team: n.props.team || '', dept: deptOf(n.props.team_path, n.props.team) }; });
      R.persons = ps.sort(function (a, b) { return a.name.localeCompare(b.name, 'ko'); });
      var groups = {}; ps.forEach(function (p) { (groups[p.dept] = groups[p.dept] || []).push(p); });
      R.groups = Object.keys(groups).sort(function (a, b) { if (a === '기타') return 1; if (b === '기타') return -1; return groups[b].length - groups[a].length || a.localeCompare(b, 'ko'); }).map(function (d) { return { dept: d, members: groups[d] }; });
      if (!R.dept && R.groups.length) R.dept = R.groups[0].dept;
      renderPicker();
    });
  }
  // 프로젝트 선택 → 과거 회의에 2회 이상 참석한 사람을 참석자로 미리 넣는다 (빼는 건 칩 ✕ 한 번)
  function autoAttendees(project) {
    if (!project) return;
    fetch('/meeting/attendees?project=' + encodeURIComponent(project), { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
      var list = d.attendees || [], reg = list.filter(function (x) { return x.count >= 2; });
      if (!reg.length) reg = list.slice(0, 5);
      var added = [];
      reg.forEach(function (x) { if (R.meta.attendees.indexOf(x.name) < 0) { R.meta.attendees.push(x.name); added.push(x.name); } });
      renderAtt();
      var note = R.formEl && R.formEl.querySelector('[data-autonote]');
      if (note) note.textContent = added.length ? project + ' 회의 단골 ' + added.length + '명 자동 추가 — 불참자는 ✕로 제거' : (list.length ? '' : project + ' 과거 회의록 없음');
    }).catch(function () {});
  }

  // ── 시작 / 종료 ──
  function start() {
    if (recording()) { if (!isMe()) open(KEY); return; }
    if (R.blob && !R.saving) { if (!confirm('저장하지 않은 이전 녹음이 있습니다. 버리고 새로 시작할까요?')) return; R.blob = null; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { open(KEY); add('sys', '이 브라우저/컨텍스트에서는 마이크(getUserMedia)를 쓸 수 없습니다 — https 또는 localhost에서 열어야 합니다'); return; }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var mime = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : MediaRecorder.isTypeSupported('audio/mp4') ? 'audio/mp4' : '';
      R.mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined); R.stream = stream; R.chunks = []; R.blob = null; R.stopped = false;
      R.mr.ondataavailable = function (e) { if (e.data.size) R.chunks.push(e.data); };
      R.mr.start(1000); R.t0 = Date.now(); R.startedAt = new Date();
      waveStart(stream);
      R.live.sid = 'live' + Date.now().toString(36); R.live.seq = 0; R.live.n = 0;
      if (liveOn()) { liveSync(); liveSegLoop(); livePoll(); }   // 이전 회의에서 켜 둔 토글은 그대로 이어간다
      R.meta = { project: '', attendees: [], title: '', location: '회사', locationOther: '', context: '' };
      loadPeople();
      R.timer = setInterval(function () { var el = $('mr-time'); if (el) el.textContent = elapsed(); var h = $('mr-htime'); if (h) h.textContent = elapsed(); }, 500);
      open(KEY); if (mode !== 'chat') setMode('chat');
    }).catch(function (e) { open(KEY); add('sys', '마이크를 사용할 수 없습니다 — ' + e.message + ' · 브라우저 마이크 권한을 확인하세요'); });
  }
  function stop() {
    if (!recording()) return; clearInterval(R.timer); liveHalt();
    R.mr.onstop = function () {
      R.mr.stream.getTracks().forEach(function (t) { t.stop(); }); waveStop();
      R.blob = new Blob(R.chunks, { type: R.mr.mimeType || 'audio/webm' }); R.stopped = true;
      if (!isMe()) open(KEY); else renderHeader();
      if (!R.blob.size) { add('sys', '녹음된 오디오가 없습니다 (0바이트) — 마이크 입력 장치와 브라우저 권한을 확인하세요'); return; }
      askSave();
    };
    R.mr.stop();
  }
  function askSave() {
    showModal('<div class="mh"><h2>녹음을 저장하시겠습니까?</h2><button class="x">✕</button></div>' +
      '<p>' + esc(R.meta.title || '(회의명 미입력)') + ' · ' + elapsed() + ' · ' + Math.round(R.blob.size / 1024) + 'KB<br>저장하면 전사 → 회의록 작성 → 관계 추출(ingest)이 자동으로 이어집니다. 빈 항목이 있으면 먼저 채우게 안내합니다.</p>' +
      '<div class="act"><button class="pri" id="mr-save">저장</button><button id="mr-later">나중에 — 정보 더 채우기</button><button id="mr-discard" style="color:var(--bad)">폐기</button></div>');
    $('mr-save').addEventListener('click', function () { closeModal(); save(); });
    $('mr-later').addEventListener('click', function () { closeModal(); renderSaveRow(); });
    $('mr-discard').addEventListener('click', function () { if (!confirm('녹음을 폐기할까요? 복구할 수 없습니다.')) return; closeModal(); R.blob = null; R.stopped = false; add('sys', '녹음 폐기'); renderHeader(); });
  }
  function renderSaveRow() {
    if (!isMe() || mode !== 'chat') return;
    if (R.actEl) R.actEl.remove();
    var w = $('wrap'); if (!w) return;
    var d = document.createElement('div'); d.className = 'act'; d.innerHTML = '<button class="pri" id="mr-save2">녹음 저장 — 회의록 작성·ingest</button><button id="mr-discard2">폐기</button>';
    w.appendChild(d); R.actEl = d; scrollEnd(true);
    $('mr-save2').addEventListener('click', save);
    $('mr-discard2').addEventListener('click', function () { if (!confirm('녹음을 폐기할까요?')) return; R.blob = null; R.stopped = false; d.remove(); add('sys', '녹음 폐기'); renderHeader(); });
  }

  // ── 실시간 보조 — 답변도우미 · 참견모드 ──────────────────────────
  // 원본 녹음(R.mr)은 그대로 두고, 같은 마이크 스트림 위에 두 번째 MediaRecorder를 20초마다
  // 새로 만들었다 세운다. 매번 새 인스턴스여야 조각 하나하나가 헤더를 갖춘 독립 webm이 되어
  // 서버에서 단독으로 디코딩된다(한 인스턴스의 2번째 이후 timeslice 조각은 단독 디코딩 불가).
  // 조각 → /meeting/live/chunk → 상주 whisper 전사 → claude(haiku) → 카드는 폴링으로 받는다.
  function liveOn() { return !!(R.live.assist || R.live.interject); }
  function liveSync() {   // 토글·회의 메타를 서버 세션에 반영 (프롬프트의 회의 맥락이 된다)
    if (!R.live.sid) return Promise.resolve();
    return post('/meeting/live/mode', { id: R.live.sid, assist: R.live.assist, interject: R.live.interject,
      title: R.meta.title, project: R.meta.project, attendees: R.meta.attendees }).catch(function () {});
  }
  function liveTouch() { if (!liveOn()) return; clearTimeout(R.live.sync); R.live.sync = setTimeout(liveSync, 1500); }
  function liveSegLoop() {
    if (R.live.mr || !recording() || !liveOn() || !R.stream) return;
    (function cycle() {
      if (!recording() || !liveOn() || !R.stream) { R.live.mr = null; return; }
      var mr;
      try { mr = new MediaRecorder(R.stream, MediaRecorder.isTypeSupported('audio/webm') ? { mimeType: 'audio/webm' } : undefined); }
      catch (e) { R.live.mr = null; addLive('실시간 보조를 시작하지 못했습니다 — ' + e.message, {}, KEY); return; }
      var parts = [];
      mr.ondataavailable = function (e) { if (e.data.size) parts.push(e.data); };
      mr.onstop = function () { var b = new Blob(parts, { type: mr.mimeType || 'audio/webm' }); if (b.size > 2048) liveUpload(b); cycle(); };
      R.live.mr = mr; mr.start();
      R.live.segTimer = setTimeout(function () { if (mr.state === 'recording') mr.stop(); }, LIVE_MS);
    })();
  }
  function liveUpload(blob) {
    R.live.seq++;
    fetch('/meeting/live/chunk?id=' + encodeURIComponent(R.live.sid) + '&seq=' + R.live.seq,
      { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: blob }).catch(function () {});
  }
  function livePoll() {
    if (R.live.poll) return;
    R.live.poll = setInterval(function () {
      if (!R.live.sid || !liveOn()) return;
      fetch('/meeting/live/state?id=' + encodeURIComponent(R.live.sid) + '&since=' + R.live.n, { cache: 'no-store' })
        .then(function (r) { return r.json(); }).then(function (d) {
          (d.cards || []).forEach(function (c) { if (c.id > R.live.n) { R.live.n = c.id; liveCard(c); } });
          var el = document.getElementById('mr-live-tail');
          var kb = { loading: '프로젝트 자료 조사 중 · ', failed: '프로젝트 자료 없음 · ', ready: '', none: '' }[d.kb_state || 'none'] || '';
          if (el) el.textContent = d.err ? d.err : kb + (d.segs ? (d.busy ? '생각하는 중… · ' : '듣는 중 · ') + (d.tail || '') : '듣는 중 — 첫 전사까지 30초쯤 걸립니다');
        }).catch(function () {});
    }, 3000);
  }
  function liveCard(c) {
    var lbl = c.kind === 'assist' ? '답변도우미' : '참견';
    addLive('', { html: '<div class="mr-card ' + esc(c.kind) + '"><b>' + lbl + '</b><i>' + esc(c.ts) + '</i><p>' + esc(c.text) + '</p></div>' }, KEY);
  }
  function liveHalt(drop) {   // 녹음 종료·토글 해제 — 조각 생성을 멈춘다. drop이면 서버 세션도 버린다
    clearTimeout(R.live.segTimer); R.live.segTimer = null;
    if (R.live.mr && R.live.mr.state === 'recording') { R.live.mr.onstop = null; try { R.live.mr.stop(); } catch (e) {} }
    R.live.mr = null;
    if (drop && R.live.sid) { post('/meeting/live/stop', { id: R.live.sid }).catch(function () {}); R.live.sid = null; }
    if ((drop || !liveOn()) && R.live.poll) { clearInterval(R.live.poll); R.live.poll = null; }
  }
  function toggleLive(which) {
    R.live[which] = !R.live[which];
    renderToggles();
    if (!R.live.sid) return;                      // 녹음 전에 켜두면 시작할 때 자동으로 붙는다
    liveSync().then(function () {
      if (liveOn()) { liveSegLoop(); livePoll(); }
      else { liveHalt(); addLive('실시간 보조 종료 — 녹음·회의록 파이프라인은 그대로 진행됩니다', {}, KEY); }
    });
  }
  function togglesHtml() {
    var sw = function (k, label, hint) {
      return '<button class="mr-sw" data-tg="' + k + '" aria-pressed="' + !!R.live[k] + '" title="' + esc(hint) + '"><i></i>' + label + '</button>';
    };
    return '<div class="mr-tg">' + sw('assist', '답변도우미', '나에게 온 질문이면 그대로 말할 답변 초안을 띄운다') +
      sw('interject', '참견모드', '프로젝트 자료·지난 결정과 어긋나는 대목이 보이면 물어볼 질문을 띄운다') +
      '<span class="mr-tgn" id="mr-live-tail">' + (liveOn() ? '켜짐 — 20초마다 듣고 정리합니다' : '꺼짐 — 녹음만 진행합니다') + '</span></div>';
  }
  function renderToggles() {
    var box = document.getElementById('mr-tgbox'); if (!box) return;
    box.innerHTML = togglesHtml();
    box.querySelectorAll('[data-tg]').forEach(function (b) { b.addEventListener('click', function () { toggleLive(b.dataset.tg); }); });
  }

  // ── 입력 파형 (실제 수음 확인용) — AnalyserNode 시간영역 데이터를 캔버스에 그린다 ──
  function waveStart(stream) {
    try { R.ac = new (window.AudioContext || window.webkitAudioContext)(); var src = R.ac.createMediaStreamSource(stream); R.analyser = R.ac.createAnalyser(); R.analyser.fftSize = 1024; src.connect(R.analyser); } catch (e) { R.analyser = null; return; }
    var buf = new Uint8Array(R.analyser.fftSize);
    (function loop() { if (!R.analyser) return; R.waveRaf = requestAnimationFrame(loop);
      R.analyser.getByteTimeDomainData(buf);
      var peak = 0; for (var i = 0; i < buf.length; i++) peak = Math.max(peak, Math.abs(buf[i] - 128));
      document.querySelectorAll('canvas.mr-wave').forEach(function (cv) { drawWave(cv, buf, peak); }); })();
  }
  function waveStop() { if (R.waveRaf) cancelAnimationFrame(R.waveRaf); R.waveRaf = null; R.analyser = null; if (R.ac) { try { R.ac.close(); } catch (e) {} R.ac = null; } }
  function drawWave(cv, buf, peak) {
    var W = cv.width, H = cv.height, x = cv.getContext('2d'); x.clearRect(0, 0, W, H);
    var col = peak < 3 ? getComputedStyle(document.documentElement).getPropertyValue('--tx3').trim() || '#6a6a6a' : (getComputedStyle(document.documentElement).getPropertyValue('--bad').trim() || '#e06a6a');
    x.strokeStyle = col; x.lineWidth = cv.classList.contains('sm') ? 1.5 : 2; x.beginPath();
    var step = buf.length / W;
    for (var i = 0; i < W; i++) { var v = (buf[Math.floor(i * step)] - 128) / 128; var y = H / 2 + v * (H / 2 - 2); if (i === 0) x.moveTo(i, y); else x.lineTo(i, y); }
    x.stroke();
    if (peak < 3 && !cv.classList.contains('sm')) { x.fillStyle = col; x.font = '11px -apple-system,sans-serif'; x.textAlign = 'right'; x.fillText('입력 없음 — 마이크 확인', W - 8, 14); }
  }

  // ── 검증 · 저장 ──
  function missing() {
    var m = [];
    if (!R.meta.project) m.push('프로젝트');
    if (!R.meta.attendees.length) m.push('참석자');
    if (!R.meta.title.trim()) m.push('회의명');
    if (R.meta.location === '__other__' && !R.meta.locationOther.trim()) m.push('장소');
    return m;
  }
  function save() {
    if (!R.blob || !R.blob.size) { add('sys', '저장할 녹음이 없습니다'); return; }
    var m = missing();
    if (m.length) {
      add('sys', '필수 항목이 비어 있습니다 — ' + m.join(', ') + ' · 위 폼에서 채운 뒤 다시 [녹음 저장]');
      highlight(m); renderSaveRow(); return;
    }
    if (R.saving) return; R.saving = true;
    if (R.actEl) { R.actEl.remove(); R.actEl = null; }
    var loc = R.meta.location === '__other__' ? R.meta.locationOther.trim() : '회사';
    var recordedAt = fmtDT(R.startedAt || new Date());
    add('user', '녹음 저장 — ' + R.meta.title + ' · ' + R.meta.project + ' · ' + R.meta.attendees.join(', ') + ' · ' + recordedAt + ' · ' + loc, { force: true });
    var w = add('sys', '업로드 중… (' + Math.round(R.blob.size / 1024) + 'KB)');
    var body = { topic: R.meta.title.trim(), attendees: R.meta.attendees, project: R.meta.project, context: ('장소: ' + loc + ' · 일시: ' + recordedAt + (R.meta.context ? ' · ' + R.meta.context : '')) };
    api('recordings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (out) { if (!out.ok) throw new Error(out.error || '메타 저장 실패'); R.lastId = out.id;
        return fetch(API + 'recordings/' + encodeURIComponent(out.id) + '/audio', { method: 'POST', headers: { 'Content-Type': R.blob.type.split(';')[0] || 'audio/webm' }, body: R.blob }).then(function (r) { return r.json(); }).then(function (up) { if (!up.ok) throw new Error(up.error || '오디오 업로드 실패'); return out.id; }); })
      .then(function (id) { return post('/meeting/save', { id: id, title: R.meta.title.trim(), project: R.meta.project, attendees: R.meta.attendees, location: loc, recorded_at: recordedAt, context: R.meta.context, duration_sec: Math.floor((Date.now() - R.t0) / 1000) }); })
      .then(function (res) { w.remove(); R.blob = null; R.stopped = false; R.saving = false; lockForm(); liveHalt(true);
        add('sys', '저장 완료 — ' + res.id + ' · 전사 → 회의록 작성 → 관계 추출이 순서대로 진행됩니다 (수 분). 진행 상황은 이 대화에 표시되고, 끝나면 관계 컨펌 봇에 검토가 올라옵니다');
        renderHeader(); renderRows(); })
      .catch(function (e) { w.remove(); R.saving = false; add('sys', '저장 실패 — ' + e.message + ' · 녹음 데이터는 브라우저에 남아 있습니다. 다시 시도하세요'); renderSaveRow(); });
  }

  // ── 폼 카드 (대화 안) ──
  function plist() { return R.persons.map(function (p) { return '<option value="' + esc(p.name) + '">' + esc(p.team || '') + '</option>'; }).join(''); }
  function formHtml() {
    var projs = Object.keys(AG).filter(function (k) { return AG[k].kind === 'proj'; }).map(function (k) { return AG[k].name; });
    var custom = R.meta.project && projs.indexOf(R.meta.project) < 0;
    return '<div class="mr-form">' +
      '<div class="mr-f"><label>프로젝트 <i>*</i></label><div class="mr-v"><div class="chips">' + projs.map(function (n) { return '<button class="chip" data-proj="' + esc(n) + '" aria-selected="' + (R.meta.project === n) + '">' + esc(n) + '</button>'; }).join('') +
        '<input class="mr-in" data-projin placeholder="직접 입력" value="' + (custom ? esc(R.meta.project) : '') + '"></div><div class="mr-note" data-autonote></div></div></div>' +
      '<div class="mr-f"><label>참석자 <i>*</i></label><div class="mr-v"><div class="chips" data-attchips></div>' +
        '<div class="mr-picker"><div class="chips" data-depts></div><div class="chips" data-members></div>' +
        '<div class="mr-row"><input class="mr-in" data-att placeholder="명단에 없는 사람 — 이름 (소속) 입력 후 Enter"><button class="chip" data-attadd>추가</button></div></div></div></div>' +
      '<div class="mr-f"><label>회의명 <i>*</i></label><div class="mr-v"><input class="mr-in wide" data-title placeholder="예: 샘플AI 위험관리 체크리스트 초안 검토" value="' + esc(R.meta.title) + '"></div></div>' +
      '<div class="mr-f"><label>일시</label><div class="mr-v mr-static">' + esc(fmtDT(R.startedAt || new Date())) + ' <span>· 녹음 시작 시각으로 자동 기록</span></div></div>' +
      '<div class="mr-f"><label>장소</label><div class="mr-v"><div class="chips"><button class="chip" data-loc="회사" aria-selected="' + (R.meta.location !== '__other__') + '">회사</button><button class="chip" data-loc="__other__" aria-selected="' + (R.meta.location === '__other__') + '">그 외</button>' +
        '<input class="mr-in" data-locin placeholder="장소 입력" value="' + esc(R.meta.locationOther) + '" style="' + (R.meta.location === '__other__' ? '' : 'display:none') + '"></div></div></div>' +
      '<div class="mr-f"><label>메모</label><div class="mr-v"><input class="mr-in wide" data-ctx placeholder="배경·이전 회의와의 관계·유의점 (선택)" value="' + esc(R.meta.context) + '"></div></div>' +
      '</div>';
  }
  function renderPicker() {
    var d = R.formEl; if (!d) return; var de = d.querySelector('[data-depts]'), me = d.querySelector('[data-members]'); if (!de || !me) return;
    if (!R.groups) { de.innerHTML = '<span class="mr-note">인물 명단 불러오는 중…</span>'; me.innerHTML = ''; return; }
    de.innerHTML = R.groups.map(function (g) { return '<button class="chip mr-dept" data-dept="' + esc(g.dept) + '" aria-selected="' + (R.dept === g.dept) + '">' + esc(g.dept) + ' <span>' + g.members.length + '</span></button>'; }).join('');
    var g = R.groups.filter(function (x) { return x.dept === R.dept; })[0];
    me.innerHTML = g ? g.members.map(function (p) { var on = R.meta.attendees.indexOf(p.name) >= 0; return '<button class="chip mr-mem" data-name="' + esc(p.name) + '" aria-selected="' + on + '" title="' + esc(p.team) + '">' + esc(p.name) + (p.team && p.team !== g.dept ? '<small>' + esc(p.team) + '</small>' : '') + '</button>'; }).join('') : '';
    de.querySelectorAll('[data-dept]').forEach(function (b) { b.addEventListener('click', function () { R.dept = b.dataset.dept; renderPicker(); }); });
    me.querySelectorAll('[data-name]').forEach(function (b) { b.addEventListener('click', function () { var n = b.dataset.name, i = R.meta.attendees.indexOf(n); if (i >= 0) R.meta.attendees.splice(i, 1); else R.meta.attendees.push(n); renderAtt(); renderPicker(); liveTouch(); }); });
  }
  function renderForm() {
    var d = add('bot', '', { plain: true, force: true }); if (!d) return;
    d.style.maxWidth = '100%'; d.style.width = 'min(720px,100%)';
    d.innerHTML = '<div class="gr-q">회의 정보를 채워 주세요 — 녹음 중에도 됩니다. <span class="qtype">* 필수 · 저장 시 회의록 frontmatter의 권위 출처</span></div>' + formHtml();
    R.formEl = d; bindForm(d); renderAtt(); renderPicker();
  }
  function bindForm(d) {
    d.querySelectorAll('[data-proj]').forEach(function (b) { b.addEventListener('click', function () { R.meta.project = b.dataset.proj; d.querySelector('[data-projin]').value = ''; d.querySelectorAll('[data-proj]').forEach(function (x) { x.setAttribute('aria-selected', String(x === b)); }); autoAttendees(R.meta.project); liveTouch(); }); });
    d.querySelector('[data-projin]').addEventListener('input', function (e) { R.meta.project = e.target.value.trim(); d.querySelectorAll('[data-proj]').forEach(function (x) { x.setAttribute('aria-selected', 'false'); }); });
    d.querySelector('[data-projin]').addEventListener('change', function (e) { autoAttendees(e.target.value.trim()); });
    var ai = d.querySelector('[data-att]');
    function addA() { var v = ai.value.trim(); if (v && R.meta.attendees.indexOf(v) < 0) R.meta.attendees.push(v); ai.value = ''; renderAtt(); liveTouch(); ai.focus(); }
    ai.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); addA(); } });
    d.querySelector('[data-attadd]').addEventListener('click', addA);
    d.querySelector('[data-title]').addEventListener('input', function (e) { R.meta.title = e.target.value; liveTouch(); });
    d.querySelectorAll('[data-loc]').forEach(function (b) { b.addEventListener('click', function () { R.meta.location = b.dataset.loc; d.querySelectorAll('[data-loc]').forEach(function (x) { x.setAttribute('aria-selected', String(x === b)); }); var li = d.querySelector('[data-locin]'); li.style.display = R.meta.location === '__other__' ? '' : 'none'; if (R.meta.location === '__other__') li.focus(); }); });
    d.querySelector('[data-locin]').addEventListener('input', function (e) { R.meta.locationOther = e.target.value; });
    d.querySelector('[data-ctx]').addEventListener('input', function (e) { R.meta.context = e.target.value; });
  }
  function renderAtt() { var el = R.formEl && R.formEl.querySelector('[data-attchips]'); if (!el) return;
    el.innerHTML = R.meta.attendees.length ? R.meta.attendees.map(function (a, i) { return '<button class="chip" data-rm="' + i + '" aria-selected="true" title="클릭하면 제거">' + esc(a) + ' ✕</button>'; }).join('') : '<span class="gr-ev" style="margin:0">아직 없음</span>';
    el.querySelectorAll('[data-rm]').forEach(function (b) { b.addEventListener('click', function () { R.meta.attendees.splice(+b.dataset.rm, 1); renderAtt(); renderPicker(); }); }); }
  function highlight(list) { var d = R.formEl; if (!d) return; var map = { '프로젝트': '[data-projin]', '참석자': '[data-att]', '회의명': '[data-title]', '장소': '[data-locin]' };
    list.forEach(function (n) { var el = d.querySelector(map[n]); if (el) { el.classList.add('mr-miss'); setTimeout(function () { el.classList.remove('mr-miss'); }, 3000); } });
    var first = d.querySelector(map[list[0]]); if (first) { first.scrollIntoView({ block: 'center' }); first.focus(); } }
  function lockForm() { var d = R.formEl; if (!d) return; d.querySelectorAll('input,button').forEach(function (x) { x.disabled = true; }); d.style.opacity = .7; R.formEl = null; }

  // ── 대화 본문 · 헤더 · 드로어 ──
  function renderChatBody() {
    if (recording() || R.stopped) {
      add('sys', '', { html: recording() ? '<span style="color:var(--bad)">● 녹음 중 <span id="mr-time">' + elapsed() + '</span></span> — 회의를 진행하세요. 끝나면 헤더의 [■ 녹음 종료]. 이 탭을 닫거나 Mac이 잠자기에 들어가면 끊깁니다' : '■ 녹음 종료 — ' + elapsed() + ' · 아래 폼을 확인하고 [녹음 저장]' });
      if (recording()) { var wv = document.createElement('div'); wv.className = 'mr-wavebox'; wv.innerHTML = '<canvas class="mr-wave" width="720" height="56"></canvas><span>실시간 입력 파형 — 선이 움직이면 수음 중</span>'; $('wrap').appendChild(wv); }
      if (recording()) { var tb = document.createElement('div'); tb.id = 'mr-tgbox'; $('wrap').appendChild(tb); renderToggles(); }
      renderForm();
      if (R.stopped && R.blob) renderSaveRow();
    } else {
      var run = RUNS[KEY];
      if (run && run.running) add('sys', '', { html: '<span class="spin"></span><span id="mr-run">' + esc(runLabel(run)) + '</span>' });
      else add('sys', '녹음이 없습니다. 헤더 또는 사이드바의 ● 녹음을 누르면 바로 녹음이 시작되고 회의 정보를 입력할 수 있습니다');
    }
  }
  // 파이프라인 진행 문구 — 전사는 %(세그먼트 타임스탬프÷녹음 길이), ingest는 경과 시간만.
  // elapsed는 서버가 응답 시점에 계산해 준 값이라, 여기에 수신 후 흐른 시간만 더하면 시계 차이와 무관하다.
  function hms(s) { return pad(Math.floor(s / 3600)) + ':' + pad(Math.floor(s % 3600 / 60)) + ':' + pad(s % 60); }
  function runLabel(run) {
    if (!run || !run.running) return '';
    var s = (run.elapsed || 0) + Math.max(0, Math.floor((Date.now() - (run._recv || Date.now())) / 1000));
    var pct = (run.phase === 'transcribe' && run.pct != null) ? ' · ' + run.pct + '%' : '';
    var eta = '';
    if (run.phase === 'transcribe' && run.pct > 3) eta = ' · 남은 시간 약 ' + Math.max(1, Math.round(s * (100 - run.pct) / run.pct / 60)) + '분';
    return (run.note || '처리 중') + pct + ' · 경과 ' + hms(s) + eta;
  }
  setInterval(function () {
    var el = document.getElementById('mr-run'); if (!el) return;
    var run = RUNS[KEY];
    if (run && run.running) el.textContent = runLabel(run); else renderMain();
  }, 1000);
  function headerButtons() { return recording() ? '<canvas class="mr-wave sm" width="90" height="22" title="입력 파형"></canvas><button class="ic gr-rec" id="mr-stop" aria-pressed="true">■ 녹음 종료 <span id="mr-htime">' + elapsed() + '</span></button>' : '<button class="ic gr-rec" id="mr-start">● 녹음</button>'; }
  function bindHeader() { var s2 = $('mr-stop'); if (s2) s2.addEventListener('click', stop); var s1 = $('mr-start'); if (s1) s1.addEventListener('click', start); }
  function headerSub() { if (recording()) return '녹음 중 · ' + (R.meta.title || '회의명 미입력'); var run = RUNS[KEY]; if (run && run.running) return run.note; return '● 녹음 → 회의 정보 → 저장하면 전사·회의록·관계 추출까지 자동'; }
  function preview() { var e = evHist[KEY] && evHist[KEY][0]; if (recording()) return { t: '● 녹음 중 — ' + (R.meta.title || '회의명 미입력'), tm: elapsed() }; if (e) return { t: e.text, tm: e.ts.slice(0, 5) }; return { t: '녹음 → 전사 → 회의록 → 관계 추출', tm: '' }; }
  function drawerHtml() {
    var run = RUNS[KEY];
    var h = '<div class="id"><span class="ava lg" style="--c:#d98a8a"></span><b>미팅 기록자</b><small>meeting-recorder</small></div>';
    h += '<div><h3>역할</h3><p>회의 녹음을 받아 전사하고, 녹음 중 입력한 회의명·참석자·프로젝트·장소·일시를 권위 출처로 회의록을 작성해 60-Sources/meetings에 아카이브한다. 이어서 관계 추출 검토를 만들어 관계 컨펌 봇에 넘긴다.</p></div>';
    h += '<div><h3>상태</h3><div class="kv"><span>녹음</span><span>' + (recording() ? '<span style="color:var(--bad)">● ' + elapsed() + '</span>' : R.stopped && R.blob ? '종료 · 저장 대기' : '없음') + '</span></div><div class="kv"><span>파이프라인</span><span>' + (run ? esc(run.note) + ' · ' + esc(run.last) : '—') + '</span></div></div>';
    h += '<div><h3>파이프라인</h3><div class="ev"><div>① 오디오·meta 저장 → 00-Inbox/recordings/</div><div>② 전사 — record_worker.sh (mlx-whisper)</div><div>③ /ingest — 회의록 + 관계 검토 JSON</div><div>④ 관계 컨펌 봇에서 확정 → Neo4j</div></div></div>';
    h += '<div><h3>최근 녹음</h3><div id="mr-recs" class="ev">불러오는 중…</div></div>';
    return h;
  }
  function renderDrawer() {
    var d = $('dr'); if (!d) return; d.innerHTML = drawerHtml();
    api('recordings').then(function (list) { var el = $('mr-recs'); if (!el) return; el.innerHTML = list.length ? list.slice(0, 8).map(function (r) { return '<div><i>' + esc((r.recorded_at || '').slice(5, 16)) + '</i>' + esc(r.topic || r.id) + ' <span style="color:var(--tx3)">· ' + esc(r.status) + '</span></div>'; }).join('') : '<div>없음</div>'; }).catch(function () { var el = $('mr-recs'); if (el) el.textContent = '조회 실패'; });
  }
  function debugForm() { loadPeople(); R.startedAt = new Date(); R.t0 = Date.now() - 754000; R.stopped = true; R.blob = new Blob([new Uint8Array(1024)], { type: 'audio/webm' }); R.meta.attendees = ['사용자', '홍길동']; open(KEY); }
  return { KEY: KEY, start: start, debugForm: debugForm, stop: stop, recording: recording, ensureAgent: ensureAgent, renderChatBody: renderChatBody, headerButtons: headerButtons, bindHeader: bindHeader, headerSub: headerSub, preview: preview, renderDrawer: renderDrawer };
})();
