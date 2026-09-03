'use strict';
// ─────────────────────────────────────────────────────────────
// 관계 컨펌 봇 (confirm-bot) — review-ui(57900)의 관계 검토·인물 그래프·동의어 정리·녹음을
// Agent Office 셸 안에서 제공한다. API는 /graph-api/* (server.py 프록시) 로 호출한다.
// office.html의 전역( $, esc, md, add, scrollEnd, post, showDoc, showModal, closeModal,
// renderRows, renderMain, renderHeader, setMode, toggleDrawer, open, AG, sel, mode, drawerOn )을 사용한다.
//
// 구조
//   GR.state   — 현재 검토 문서(RV), 인물 확정 상태, 카드 진행, Inbox·검토 목록·그래프 캐시
//   GR.chat    — 카드 모드를 대화로 렌더 (질문 = 봇 말풍선, 선택지 = 인라인 버튼)
//   GR.table   — 표 모드 (1단계 인물 / 2단계 관계)
//   GR.graph   — 인물 관계 그래프 캔버스 (graph.html force 레이아웃 이식)
//   GR.syn     — 동의어 정리 카드
//   GR.rec     — 녹음 → 대화형 4단계 → 업로드
// ─────────────────────────────────────────────────────────────
var GR = (function () {
  var KEY = 'confirm-bot';
  var API = '/graph-api/';
  var RELS = ["ATTENDED","REPORTS_TO","WORKS_WITH","WORKS_ON","DECIDED","PRODUCED","ASSIGNED_TO","DISCUSSED","AFFECTS","BELONGS_TO","PART_OF"];
  var REL_KO = { ATTENDED:"참석", REPORTS_TO:"보고", WORKS_WITH:"협업", WORKS_ON:"프로젝트 참여", DECIDED:"결정", PRODUCED:"산출",
                 ASSIGNED_TO:"담당", DISCUSSED:"논의", AFFECTS:"영향", BELONGS_TO:"소속", PART_OF:"상위 조직" };
  var NODE_COLORS = { Person:'#7aa2f7', Organization:'#e48fb0', Meeting:'#e0a24a', Project:'#6fcf97', Concept:'#b58af9',
                      ActionItem:'#5fb8a6', Topic:'#8d97a8', Decision:'#e06a6a' };
  var LABEL_KO = { Person:'인물', Organization:'조직', Project:'프로젝트', Concept:'개념', Meeting:'회의', ActionItem:'액션', Topic:'주제', Decision:'결정' };

  var S = { reviews: [], inbox: [], graphStats: null, RV: null, inboxTimer: null, pollingInbox: false };
  function api(path, opt) { return fetch(API + path, opt || { cache: 'no-store' }).then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || j.detail || r.status); return j; }); }); }
  function apost(path, body) { return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); }
  function isMe() { return sel === KEY; }

  // ── 목록·요약 ──
  function loadLists() {
    return Promise.all([api('reviews').catch(function () { return []; }), api('inbox').catch(function () { return []; })]).then(function (r) {
      S.reviews = r[0] || []; S.inbox = r[1] || [];
      var running = S.inbox.some(function (it) { return it.job === 'running'; });
      if (running && !S.inboxTimer) S.inboxTimer = setInterval(function () { loadLists().then(function () { if (drawerOn && isMe()) renderDrawer(); }); }, 4000);
      if (!running && S.inboxTimer) { clearInterval(S.inboxTimer); S.inboxTimer = null; }
      renderRows();
      return S;
    });
  }
  function pendingCount() { return S.reviews.filter(function (r) { return r.status === 'pending'; }).length; }
  function preview() {
    var p = pendingCount();
    var applied = S.reviews.filter(function (r) { return r.status === 'applied'; });
    var last = applied.length ? applied[applied.length - 1] : null;
    if (S.RV && S.RV.data.status === 'pending') return { t: '검토 중 — ' + (S.RV.data.doc || S.RV.file), tm: '' };
    if (p) return { t: '검토 대기 ' + p + '건' + (last ? ' · 최근 적재 ' + (last.doc || last.file) : ''), tm: last && last.date ? last.date.slice(5) : '' };
    if (last) return { t: '적재 완료 — ' + (last.doc || last.file) + ' · 관계 ' + last.count + '건', tm: last.date ? last.date.slice(5) : '' };
    return { t: 'Neo4j 관계 검토 · 인물 그래프 · 동의어 정리', tm: '' };
  }

  // ── 검토 상태 (index.html 로직을 DOM 의존 없이 상태로 재구성) ──
  function bigrams(s) { if (s.length < 2) return [s]; var b = []; for (var i = 0; i < s.length - 1; i++) b.push(s.slice(i, i + 2)); return b; }
  function nameSim(a, b) { if (a === b) return 1; var A = bigrams(a), B0 = bigrams(b), B = B0.slice(), hit = 0;
    A.forEach(function (g) { var j = B.indexOf(g); if (j >= 0) { hit++; B.splice(j, 1); } }); return (2 * hit) / (A.length + B0.length); }

  function openReview(file) {
    return api('reviews/' + encodeURIComponent(file)).then(function (data) {
      var rels = data.relations || [], pending = data.status === 'pending';
      var RV = { file: file, data: data, pending: pending, rels: rels, persons: [], orgs: [], speakerMap: {}, currentPersons: [],
                 entityState: {}, entityMeta: {}, selected: {}, types: {}, keys: {}, override: {}, cards: [], cardIdx: 0, answered: {}, applied: null };
      (data.speakers || []).forEach(function (sp) { RV.speakerMap[sp.id] = sp; });
      var seen = {};
      rels.forEach(function (r, i) {
        [r.from, r.to].forEach(function (n) { if (n.label === 'Person' && !seen[n.key]) { seen[n.key] = 1; RV.currentPersons.push(n.key); } });
        RV.selected[i] = pending && r.confidence !== 'implied';
        RV.types[i] = r.type; RV.keys[i] = { from: r.from.key, to: r.to.key };
      });
      var p2 = pending && RV.currentPersons.length ? Promise.all([api('persons').catch(function () { return []; }), api('organizations').catch(function () { return []; })]) : Promise.resolve([[], []]);
      return p2.then(function (r) {
        RV.persons = r[0] || []; RV.orgs = r[1] || [];
        RV.currentPersons.forEach(function (k, i) {
          var exact = RV.persons.filter(function (p) { return p.name === k || (p.aliases || []).indexOf(k) >= 0; })[0];
          var best = null; if (!exact) RV.persons.forEach(function (p) { var s = nameSim(k, p.name); if (s >= 0.5 && (!best || s > best.s)) best = { name: p.name, s: s }; });
          var orgExact = RV.orgs.filter(function (o) { return o.name === k || (o.aliases || []).indexOf(k) >= 0; })[0];
          var orgBest = orgExact ? { name: orgExact.name, s: 1 } : null;
          if (!orgBest) RV.orgs.forEach(function (o) { var s = nameSim(k, o.name); if (s >= 0.5 && (!orgBest || s > orgBest.s)) orgBest = { name: o.name, s: s }; });
          if (exact && !orgExact) RV.entityState[k] = { mode: 'map', value: exact.name };
          RV.entityMeta[k] = { i: i, exact: !!exact, best: best, orgBest: orgBest, orgExact: !!orgExact };
        });
        S.RV = RV; buildCards(); return RV;
      });
    });
  }
  function resolveKey(k) { var s = S.RV.entityState[k]; if (!s) return k; if (s.mode === 'drop') return null; if ((s.mode === 'map' || s.mode === 'rename' || s.mode === 'org') && s.value) return s.value; return k; }
  function resolveLabel(k) { var s = S.RV.entityState[k]; return s && s.mode === 'org' ? 'Organization' : 'Person'; }
  function setEntity(k, mode, value) { S.RV.entityState[k] = { mode: mode, value: value == null ? null : value }; refreshGate(); }
  function undecided() { return S.RV.currentPersons.filter(function (k) { var s = S.RV.entityState[k]; return !s || (s.mode === 'rename' && !s.value); }); }
  function overrideOf(i, side) { var o = S.RV.override[i]; return o && o[side] ? o[side] : null; }
  function setOverride(i, side, value) { var v = String(value || '').trim(); if (!v) return false; (S.RV.override[i] = S.RV.override[i] || {})[side] = v; S.RV.keys[i][side] = v; return true; }
  function dropped(i) { var r = S.RV.rels[i]; return [{ n: r.from, s: 'from' }, { n: r.to, s: 'to' }].some(function (e) { return e.n.label === 'Person' && !overrideOf(i, e.s) && resolveKey(e.n.key) === null; }); }
  function refreshGate() { S.RV.rels.forEach(function (r, i) { if (dropped(i)) S.RV.selected[i] = false; }); }
  function endpointKey(n, i, side) { var ov = overrideOf(i, side); if (ov) return ov; if (n.label === 'Person') { var v = resolveKey(n.key); return v === null ? null : v; } return S.RV.keys[i][side]; }
  function collect() {
    var out = [];
    S.RV.rels.forEach(function (r, i) {
      if (!S.RV.selected[i] || dropped(i)) return;
      out.push(Object.assign({}, r, { type: S.RV.types[i],
        from: Object.assign({}, r.from, { key: endpointKey(r.from, i, 'from'), label: r.from.label === 'Person' ? resolveLabel(r.from.key) : r.from.label }),
        to: Object.assign({}, r.to, { key: endpointKey(r.to, i, 'to'), label: r.to.label === 'Person' ? resolveLabel(r.to.key) : r.to.label }) }));
    });
    return out;
  }
  function apply() {
    var RV = S.RV, relations = collect();
    if (!relations.length) return Promise.reject(new Error('선택된 관계가 없습니다'));
    if (undecided().length) return Promise.reject(new Error('인물 확정이 끝나지 않았습니다 — 미확정 ' + undecided().join(', ')));
    var aliases = {};
    RV.currentPersons.forEach(function (k) { if (RV.speakerMap[k]) return; var res = resolveKey(k); if (res && res !== k) { (aliases[res] = aliases[res] || { label: resolveLabel(k), variants: [] }).variants.push(k); } });
    return apost('apply', { file: RV.file, relations: relations, aliases: aliases }).then(function (out) {
      if (!out.ok) throw new Error(out.error + (out.detail ? ' — ' + out.detail : ''));
      RV.applied = out; RV.pending = false; RV.data.status = 'applied';
      return loadLists().then(function () { return out; });
    });
  }

  // ── 카드 (질문 순서) ──
  function buildCards() {
    var RV = S.RV, cards = [];
    RV.currentPersons.forEach(function (k) {
      if (RV.entityState[k]) return;
      var m = RV.entityMeta[k];
      cards.push(RV.speakerMap[k] ? { kind: 'speaker', k: k, sp: RV.speakerMap[k] } : { kind: 'entity', k: k, best: m.best, orgBest: m.orgBest, orgExact: m.orgExact });
    });
    cards.push({ kind: 'batch' });
    RV.rels.forEach(function (r, i) { if (r.confidence === 'implied') cards.push({ kind: 'implied', r: r, i: i }); });
    cards.push({ kind: 'final' });
    RV.cards = cards; RV.cardIdx = 0;
  }
  function nodeTitle(n) { var p = n.props || {}, t = p.desc || p.summary || p.topic || p.name; return t ? (t.length > 36 ? t.slice(0, 36) + '…' : t) : n.key; }
  function descOf(n) { var d = n.props && (n.props.desc || n.props.summary); return d ? (d.length > 42 ? d.slice(0, 42) + '…' : d) : null; }
  function relLabel(r, i) {
    var from = r.from.label === 'Person' ? resolveKey(r.from.key) : r.from.key, to = r.to.label === 'Person' ? resolveKey(r.to.key) : r.to.key;
    if (from === null || to === null) return null;
    if (r.type === 'ATTENDED') return from;
    if (r.type === 'PRODUCED') return descOf(r.to) || to;
    return (descOf(r.from) || from) + ' → ' + (descOf(r.to) || to);
  }

  // ── 대화 렌더 ──
  var C = {};   // 대화 화면 상태: 현재 질문의 action row
  function personOpts(list, selectedName) { return list.map(function (p) { return '<option value="' + esc(p.name) + '"' + (p.name === selectedName ? ' selected' : '') + '>' + esc(p.name) + (p.team ? ' (' + esc(p.team) + ')' : '') + '</option>'; }).join(''); }
  function orgOpts(k, pre) { return '<option value="__new__">신규 조직 \'' + esc(k) + '\'</option>' + S.RV.orgs.map(function (o) { return '<option value="' + esc(o.name) + '"' + (pre && o.name === pre ? ' selected' : '') + '>' + esc(o.name) + (o.kind === 'external' ? ' (외부)' : '') + '</option>'; }).join(''); }
  function botHtml(html) { var d = add('bot', '', { plain: true, force: true }); if (d) d.innerHTML = html; return d; }
  function actRow(html) { var w = $('wrap'); if (!w) return null; var d = document.createElement('div'); d.className = 'act'; d.innerHTML = html; w.appendChild(d); scrollEnd(true); C.row = d; return d; }
  function answer(text) { if (C.row) { C.row.remove(); C.row = null; } if (text) { add('user', text, { force: true }); if (S.RV) S.RV.answered[S.RV.cardIdx] = text; } }
  function prevCard() { var RV = S.RV; if (RV.cardIdx <= 0) return; if (C.row) { C.row.remove(); C.row = null; } add('user', '← 이전 질문', { force: true }); RV.cardIdx--; renderCard(); }
  function progress() { var RV = S.RV, w = $('wrap'); if (!w) return; var old = w.querySelector('.gr-prog'); if (old) old.remove();
    var d = document.createElement('div'); d.className = 'sys gr-prog'; d.style.fontFamily = 'var(--mono)'; d.style.fontSize = '10.5px';
    var sel2 = 0; RV.rels.forEach(function (_, i) { if (RV.selected[i] && !dropped(i)) sel2++; });
    d.textContent = '질문 ' + Math.min(RV.cardIdx + 1, RV.cards.length) + ' / ' + RV.cards.length + ' · 인물 ' + (RV.currentPersons.length - undecided().length) + '/' + RV.currentPersons.length + ' 확정 · 관계 ' + sel2 + ' 선택'; w.appendChild(d); }
  function nextCard() { var RV = S.RV; RV.cardIdx = Math.min(RV.cardIdx + 1, RV.cards.length - 1); renderCard(); }
  function renderCard() {
    var RV = S.RV; if (!RV || !isMe() || mode !== 'chat') return;
    var c = RV.cards[RV.cardIdx];
    if (RV.answered[RV.cardIdx] && c.kind !== 'final') add('sys', '이전 답: ' + RV.answered[RV.cardIdx] + ' — 다시 고르면 덮어씁니다');
    if (c.kind === 'speaker') {
      var sp = c.sp, cands = (sp.candidates || []).filter(Boolean), quotes = (sp.quotes || []).filter(Boolean);
      var done = []; RV.currentPersons.forEach(function (k) { var s = RV.entityState[k]; if (RV.speakerMap[k] && s && s.value && done.indexOf(s.value) < 0) done.push(s.value); });
      botHtml((sp.address_hint ? '<div class="gr-hint">' + esc(sp.address_hint) + '</div>' : '') +
        '<div class="gr-q">이 화자는 누구인가요? <span class="qtype">' + esc(c.k) + (sp.utterance_count != null ? ' · 발화 ' + sp.utterance_count + '회' : '') + (sp.share != null ? ' · 전체의 ' + Math.round(sp.share * 100) + '%' : '') + '</span></div>' +
        (sp.summary ? '<div class="gr-ev">' + esc(sp.summary) + '</div>' : '') +
        (quotes.length ? '<ul class="gr-quotes">' + quotes.map(function (q) { return '<li>' + esc(q) + '</li>'; }).join('') + '</ul>' : ''));
      var row = actRow(cands.map(function (n, j) { return '<button class="pri" data-c="' + j + '">' + esc(n) + '</button>'; }).join('') +
        '<button data-drop>모르겠음 — 미상으로</button>' +
        (done.length ? '<span class="more">앞에서 확인한 사람: <select data-same>' + done.map(function (n) { return '<option>' + esc(n) + '</option>'; }).join('') + '</select><button data-samebtn>같은 사람</button></span>' : '') +
        (RV.persons.length ? '<span class="more">다른 사람: <select data-other>' + personOpts(RV.persons) + '</select><button data-otherbtn>이 사람</button></span>' : '') +
        '<span class="more">명단에 없음: <input data-new placeholder="이름" size="8"><button data-newbtn>등록</button></span>');
      row.querySelectorAll('[data-c]').forEach(function (b) { b.addEventListener('click', function () { speaker(c.k, cands[+b.dataset.c]); }); });
      row.querySelector('[data-drop]').addEventListener('click', function () { setEntity(c.k, 'drop'); answer('모르겠음 — 미상'); nextCard(); });
      var sb = row.querySelector('[data-samebtn]'); if (sb) sb.addEventListener('click', function () { speaker(c.k, row.querySelector('[data-same]').value); });
      var ob = row.querySelector('[data-otherbtn]'); if (ob) ob.addEventListener('click', function () { speaker(c.k, row.querySelector('[data-other]').value); });
      row.querySelector('[data-newbtn]').addEventListener('click', function () { speaker(c.k, row.querySelector('[data-new]').value); });
      row.querySelector('[data-new]').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); speaker(c.k, e.target.value); } });
    } else if (c.kind === 'entity') {
      var cand = c.best, others = RV.persons.filter(function (p) { return !cand || p.name !== cand.name; });
      var q = c.orgExact ? '<b>' + esc(c.k) + '</b> — 조직도에 있는 <b>부서/조직명</b>과 일치해요. 인물이 아니라 조직으로 기록할까요?'
        : cand ? '<b>' + esc(c.k) + '</b> — 기존에 알고 있는 <b>' + esc(cand.name) + '</b>과 같은 사람인가요? <span class="qtype">유사도 ' + Math.round(cand.s * 100) + '%</span>'
        : '<b>' + esc(c.k) + '</b> — 처음 보는 이름이에요. 어떻게 할까요?';
      botHtml('<div class="gr-q">' + q + '</div>');
      var row2 = actRow((c.orgExact ? '<button class="pri" data-org>조직으로 기록</button>' : '') +
        (cand ? '<button class="' + (c.orgExact ? '' : 'pri') + '" data-map>같은 사람이에요</button>' : '') +
        '<button class="' + (cand || c.orgExact ? '' : 'pri') + '" data-new>새 인물로 등록</button><button data-drop>제외</button>' +
        '<span class="more">인물이 아니에요: <select data-orgsel>' + orgOpts(c.k, c.orgBest && c.orgBest.name) + '</select><button data-orgbtn>부서/기관으로</button></span>' +
        (others.length ? '<span class="more">다른 사람이에요: <select data-other>' + personOpts(others) + '</select><button data-otherbtn>이 사람으로</button></span>' : '') +
        '<span class="more">표기가 틀렸어요: <input data-rn placeholder="올바른 이름" size="8"><button data-rnbtn>수정</button></span>');
      var mp = row2.querySelector('[data-map]'); if (mp) mp.addEventListener('click', function () { setEntity(c.k, 'map', cand.name); answer('같은 사람 — ' + cand.name); nextCard(); });
      row2.querySelector('[data-new]').addEventListener('click', function () { setEntity(c.k, 'new'); answer('새 인물로 등록'); nextCard(); });
      row2.querySelector('[data-drop]').addEventListener('click', function () { setEntity(c.k, 'drop'); answer('제외'); nextCard(); });
      function toOrg() { var v = row2.querySelector('[data-orgsel]').value; setEntity(c.k, 'org', v === '__new__' ? null : v); answer('조직으로 — ' + (v === '__new__' ? '신규 ' + c.k : v)); nextCard(); }
      var og = row2.querySelector('[data-org]'); if (og) og.addEventListener('click', toOrg);
      row2.querySelector('[data-orgbtn]').addEventListener('click', toOrg);
      var ot = row2.querySelector('[data-otherbtn]'); if (ot) ot.addEventListener('click', function () { var v = row2.querySelector('[data-other]').value; setEntity(c.k, 'map', v); answer('다른 사람 — ' + v); nextCard(); });
      function rn() { var v = row2.querySelector('[data-rn]').value.trim(); if (!v) return; setEntity(c.k, 'rename', v); answer('표기 수정 → ' + v); nextCard(); }
      row2.querySelector('[data-rnbtn]').addEventListener('click', rn);
      row2.querySelector('[data-rn]').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); rn(); } });
    } else if (c.kind === 'batch') {
      var groups = {};
      RV.rels.forEach(function (r, i) { if (r.confidence === 'implied') return; var lb = relLabel(r, i); if (lb === null) return;
        (groups[r.type] = groups[r.type] || []).push('<button class="chip gr-chip' + (RV.selected[i] ? '' : ' off') + '" data-i="' + i + '" title="' + esc(r.evidence || '') + '">' + esc(lb) + '</button>'); });
      var list = Object.keys(groups).map(function (t) { return '<div><b>' + esc(REL_KO[t] || t) + '</b><div class="chips">' + groups[t].join('') + '</div></div>'; }).join('') || '<div class="gr-ev">근거가 명시된 관계가 없습니다</div>';
      var d = botHtml('<div class="gr-q">근거가 명시된 관계는 이렇게 정리했어요. <span class="qtype">아닌 항목은 눌러서 빼세요 (다시 누르면 복구)</span></div><div class="gr-rgrp">' + list + '</div>');
      d.querySelectorAll('.gr-chip').forEach(function (b) { b.addEventListener('click', function () { var i = +b.dataset.i; if (!RV.pending) return; RV.selected[i] = !RV.selected[i]; b.classList.toggle('off', !RV.selected[i]); progress(); }); });
      var row3 = actRow('<button class="pri" data-go>이대로 진행</button>');
      row3.querySelector('[data-go]').addEventListener('click', function () { d.querySelectorAll('.gr-chip').forEach(function (b) { b.disabled = true; b.style.cursor = 'default'; }); answer('이대로 진행'); nextCard(); });
    } else if (c.kind === 'implied') {
      var r = c.r, from = r.from.label === 'Person' ? resolveKey(r.from.key) : r.from.key, to = r.to.label === 'Person' ? resolveKey(r.to.key) : r.to.key;
      if (from === null || to === null) { nextCard(); return; }
      botHtml('<div class="gr-q">' + (r.question ? esc(r.question) : '<b>' + esc(from) + '</b> — ' + esc(REL_KO[r.type] || r.type) + ' → <b>' + esc(to) + '</b> 관계가 정황상 추정돼요. 기록할까요?') + '</div><div class="gr-ev">근거: ' + esc(r.evidence || '—') + '</div>');
      var ends = [{ side: 'from', n: r.from, name: overrideOf(c.i, 'from') || from }, { side: 'to', n: r.to, name: overrideOf(c.i, 'to') || to }];
      var row4 = actRow('<button class="pri" data-yes>기록할게요</button><button data-no>건너뛰기</button>' +
        '<span class="more">대상이 틀렸어요: <select data-side>' + ends.map(function (e) { return '<option value="' + e.side + '">' + esc(overrideOf(c.i, e.side) ? e.name : nodeTitle(e.n)) + ' (' + esc(LABEL_KO[e.n.label] || e.n.label) + ')</option>'; }).join('') + '</select> 대신 →</span>' +
        '<span class="more chips" data-cands></span>' +
        '<span class="more"><input data-alt placeholder="직접 입력" size="10"><button data-altbtn>교체 후 기록</button></span>');
      function curSide() { return row4.querySelector('[data-side]').value; }
      function swapTo(name) { var side = curSide(), e = ends.filter(function (x) { return x.side === side; })[0]; if (!setOverride(c.i, side, name)) return; RV.selected[c.i] = true; answer(e.name + ' 대신 ' + String(name).trim() + ' — 기록'); nextCard(); }
      function fillCands() {
        var side = curSide(), e = ends.filter(function (x) { return x.side === side; })[0], names = [], shown = {};
        if (e.n.label === 'Person') RV.currentPersons.forEach(function (k) { var v = resolveKey(k); if (v && names.indexOf(v) < 0) names.push(v); });
        else RV.rels.forEach(function (x) { [x.from, x.to].forEach(function (n) { if (n.label === e.n.label && names.indexOf(n.key) < 0) { names.push(n.key); shown[n.key] = nodeTitle(n); } }); });
        if (e.n.label === 'Organization') RV.orgs.forEach(function (o) { if (names.indexOf(o.name) < 0) names.push(o.name); });
        names = names.filter(function (v) { return v !== e.name; }).slice(0, 12);
        var box = row4.querySelector('[data-cands]');
        box.innerHTML = names.length ? names.map(function (v) { return '<button class="chip gr-chip" data-v="' + esc(v) + '" title="' + esc(v) + '">' + esc(shown[v] || v) + '</button>'; }).join('') : '<span class="gr-ev">후보 없음 — 직접 입력</span>';
        box.querySelectorAll('[data-v]').forEach(function (b) { b.addEventListener('click', function () { swapTo(b.dataset.v); }); });
      }
      fillCands();
      row4.querySelector('[data-side]').addEventListener('change', fillCands);
      row4.querySelector('[data-yes]').addEventListener('click', function () { RV.selected[c.i] = true; answer('기록'); nextCard(); });
      row4.querySelector('[data-no]').addEventListener('click', function () { RV.selected[c.i] = false; answer('건너뛰기'); nextCard(); });
      row4.querySelector('[data-altbtn]').addEventListener('click', function () { swapTo(row4.querySelector('[data-alt]').value); });
      row4.querySelector('[data-alt]').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); swapTo(e.target.value); } });
    } else {
      var n = collect().length, total = RV.rels.length, und = undecided();
      botHtml('<div class="gr-q">확인 끝났어요. 관계 <b>' + n + '건</b>을 기록하고 ' + (total - n) + '건은 제외할게요.' + (und.length ? '<div class="gr-hint">미확정 인물 ' + esc(und.join(', ')) + ' — 표 모드에서 확정해야 적재할 수 있어요</div>' : '') + '</div>');
      var row5 = actRow('<button class="pri" data-apply' + (und.length || !n ? ' disabled' : '') + '>Neo4j 적재</button><button data-table>표 모드로 검토</button><button data-reject>전체 반려</button>');
      row5.querySelector('[data-apply]').addEventListener('click', function () { row5.querySelector('[data-apply]').disabled = true; doApply(); });
      row5.querySelector('[data-table]').addEventListener('click', function () { setMode('gr-table'); });
      row5.querySelector('[data-reject]').addEventListener('click', function () { if (!confirm('이 문서의 관계 추출을 전체 반려할까요?')) return; apost('reject', { file: RV.file }).then(function () { RV.pending = false; RV.data.status = 'rejected'; answer('전체 반려'); add('sys', '반려 처리됨 — ' + (RV.data.doc || RV.file)); loadLists().then(function () { if (drawerOn) renderDrawer(); }); }); });
    }
    if (RV.cardIdx > 0 && C.row) { var bk = document.createElement('button'); bk.className = 'gr-back'; bk.textContent = '← 이전 질문'; bk.title = '앞 질문으로 돌아가 답을 고칩니다'; bk.addEventListener('click', prevCard); C.row.appendChild(bk); }
    progress();
  }
  function speaker(k, name) { var v = String(name || '').trim(); if (!v) return; if (S.RV.persons.some(function (p) { return p.name === v; })) setEntity(k, 'map', v); else setEntity(k, 'rename', v); answer(v); nextCard(); }
  function doApply() {
    answer('Neo4j 적재');
    var w = add('sys', '적재 중…');
    apply().then(function (out) { w.remove(); add('sys', '적재 완료 — ' + out.applied + '건 (익명 노드 검사 ' + out.anon_nodes + (out.attendees_patched ? ' · 회의록 attendees 갱신됨' : '') + ')');
      botHtml('<div class="gr-q">기록했어요. 다음 검토 문서는 오른쪽 ⓘ 목록에서 고르거나, 그래프에서 확인할 수 있어요.</div>');
      renderRows(); renderHeader(); if (drawerOn) renderDrawer(); })
      .catch(function (e) { w.remove(); add('sys', '적재 실패 — ' + e.message); });
  }

  function startReview(file) {
    return openReview(file).then(function (RV) {
      if (!isMe()) { open(KEY); return; }
      if (mode !== 'chat') setMode('chat'); else renderMain();   // 대화를 처음부터 다시 그린다 (이력 + 검토 시작)
    }).catch(function (e) { add('sys', '검토를 열 수 없습니다 — ' + e.message); });
  }
  function renderChatBody() {
    var w = $('wrap'); if (!w) return;
    var RV = S.RV;
    if (!RV) {
      var p = pendingCount();
      add('sys', p ? '검토 대기 ' + p + '건 — 오른쪽 ⓘ 목록에서 문서를 고르면 여기서 확인 질문이 시작됩니다' : '대기 중인 검토가 없습니다. Inbox에 문서를 넣고 ingest를 실행하면 관계 추출 결과가 여기 올라옵니다');
      add('sys', '입력창에 자연어로 물으면 그래프를 조회해 답합니다 — 예: "김도연이 참석한 회의는?"');
      if (!drawerOn) toggleDrawer(true);
      return;
    }
    add('sys', '검토 ' + (RV.pending ? '시작' : '(' + (RV.data.status === 'applied' ? '적재됨' : '반려') + ')') + ' — ' + (RV.data.doc || RV.file) + (RV.data.date ? ' · ' + RV.data.date : '') + ' · 관계 ' + RV.rels.length + '건' + (Object.keys(RV.speakerMap).length ? ' · 화자 ' + Object.keys(RV.speakerMap).length + '명' : ''));
    if (!RV.pending) { var sel2 = 0; RV.rels.forEach(function (_, i) { if (RV.selected[i]) sel2++; }); botHtml('<div class="gr-q">이 문서는 이미 ' + (RV.data.status === 'applied' ? '적재' : '반려') + '되었어요. 내용은 [표 모드]에서 볼 수 있어요.</div>'); actRow('<button data-table>표 모드로 보기</button>').querySelector('[data-table]').addEventListener('click', function () { setMode('gr-table'); }); return; }
    // 자동 매칭된 인물 안내
    var auto = RV.currentPersons.filter(function (k) { return RV.entityState[k] && RV.entityState[k].mode === 'map'; });
    if (auto.length) add('sys', '자동 매칭 — ' + auto.map(function (k) { return k === RV.entityState[k].value ? k : k + '→' + RV.entityState[k].value; }).join(', '));
    // 이미 답한 카드는 요약으로 재생 (모드 전환 후 복귀 시)
    RV.cardIdx = Math.min(RV.cardIdx, RV.cards.length - 1);
    renderCard();
  }

  // ── 드로어 ──
  function drawerHtml() {
    var RV = S.RV;
    var h = '<div class="id"><span class="ava lg gr-ava" style="--c:#c9b45c"><i style="left:14px;top:16px"></i><i style="left:34px;top:22px"></i><i style="left:22px;top:36px"></i></span><b>관계 컨펌 봇</b><small>confirm-bot · jarvis-neo4j</small></div>';
    h += '<div><h3>Inbox — 처리 대기 ' + S.inbox.filter(function (x) { return !x.gone; }).length + '</h3>';
    h += S.inbox.length ? S.inbox.map(function (it) { var st = it.job === 'running' ? '<span class="chip warn">분석 중…</span>' : it.job === 'done' ? '<span class="chip ok">완료</span>' : it.job === 'failed' ? '<span class="chip" style="color:var(--bad)">실패</span>' : it.gone ? '<span class="chip">아카이브</span>' : '<button class="chip" data-ingest="' + esc(it.file) + '">ingest</button>';
      return '<div class="gr-doc"><span class="gr-st ' + (it.job === 'running' ? 'p' : it.job === 'done' ? 'a' : '') + '"></span><span class="gr-t" title="' + esc(it.file) + '">' + esc(it.file) + '</span>' + st + '</div>'; }).join('') : '<div class="ev">비어 있음 — 00-Inbox/에 문서를 넣으면 나타난다</div>';
    h += '</div>';
    var pend = S.reviews.filter(function (r) { return r.status === 'pending'; }), rest = S.reviews.filter(function (r) { return r.status !== 'pending'; }).reverse();
    h += '<div><h3>검토 목록 · 대기 ' + pend.length + '</h3>' + pend.concat(rest).map(function (r) {
      var ttl = (r.doc || r.file) + (r.date ? ' · ' + r.date : '') + ' · 관계 ' + r.count + '건 · ' + (r.status === 'pending' ? '대기' : r.status === 'applied' ? '적재됨' : '반려') + '\n' + r.file;
      return '<div class="gr-row' + (RV && RV.file === r.file ? ' on' : '') + '"><button class="gr-doc" data-open="' + esc(r.file) + '" title="' + esc(ttl) + '"><span class="gr-st ' + (r.status === 'pending' ? 'p' : r.status === 'applied' ? 'a' : 'r') + '"></span><span class="gr-t">' + esc(r.doc || r.file) + '</span><span class="gr-n">' + r.count + '</span></button>' + (r.status === 'pending' ? '<button class="gr-del" data-del="' + esc(r.file) + '" title="검토 목록에서 삭제 (.cache/review/trash로 이동)">✕</button>' : '') + '</div>'; }).join('') + (S.reviews.length ? '' : '<div class="ev">검토 없음</div>') + '</div>';
    if (RV) h += '<div><h3>이 문서</h3><div class="kv"><span>출처</span><span style="font-family:var(--mono);font-size:10.5px;max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(RV.data.source || '') + '">' + esc((RV.data.source || '-').split('/').slice(-1)[0]) + '</span></div><div class="kv"><span>화자 / 인물</span><span>' + Object.keys(RV.speakerMap).length + ' / ' + RV.currentPersons.length + '</span></div><div class="kv"><span>직접 / 정황</span><span>' + RV.rels.filter(function (r) { return r.confidence !== 'implied'; }).length + ' / ' + RV.rels.filter(function (r) { return r.confidence === 'implied'; }).length + '</span></div>' + (RV.pending ? '<button class="run sec2" data-del style="margin-top:8px">이 검토 삭제</button>' : '') + '</div>';
    var g = S.graphStats; if (g) h += '<div><h3>그래프</h3>' + Object.keys(g).sort().map(function (l) { return '<div class="kv"><span><i class="dot" style="background:' + (NODE_COLORS[l] || '#777') + '"></i>' + esc(LABEL_KO[l] || l) + '</span><span>' + g[l] + '</span></div>'; }).join('') + '</div>';
    return h;
  }
  function renderDrawer() {
    var d = $('dr'); if (!d) return; d.innerHTML = drawerHtml();
    d.querySelectorAll('[data-open]').forEach(function (b) { b.addEventListener('click', function () { startReview(b.dataset.open).then(function () { renderDrawer(); }); }); });
    d.querySelectorAll('[data-del]').forEach(function (b) { b.addEventListener('click', function (e) { e.stopPropagation(); var f = b.dataset.del, r = S.reviews.filter(function (x) { return x.file === f; })[0];
      if (!confirm('검토 목록에서 삭제할까요?\n' + ((r && r.doc) || f) + '\n\n(파일은 .cache/review/trash/로 이동되어 복구할 수 있습니다)')) return;
      apost('delete', { file: f }).then(function () { if (S.RV && S.RV.file === f) S.RV = null; add('sys', '검토 삭제 — ' + ((r && r.doc) || f)); return loadLists(); }).then(function () { renderDrawer(); if (isMe() && !S.RV) renderMain(); }).catch(function (err) { alert('삭제 실패 — ' + err.message); }); }); });
    d.querySelectorAll('[data-ingest]').forEach(function (b) { b.addEventListener('click', function () { b.disabled = true; b.textContent = '시작…'; apost('ingest', { file: b.dataset.ingest }).then(function (o) { if (!o.ok) alert(o.error); return loadLists(); }).then(renderDrawer).catch(function (e) { alert('ingest 실패 — ' + e.message); renderDrawer(); }); }); });
    var del = d.querySelector('[data-del]'); if (del) del.addEventListener('click', function () { var RV = S.RV; if (!confirm("'" + (RV.data.doc || RV.file) + "' 검토를 목록에서 지울까요?\n추출된 관계가 사라집니다 (.cache/review/trash/로 이동)")) return; apost('delete', { file: RV.file }).then(function () { S.RV = null; return loadLists(); }).then(function () { renderDrawer(); if (mode === 'chat') renderChatBody(); else setMode('chat'); }); });
    if (!S.graphStats) api('graph').then(function (g) { var st = {}; (g.nodes || []).forEach(function (n) { st[n.label] = (st[n.label] || 0) + 1; }); S.graphStats = st; if (drawerOn && isMe()) renderDrawer(); }).catch(function () {});
  }

  // ── 표 모드 ──
  function renderTable() {
    var RV = S.RV, body = $('body');
    if (!RV) { body.innerHTML = '<div class="empty">검토할 문서를 ⓘ 목록에서 선택하세요</div>'; return; }
    var pending = RV.pending;
    var ent = RV.currentPersons.map(function (k, i) {
      var s = RV.entityState[k] || {}, m = RV.entityMeta[k], occ = RV.rels.filter(function (r) { return (r.from.label === 'Person' && r.from.key === k) || (r.to.label === 'Person' && r.to.key === k); }).length;
      var opt = function (mode2, label) { return '<button class="chip' + (s.mode === mode2 ? '' : ' off') + '" data-k="' + i + '" data-m="' + mode2 + '"' + (pending ? '' : ' disabled') + '>' + label + '</button>'; };
      var pre = s.mode === 'map' && s.value ? s.value : m.best ? m.best.name : (RV.persons[0] || {}).name;
      return '<tr><td><b>' + esc(k) + '</b>' + (RV.speakerMap[k] ? '<div class="ev">' + esc(RV.speakerMap[k].summary || '') + '</div>' : '') + '</td><td class="gr-mono">관계 ' + occ + '</td><td><div class="chips">' +
        opt('new', '신규') + opt('map', '기존 매핑') + '<select data-mapsel="' + i + '"' + (pending ? '' : ' disabled') + '>' + personOpts(RV.persons, pre) + '</select>' +
        opt('rename', '이름 수정') + '<input data-rn="' + i + '" placeholder="' + esc(k) + '" value="' + esc(s.mode === 'rename' && s.value ? s.value : '') + '" size="8"' + (pending ? '' : ' disabled') + '>' +
        opt('org', '조직') + '<select data-orgsel="' + i + '"' + (pending ? '' : ' disabled') + '>' + orgOpts(k, s.mode === 'org' ? s.value : (m.orgBest && m.orgBest.name)) + '</select>' +
        opt('drop', '제외') + '</div></td><td>' + (s.mode === 'map' ? '<span class="conf d">매핑 → ' + esc(s.value) + '</span>' : s.mode === 'new' ? '<span class="conf d">신규</span>' : s.mode === 'org' ? '<span class="conf d">조직' + (s.value ? ' → ' + esc(s.value) : '') + '</span>' : s.mode === 'rename' ? (s.value ? '<span class="conf d">→ ' + esc(s.value) + '</span>' : '<span class="conf i">이름 입력</span>') : s.mode === 'drop' ? '<span class="conf" style="color:var(--tx3)">제외</span>' : '<span class="conf i">미확정</span>') + '</td></tr>'; }).join('');
    var rows = RV.rels.map(function (r, i) {
      var dr = dropped(i), on = RV.selected[i] && !dr;
      var cell = function (n, side) { var isP = n.label === 'Person'; var val = overrideOf(i, side) || (isP ? (resolveKey(n.key) === null ? n.key : resolveKey(n.key)) : RV.keys[i][side]); return '<span class="lbl">' + esc(isP ? resolveLabel(n.key) : n.label) + '</span><input data-key="' + i + '" data-side="' + side + '" value="' + esc(val) + '"' + (pending && !isP ? '' : ' disabled') + '>'; };
      return '<tr class="' + (on ? '' : 'ex') + '"><td><input type="checkbox" data-chk="' + i + '"' + (on ? ' checked' : '') + (pending && !dr ? '' : ' disabled') + '></td><td>' + cell(r.from, 'from') + '</td><td><select data-ty="' + i + '"' + (pending ? '' : ' disabled') + '>' + RELS.map(function (t) { return '<option value="' + t + '"' + (t === RV.types[i] ? ' selected' : '') + '>' + (REL_KO[t] || t) + ' · ' + t + '</option>'; }).join('') + '</select></td><td>' + cell(r.to, 'to') + '</td><td><span class="conf ' + (r.confidence === 'implied' ? 'i' : 'd') + '">' + (r.confidence === 'implied' ? '정황' : '직접') + '</span></td><td class="gr-ev2">' + esc(r.evidence || '') + '</td></tr>'; }).join('');
    var und = undecided();
    body.innerHTML = '<div class="gr-tbl">' +
      (RV.currentPersons.length && pending ? '<div class="gr-stage">1단계 — 인물 확정 <span>· 기존 그래프 인물 ' + RV.persons.length + '명과 대조 · 매핑/수정 시 원 표기는 aliases로 보존</span></div><div style="overflow-x:auto"><table><thead><tr><th style="width:150px">추출된 이름</th><th style="width:70px">등장</th><th>처리 방법</th><th style="width:120px">상태</th></tr></thead><tbody>' + ent + '</tbody></table></div>' +
        '<div class="gr-lock' + (und.length ? '' : ' ok') + '">' + (und.length ? '인물 확정이 끝나지 않았습니다 — 미확정 ' + und.length + '명 (' + esc(und.join(', ')) + ') · 확정 전에는 적재할 수 없습니다' : '인물 확정 완료 — 확정된 이름이 아래 관계에 반영되었습니다') + '</div>' : '') +
      '<div class="gr-stage">2단계 — 관계 검토 <span>· ' + esc(RV.data.doc || RV.file) + ' · ' + (RV.data.status === 'pending' ? '대기' : RV.data.status === 'applied' ? '적재됨' : '반려') + '</span></div><div style="overflow-x:auto"><table><thead><tr><th style="width:40px">적재</th><th>From</th><th style="width:170px">관계</th><th>To</th><th style="width:60px">근거</th><th>근거 문장</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<div class="gr-tbar"><button class="pri" data-apply' + (pending && !und.length ? '' : ' disabled') + '>선택 ' + collect().length + '건 Neo4j 적재</button><button data-reject' + (pending ? '' : ' disabled') + '>전체 반려</button><button data-chat>← 카드 대화로</button><span id="gr-result" class="ev"></span></div></div>';
    body.querySelectorAll('[data-m]').forEach(function (b) { b.addEventListener('click', function () { var i = +b.dataset.k, k = RV.currentPersons[i], m2 = b.dataset.m, v = null;
      if (m2 === 'map') v = body.querySelector('[data-mapsel="' + i + '"]').value; if (m2 === 'rename') v = body.querySelector('[data-rn="' + i + '"]').value.trim(); if (m2 === 'org') { var ov = body.querySelector('[data-orgsel="' + i + '"]').value; v = ov === '__new__' ? null : ov; }
      setEntity(k, m2, v); renderTable(); }); });
    body.querySelectorAll('[data-mapsel]').forEach(function (s2) { s2.addEventListener('change', function () { var i = +s2.dataset.mapsel; setEntity(RV.currentPersons[i], 'map', s2.value); renderTable(); }); });
    body.querySelectorAll('[data-orgsel]').forEach(function (s2) { s2.addEventListener('change', function () { var i = +s2.dataset.orgsel; setEntity(RV.currentPersons[i], 'org', s2.value === '__new__' ? null : s2.value); renderTable(); }); });
    body.querySelectorAll('[data-rn]').forEach(function (inp) { inp.addEventListener('change', function () { var i = +inp.dataset.rn; setEntity(RV.currentPersons[i], 'rename', inp.value.trim()); renderTable(); }); });
    body.querySelectorAll('[data-chk]').forEach(function (c) { c.addEventListener('change', function () { RV.selected[+c.dataset.chk] = c.checked; renderTable(); }); });
    body.querySelectorAll('[data-ty]').forEach(function (s2) { s2.addEventListener('change', function () { RV.types[+s2.dataset.ty] = s2.value; }); });
    body.querySelectorAll('[data-key]').forEach(function (inp) { inp.addEventListener('change', function () { RV.keys[+inp.dataset.key][inp.dataset.side] = inp.value.trim(); }); });
    body.querySelector('[data-chat]').addEventListener('click', function () { setMode('chat'); });
    body.querySelector('[data-apply]').addEventListener('click', function () { var res = $('gr-result'); res.textContent = '적재 중…'; body.querySelector('[data-apply]').disabled = true;
      apply().then(function (out) { res.textContent = '적재 완료 — ' + out.applied + '건'; renderTable(); renderRows(); if (drawerOn) renderDrawer(); }).catch(function (e) { res.textContent = '실패 — ' + e.message; body.querySelector('[data-apply]').disabled = false; }); });
    body.querySelector('[data-reject]').addEventListener('click', function () { if (!confirm('전체 반려할까요?')) return; apost('reject', { file: RV.file }).then(function () { RV.pending = false; RV.data.status = 'rejected'; loadLists().then(function () { renderTable(); if (drawerOn) renderDrawer(); }); }); });
  }

  // ── 그래프 (graph.html 이식) ──
  var G = { nodes: [], links: [], vis: [], visL: [], hidden: {}, wmode: 'deg', view: { scale: 1, tx: 0, ty: 0 }, cv: null, ctx: null, W: 0, H: 0, drag: null, pan: null, down: null, raf: null, q: '', sel: null };
  function koAlias(props) { var a = props && props.aliases; if (!Array.isArray(a)) return null; return a.filter(function (x) { return typeof x === 'string' && /[가-힣]/.test(x); })[0] || null; }
  function dispName(label, key, props) { var cut = function (s, n) { return s.length > n ? s.slice(0, n) + '…' : s; };
    if (label === 'Concept') { var ko = koAlias(props); if (ko) return cut(ko, 18); }
    if (label === 'ActionItem' || label === 'Decision') { var d = props && (props.desc || props.summary); if (d) return cut(d, 20); return key.indexOf('#') >= 0 ? '#' + key.split('#').pop() : cut(key, 20); }
    if (label === 'Meeting') { var slug = key.replace(/^\d{4}-\d{2}-\d{2}-/, ''); return cut(slug.split('-').pop(), 20); }
    return cut(key, 24); }
  function graphLoad() {
    return api('graph').then(function (d) {
      var propsMap = {}; (d.nodes || []).forEach(function (n) { propsMap[n.label + ':' + n.key] = n.props; });
      var byId = {}, list = [];
      (d.links || []).forEach(function (l) { [[l.fromLabel, l.from], [l.toLabel, l.to]].forEach(function (p) { var id = p[0] + ':' + p[1]; if (!byId[id]) { byId[id] = { id: id, label: p[0], key: p[1], props: propsMap[id] || {}, disp: dispName(p[0], p[1], propsMap[id]), x: G.W / 2 + (Math.random() - .5) * 300, y: G.H / 2 + (Math.random() - .5) * 300, vx: 0, vy: 0, deg: 0, recent: 0 }; list.push(byId[id]); } }); });
      G.nodes = list;
      G.links = (d.links || []).map(function (l) { return { a: byId[l.fromLabel + ':' + l.from], b: byId[l.toLabel + ':' + l.to], type: l.type, implied: l.confidence === 'implied', t: l.recorded_at ? (Date.parse(l.recorded_at) || 0) : 0 }; });
      G.links.forEach(function (l) { l.a.deg++; l.b.deg++; if (l.t) { l.a.recent = Math.max(l.a.recent, l.t); l.b.recent = Math.max(l.b.recent, l.t); } });
      var ts = G.links.map(function (l) { return l.t; }).filter(Boolean), tmin = Math.min.apply(null, ts.concat([Infinity])), span = Math.max.apply(null, ts.concat([-Infinity])) - tmin;
      var norm = function (t) { return t ? (span ? (t - tmin) / span : 1) : 0; };
      G.links.forEach(function (l) { l.norm = norm(l.t); }); G.nodes.forEach(function (n) { n.recentNorm = norm(n.recent); });
      var st = {}; G.nodes.forEach(function (n) { st[n.label] = (st[n.label] || 0) + 1; }); S.graphStats = st;
      // 기본 숨김: Concept·Topic (수백 개로 화면을 덮는다) — 최초 1회
      if (!G.inited) { G.inited = true; ['Concept', 'Topic'].forEach(function (l) { if (st[l] > 40) G.hidden[l] = true; }); }
      graphWeights(); graphFilter(); graphCtl();
    });
  }
  function graphWeights() { G.nodes.forEach(function (n) { n.r = G.wmode === 'deg' ? Math.min(30, 8 + 2.5 * Math.sqrt(n.deg)) : G.wmode === 'recent' ? 8 + 18 * n.recentNorm : 13; }); G.links.forEach(function (l) { l.w = G.wmode === 'recent' ? 0.5 + 3.5 * l.norm : 1; l.alpha = G.wmode === 'recent' ? 0.35 + 0.65 * l.norm : 1; }); }
  function graphFilter() { var q = G.q.toLowerCase(); G.vis = G.nodes.filter(function (n) { return !G.hidden[n.label] && (!q || n.key.toLowerCase().indexOf(q) >= 0 || n.disp.toLowerCase().indexOf(q) >= 0 || (n.props.aliases || []).some(function (a) { return String(a).toLowerCase().indexOf(q) >= 0; })); }); var vs = {}; G.vis.forEach(function (n) { vs[n.id] = 1; }); G.visL = G.links.filter(function (l) { return vs[l.a.id] && vs[l.b.id]; }); var el = $('gr-empty'); if (el) el.style.display = G.vis.length ? 'none' : 'grid'; }
  function graphCtl() { var t = $('gr-types'); if (!t) return; var st = S.graphStats || {}; t.innerHTML = Object.keys(st).sort().map(function (l) { return '<label><input type="checkbox" data-l="' + l + '"' + (G.hidden[l] ? '' : ' checked') + '><i class="dot" style="background:' + (NODE_COLORS[l] || '#777') + '"></i>' + esc(LABEL_KO[l] || l) + ' <span style="color:var(--tx3)">' + st[l] + '</span></label>'; }).join('');
    t.querySelectorAll('input').forEach(function (cb) { cb.addEventListener('change', function () { if (cb.checked) delete G.hidden[cb.dataset.l]; else G.hidden[cb.dataset.l] = true; graphFilter(); }); }); }
  function toWorld(sx, sy) { return [(sx - G.view.tx) / G.view.scale, (sy - G.view.ty) / G.view.scale]; }
  function graphTick() { var V = G.vis; V.forEach(function (n) { n.fx = 0; n.fy = 0; });
    for (var i = 0; i < V.length; i++) for (var j = i + 1; j < V.length; j++) { var a = V[i], b = V[j], dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy || 1, d = Math.sqrt(d2), f = Math.min(12000 / d2, 8); a.fx += f * dx / d; a.fy += f * dy / d; b.fx -= f * dx / d; b.fy -= f * dy / d; }
    G.visL.forEach(function (l) { var dx = l.b.x - l.a.x, dy = l.b.y - l.a.y, d = Math.sqrt(dx * dx + dy * dy) || 1, f = (d - 150) * 0.02; l.a.fx += f * dx / d; l.a.fy += f * dy / d; l.b.fx -= f * dx / d; l.b.fy -= f * dy / d; });
    V.forEach(function (n) { if (n === G.drag) return; n.fx += (G.W / 2 - n.x) * 0.002; n.fy += (G.H / 2 - n.y) * 0.002; n.vx = (n.vx + n.fx) * 0.85; n.vy = (n.vy + n.fy) * 0.85; n.x += n.vx; n.y += n.vy; }); }
  function graphDraw() { var ctx = G.ctx, dpr = devicePixelRatio || 1, light = document.documentElement.getAttribute('data-theme') === 'light' || (!document.documentElement.getAttribute('data-theme') && matchMedia('(prefers-color-scheme: light)').matches);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, G.W, G.H);
    ctx.setTransform(dpr * G.view.scale, 0, 0, dpr * G.view.scale, dpr * G.view.tx, dpr * G.view.ty); ctx.font = '10px -apple-system,sans-serif';
    var lineC = light ? '120,120,130' : '110,110,120', txtC = light ? '#3a3a40' : '#c8c8cc', edgeTxt = light ? '#8a8a90' : '#6a6a70', ring = light ? '#ffffff' : '#0f0f0f';
    G.visL.forEach(function (l) { ctx.strokeStyle = 'rgba(' + lineC + ',' + (0.55 * l.alpha) + ')'; ctx.lineWidth = l.w; ctx.setLineDash(l.implied ? [4, 4] : []); ctx.beginPath(); ctx.moveTo(l.a.x, l.a.y); ctx.lineTo(l.b.x, l.b.y); ctx.stroke(); ctx.setLineDash([]);
      if (G.view.scale > 0.7) { ctx.fillStyle = edgeTxt; ctx.textAlign = 'center'; ctx.fillText(REL_KO[l.type] || l.type, (l.a.x + l.b.x) / 2, (l.a.y + l.b.y) / 2 - 4); }
      var ang = Math.atan2(l.b.y - l.a.y, l.b.x - l.a.x), off = l.b.r + 3, tx = l.b.x - off * Math.cos(ang), ty = l.b.y - off * Math.sin(ang);
      ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(tx - 7 * Math.cos(ang - .4), ty - 7 * Math.sin(ang - .4)); ctx.lineTo(tx - 7 * Math.cos(ang + .4), ty - 7 * Math.sin(ang + .4)); ctx.closePath(); ctx.fillStyle = 'rgba(' + lineC + ',' + (0.7 * l.alpha) + ')'; ctx.fill(); });
    G.vis.forEach(function (n) { ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 7); ctx.fillStyle = NODE_COLORS[n.label] || '#777'; ctx.fill(); ctx.strokeStyle = n === G.sel ? txtC : ring; ctx.lineWidth = n === G.sel ? 3 : 2; ctx.stroke();
      var fs = Math.min(16, Math.max(11, Math.round(11 + (n.r - 13) * 0.3))); ctx.font = '600 ' + fs + 'px -apple-system,sans-serif'; ctx.fillStyle = txtC; ctx.textAlign = 'center'; ctx.fillText(n.disp, n.x, n.y + n.r + 14); }); }
  var FIELD_KO = { desc: '내용', summary: '요약', status: '상태', topic: '주제', date: '일자', team: '소속', team_path: '소속 경로', role: '직책', rank: '직급', email: '이메일', kind: '구분', aliases: '별칭', wiki_path: '위키 노트', source: '출처', rationale: '근거', recorded_at: '기록일', role_code: null, rank_code: null };
  function graphInfo(n) { var el = $('gr-info'); if (!el) return; if (!n) { el.style.display = 'none'; return; } G.sel = n;
    var rows = Object.keys(n.props).filter(function (k) { return FIELD_KO[k] !== null && n.props[k] !== '' && n.props[k] != null; }).map(function (k) { var v = n.props[k]; return '<div class="kv"><span>' + esc(FIELD_KO[k] || k) + '</span><span>' + esc(Array.isArray(v) ? v.join(', ') : v) + '</span></div>'; }).join('');
    var ko = n.label === 'Concept' ? koAlias(n.props) : null, deg = n.deg;
    el.innerHTML = '<button class="gr-x" title="닫기">✕</button><b><i class="dot" style="background:' + (NODE_COLORS[n.label] || '#777') + '"></i>' + esc(ko || n.key) + '</b><small>' + esc(LABEL_KO[n.label] || n.label) + (ko ? ' · ' + esc(n.key) : '') + ' · 연결 ' + deg + '</small>' + rows + '<div class="act" style="margin-top:8px"><button class="pri" data-ask>이 노드로 봇에게 질문</button>' + (n.label === 'Person' ? '<button data-note>활동 노트</button>' : '') + '</div>';
    el.style.display = 'block';
    el.querySelector('.gr-x').addEventListener('click', function () { G.sel = null; el.style.display = 'none'; });
    el.querySelector('[data-ask]').addEventListener('click', function () { setMode('chat'); var inp = $('in'); if (inp) { inp.value = (ko || n.key) + (n.label === 'Person' ? '이 참석한 회의와 맡은 액션아이템을 정리해줘' : n.label === 'Meeting' ? ' 회의의 참석자·결정·액션아이템을 정리해줘' : '에 대해 그래프에서 아는 것을 정리해줘'); inp.focus(); } });
    var nb = el.querySelector('[data-note]'); if (nb) nb.addEventListener('click', function () { fetch('/file?path=' + encodeURIComponent('70-Activity/persons/' + n.key + '.md')).then(function (r) { if (!r.ok) throw 0; return r.json(); }).then(function (x) { showDoc('70-Activity/persons/' + n.key + '.md', x.text); }).catch(function () { alert('활동 노트가 없습니다 — 70-Activity/persons/' + n.key + '.md'); }); }); }
  function renderGraph() {
    var body = $('body');
    body.innerHTML = '<div class="gr-wrap"><canvas id="gr-cv"></canvas>' +
      '<div class="gr-search"><span>⌕</span><input id="gr-q" placeholder="노드 검색 — 이름·별칭" value="' + esc(G.q) + '"></div>' +
      '<div class="gr-ctl"><h4>가중치</h4><select id="gr-w"><option value="none"' + (G.wmode === 'none' ? ' selected' : '') + '>없음</option><option value="deg"' + (G.wmode === 'deg' ? ' selected' : '') + '>연결 많은 순</option><option value="recent"' + (G.wmode === 'recent' ? ' selected' : '') + '>최신순</option></select><h4>표시 유형</h4><div id="gr-types"></div><div class="gr-dash">─ direct &nbsp; ┄ implied · 드래그 이동 · 휠 줌 · 더블클릭 초기화</div></div>' +
      '<div class="gr-info" id="gr-info" style="display:none"></div><div class="gr-empty" id="gr-empty" style="display:none">표시할 노드가 없습니다</div></div>';
    var cv = $('gr-cv'); G.cv = cv; G.ctx = cv.getContext('2d');
    function resize() { var r = cv.parentElement.getBoundingClientRect(); G.W = r.width; G.H = r.height; cv.width = G.W * (devicePixelRatio || 1); cv.height = G.H * (devicePixelRatio || 1); }
    resize(); G.resize = resize; window.addEventListener('resize', resize);
    $('gr-w').addEventListener('change', function (e) { G.wmode = e.target.value; graphWeights(); });
    $('gr-q').addEventListener('input', function (e) { G.q = e.target.value.trim(); graphFilter(); });
    cv.addEventListener('mousedown', function (e) { var r = cv.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top, w = toWorld(sx, sy); G.drag = G.vis.filter(function (n) { return (n.x - w[0]) * (n.x - w[0]) + (n.y - w[1]) * (n.y - w[1]) < (n.r + 5) * (n.r + 5); })[0] || null; if (!G.drag) G.pan = { sx: e.clientX, sy: e.clientY, tx: G.view.tx, ty: G.view.ty }; G.down = { sx: sx, sy: sy, node: G.drag }; });
    cv.addEventListener('mousemove', function (e) { if (G.drag) { var r = cv.getBoundingClientRect(), w = toWorld(e.clientX - r.left, e.clientY - r.top); G.drag.x = w[0]; G.drag.y = w[1]; G.drag.vx = G.drag.vy = 0; } else if (G.pan) { G.view.tx = G.pan.tx + (e.clientX - G.pan.sx); G.view.ty = G.pan.ty + (e.clientY - G.pan.sy); } });
    window.addEventListener('mouseup', function (e) { if (!G.cv || !G.cv.isConnected) return; var r = cv.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top; if (G.down && (G.down.sx - sx) * (G.down.sx - sx) + (G.down.sy - sy) * (G.down.sy - sy) < 25) { if (G.down.node) graphInfo(G.down.node); else { G.sel = null; graphInfo(null); } } G.drag = null; G.pan = null; G.down = null; });
    cv.addEventListener('wheel', function (e) { e.preventDefault(); var r = cv.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top, w = toWorld(sx, sy); G.view.scale = Math.min(4, Math.max(0.2, G.view.scale * Math.exp(-e.deltaY * 0.0015))); G.view.tx = sx - w[0] * G.view.scale; G.view.ty = sy - w[1] * G.view.scale; }, { passive: false });
    cv.addEventListener('dblclick', function () { G.view = { scale: 1, tx: 0, ty: 0 }; });
    var p = G.nodes.length ? Promise.resolve(graphCtl()) : graphLoad();
    p.then(function () { graphFilter(); if (G.raf) cancelAnimationFrame(G.raf); (function loop() { if (!G.cv || !G.cv.isConnected) { G.raf = null; return; } graphTick(); graphDraw(); G.raf = requestAnimationFrame(loop); })(); })
     .catch(function (e) { body.innerHTML = '<div class="empty">그래프를 불러올 수 없습니다 — ' + esc(e.message) + '</div>'; });
  }

  // ── 동의어 ──
  function renderSyn() {
    var body = $('body'); body.innerHTML = '<div class="empty">동의어 후보 검사 중…</div>';
    api('synonyms').then(function (groups) {
      if (!groups.length) { body.innerHTML = '<div class="empty">동의어 후보가 없습니다</div>'; return; }
      body.innerHTML = '<div class="gr-syn">' + groups.map(function (g, gi) {
        var sameName = g.members.length === 2 && g.members[0].name === g.members[1].name;
        return '<div class="gr-sc" data-gi="' + gi + '"><div class="h"><span class="k" style="background:' + (NODE_COLORS[g.label] || '#777') + '22;color:' + (NODE_COLORS[g.label] || '#777') + '">' + esc(LABEL_KO[g.label] || g.label) + '</span>' + esc((g.reasons || []).join(' · ')) + '</div>' +
          g.members.map(function (m, mi) { return '<label class="m"><input type="radio" name="gr-canon' + gi + '" value="' + mi + '"' + (mi === 0 ? ' checked' : '') + '><span>' + esc(m.name) + (m.aliases && m.aliases.length ? '<small>별칭: ' + esc(m.aliases.join(', ')) + '</small>' : '') + (m.team ? '<small>' + esc(m.team) + '</small>' : '') + '</span><span class="n">관계 ' + m.degree + '</span><input type="checkbox" data-inc="' + mi + '" checked' + (mi === 0 ? ' disabled' : '') + ' title="병합 대상"></label>'; }).join('') +
          '<div class="bt"><button class="' + (sameName ? '' : 'pri') + '" data-merge>병합</button>' + (sameName ? '<button class="pri" data-keep>다른 사람 — 유지</button>' : '<button data-keep>건너뛰기</button>') + '<span class="ev" data-msg></span></div></div>'; }).join('') + '</div>';
      body.querySelectorAll('.gr-sc').forEach(function (card) { var gi = +card.dataset.gi, g = groups[gi];
        card.querySelectorAll('input[type=radio]').forEach(function (rb) { rb.addEventListener('change', function () { var c = +rb.value; card.querySelectorAll('[data-inc]').forEach(function (cb) { cb.disabled = +cb.dataset.inc === c; if (+cb.dataset.inc === c) cb.checked = true; }); }); });
        card.querySelector('[data-keep]').addEventListener('click', function () { card.style.opacity = .4; card.querySelectorAll('input,button').forEach(function (x) { x.disabled = true; }); });
        card.querySelector('[data-merge]').addEventListener('click', function () { var c = +card.querySelector('input[type=radio]:checked').value, dups = g.members.filter(function (_, mi) { return mi !== c && card.querySelector('[data-inc="' + mi + '"]').checked; }).map(function (m) { return m.name; }), msg = card.querySelector('[data-msg]');
          if (!dups.length) { msg.textContent = '병합할 노드를 선택하세요'; return; }
          if (!confirm("'" + dups.join("', '") + "' → '" + g.members[c].name + "' 으로 병합합니다.\n선택된 노드는 삭제되고 별칭으로 보존됩니다.")) return;
          msg.textContent = '병합 중…';
          apost('merge', { label: g.label, canonical: g.members[c].name, merge: dups }).then(function (out) { msg.textContent = '완료 — ' + out.merged + '개 병합, 관계 ' + out.moved_rels + '건 이관'; card.querySelectorAll('input,button').forEach(function (x) { x.disabled = true; }); G.nodes = []; S.graphStats = null; }).catch(function (e) { msg.textContent = '실패 — ' + e.message; }); }); });
    }).catch(function (e) { body.innerHTML = '<div class="empty">동의어 후보 조회 실패 — ' + esc(e.message) + '</div>'; });
  }

  // 녹음은 미팅 기록자(office-meeting.js, MR)가 담당한다. 헤더 ● 버튼만 여기서 위임.
  function recording() { return !!(window.MR && MR.recording()); }

  // ── 셸 연결점 ──
  function headerButtons() {
    var rec = recording();
    return (rec ? '' : '<button class="ic gr-rec" id="gr-recbtn">● 녹음</button>') +
      '<button class="ic" id="gr-graph" aria-pressed="' + (mode === 'gr-graph') + '">그래프</button>' +
      '<button class="ic" id="gr-syn" aria-pressed="' + (mode === 'gr-syn') + '">동의어</button>' +
      '<button class="ic" id="gr-table" aria-pressed="' + (mode === 'gr-table') + '"' + (S.RV ? '' : ' disabled') + '>' + (mode === 'gr-table' ? '카드 모드' : '표 모드') + '</button>';
  }
  function bindHeader() {
    var b = $('gr-recbtn'); if (b) b.addEventListener('click', function () { if (window.MR) MR.start(); });
    var g = $('gr-graph'); if (g) g.addEventListener('click', function () { setMode(mode === 'gr-graph' ? 'chat' : 'gr-graph'); });
    var s2 = $('gr-syn'); if (s2) s2.addEventListener('click', function () { setMode(mode === 'gr-syn' ? 'chat' : 'gr-syn'); });
    var t = $('gr-table'); if (t) t.addEventListener('click', function () { setMode(mode === 'gr-table' ? 'chat' : 'gr-table'); });
  }
  function headerSub() { if (mode === 'gr-graph') return 'jarvis-neo4j 실시간'; if (mode === 'gr-syn') return '유사 표기 노드 병합 검토'; if (S.RV) return (mode === 'gr-table' ? '표 모드 · ' : '검토 중 · ') + (S.RV.data.doc || S.RV.file) + ' · 관계 ' + S.RV.rels.length + '건'; var p = pendingCount(); return p ? '검토 대기 ' + p + '건' : 'Neo4j 관계 검토 · 인물 그래프'; }
  function renderMode() { if (mode === 'gr-graph') renderGraph(); else if (mode === 'gr-syn') renderSyn(); else if (mode === 'gr-table') renderTable(); }
  function ensureAgent() { if (!AG[KEY]) { AG[KEY] = { key: KEY, name: '관계 컨펌 봇', color: '#c9b45c', hair: '#4a4030', kind: 'lib' }; ORDER.push(KEY); } }
  function chatSend(text) { return false; }
  return { KEY: KEY, S: S, loadLists: loadLists, preview: preview, pendingCount: pendingCount, renderChatBody: renderChatBody, renderDrawer: renderDrawer,
           renderMode: renderMode, headerButtons: headerButtons, bindHeader: bindHeader, headerSub: headerSub, ensureAgent: ensureAgent, chatSend: chatSend,
           startReview: startReview, recording: recording, NODE_COLORS: NODE_COLORS };
})();
