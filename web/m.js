/* Fungi mobile web UI — mirrors desktop app.js contracts:
   turn entries model, /events tape reattach, ask-card re-mount discipline.
   Mobile-specific: right-swipe session drawer (2/3 width, GSAP drag), token gate. */
marked.setOptions({ breaks: true, gfm: true });
const msgs = document.getElementById('messages'), input = document.getElementById('input'),
  btn = document.getElementById('btn-send'), status = document.getElementById('status'),
  banner = document.getElementById('asks-banner');
let processing = false, currentSessionId = null, allSessions = [], sessionDirty = false;
let _liveCount = 0; // live-node count at last streaming render (animate only fresh nodes)
let rawMessages = [];

/* ---------- token gate ---------- */
const TOKEN = (() => {
  const q = new URLSearchParams(location.search).get('t');
  if (q) { try { localStorage.setItem('fungi-token', q); } catch (e) {} return q; }
  try { return localStorage.getItem('fungi-token') || ''; } catch (e) { return ''; }
})();
function api(path) {
  if (!TOKEN) return path;
  return path + (path.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(TOKEN);
}
function unauthorized() { document.getElementById('rescan-overlay').classList.remove('hide'); }
async function fetchJSON(path, opts) {
  const r = await fetch(api(path), opts);
  if (r.status === 403) { unauthorized(); throw new Error('unauthorized'); }
  return r;
}

/* ---------- helpers ---------- */
function setCurrentSession(id) {
  currentSessionId = id;
  try { if (id) localStorage.setItem('fungi-session-m', id); else localStorage.removeItem('fungi-session-m'); } catch (e) {}
}
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function addDiv(cls, html, id) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  if (id) d.id = id;
  if (html) d.innerHTML = html;
  msgs.appendChild(d);
  return d;
}
function isNearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 80; }
function updateScrollBtn() {
  const b = document.getElementById('scroll-bottom');
  if (!b) return;
  if (isNearBottom(msgs)) b.classList.remove('visible'); else b.classList.add('visible');
}
function motionOn() { return window.gsap && !matchMedia('(prefers-reduced-motion: reduce)').matches; }
function msgIn(node, kind) {
  if (!motionOn()) return;
  gsap.from(node, { opacity: 0, y: kind === 'user' ? 14 : 18, scale: 0.96, duration: 0.4, ease: 'power3.out', clearProps: 'all' });
}
function getSessionTitle(list) {
  const u = list.find(m => m.role === 'user');
  if (!u) return 'Empty';
  const t = String(u.content).replace(/\s+/g, ' ').trim();
  return t.length > 40 ? t.slice(0, 38) + '...' : t;
}

/* ---------- sessions ---------- */
let _sessionsSeq = 0;
async function loadSessions() {
  const seq = ++_sessionsSeq;
  try {
    const r = await fetchJSON('/sessions');
    if (!r.ok) throw new Error(r.status);
    const sessions = (await r.json()).sessions || [];
    if (seq !== _sessionsSeq) return;
    allSessions = sessions;
    renderSessionList();
  } catch (e) { if (e.message !== 'unauthorized') console.error('loadSessions:', e); }
}
async function reloadSessionFromServer() {
  if (!currentSessionId) return;
  try {
    const r = await fetchJSON('/session?id=' + encodeURIComponent(currentSessionId));
    if (!r.ok) return;
    const s = await r.json();
    rawMessages = s.messages || [];
    const stick = isNearBottom(msgs);
    renderMessages(s);
    if (stick) { msgs.scrollTop = msgs.scrollHeight; updateScrollBtn(); }
    loadSessions();
  } catch (e) {}
}
async function closeCurrentSession() {
  if (!currentSessionId) return;
  if (processing && turn && turn.sessionId === currentSessionId) {
    // A turn is running here: the server persists on its own — just detach.
    setCurrentSession(null);
    return;
  }
  if (!sessionDirty) { setCurrentSession(null); return; }
  const list = rawMessages;
  if (!list || list.length <= 1) {
    try { await fetch(api('/session?id=' + encodeURIComponent(currentSessionId)), { method: 'DELETE' }); } catch (e) {}
    setCurrentSession(null);
  } else {
    const title = getSessionTitle(list);
    try {
      await fetch(api('/save'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: currentSessionId, title, messages: list }) });
    } catch (e) {}
  }
  loadSessions();
}
async function switchSession(id) {
  if (id === currentSessionId && !friendView) { closeDrawer(); return; }
  leaveFriendView();
  await closeCurrentSession();
  sessionDirty = false;
  try {
    const r = await fetchJSON('/session?id=' + encodeURIComponent(id));
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    setCurrentSession(s.id);
    rawMessages = s.messages || [];
    msgs.innerHTML = '';
    renderMessages(s);
    document.getElementById('session-title').textContent = s.title || getSessionTitle(rawMessages) || 'Fungi';
    msgs.scrollTop = msgs.scrollHeight; updateScrollBtn();
    renderSessionList(); loadSessions();
    reattachIfRunning(s.id);
  } catch (e) { window.__swErr = String((e && e.stack) || e); if (e.message !== 'unauthorized') console.error('switchSession:', e); }
  closeDrawer();
}
async function newSession() {
  // Focus any untouched "(new session)" instead of littering the list.
  const empty = allSessions.find(s => s.title === '(new session)' && (s.msgCount || 0) <= 1 && !s.running);
  leaveFriendView();
  if (empty) {
    if (empty.id !== currentSessionId) await switchSession(empty.id);
    closeDrawer();
    return;
  }
  await closeCurrentSession();
  try {
    const r = await fetchJSON('/new', { method: 'POST' });
    if (!r.ok) throw new Error(r.status);
    const { id } = await r.json();
    setCurrentSession(id); rawMessages = []; msgs.innerHTML = '';
    document.getElementById('session-title').textContent = 'Fungi';
    sessionDirty = false;
    await loadSessions();
  } catch (e) { if (e.message !== 'unauthorized') console.error('newSession:', e); }
  closeDrawer();
}
async function deleteSession(id) {
  try {
    await fetch(api('/session?id=' + encodeURIComponent(id)), { method: 'DELETE' });
    if (id === currentSessionId) {
      setCurrentSession(null); msgs.innerHTML = '';
      document.getElementById('session-title').textContent = 'Fungi';
    }
    document.getElementById('session-filter').value = '';
    await loadSessions();
  } catch (e) {}
}

/* ---------- confirm modal (desktop showConfirm contract) ---------- */
let _confirmState = null;
function showConfirm(opts) {
  const overlay = document.getElementById('confirm-overlay');
  const ok = document.getElementById('confirm-ok');
  document.getElementById('confirm-title').textContent = opts.title || '确认？';
  document.getElementById('confirm-message').textContent = opts.message || '';
  ok.textContent = opts.confirmText || '确定';
  document.getElementById('confirm-cancel').textContent = opts.cancelText || '取消';
  ok.classList.toggle('danger', !!opts.danger);
  _confirmState = { onConfirm: opts.onConfirm, danger: !!opts.danger };
  overlay.classList.add('show');
}
function closeConfirm(confirmed) {
  const overlay = document.getElementById('confirm-overlay');
  if (!overlay.classList.contains('show')) return;
  overlay.classList.remove('show');
  const state = _confirmState;
  _confirmState = null;
  if (confirmed && state && state.onConfirm) state.onConfirm();
}
document.getElementById('confirm-ok').addEventListener('click', () => closeConfirm(true));
document.getElementById('confirm-cancel').addEventListener('click', () => closeConfirm(false));
document.getElementById('confirm-overlay').addEventListener('click', e => {
  if (e.target.id === 'confirm-overlay') closeConfirm(false);
});

/* ---------- rename (desktop startRename/finishRename contract) ---------- */
function startRename(row, s) {
  const titleEl = row.querySelector('.session-row-title');
  const old = titleEl.textContent;
  const inp = document.createElement('input');
  inp.className = 'rename-input'; inp.value = s.title || old;
  inp.addEventListener('blur', () => finishRename(row, s, inp, old));
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') inp.blur();
    if (e.key === 'Escape') { row.replaceChild(titleEl, inp); titleEl.textContent = old; }
  });
  row.replaceChild(inp, titleEl); inp.focus(); inp.select();
}
async function finishRename(row, s, inp, old) {
  const newTitle = inp.value.trim();
  if (!newTitle || newTitle === old) { renderSessionList(); return; }
  try {
    await fetch(api('/save'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: s.id, title: newTitle, messages: [] }) });
    s.title = newTitle;
    if (s.id === currentSessionId) document.getElementById('session-title').textContent = newTitle;
  } catch (e) {}
  renderSessionList();
}

/* Drawer list: keyed rows survive re-render so GSAP stagger doesn't rebuild everything */
function renderSessionList() {
  const list = document.getElementById('session-list');
  const empty = document.getElementById('session-list-empty');
  const filter = (document.getElementById('session-filter').value || '').trim().toLowerCase();
  const filtered = filter ? allSessions.filter(s => (s.title || '').toLowerCase().includes(filter)) : allSessions;
  const fresh = [];
  list.querySelectorAll('.session-row').forEach(r => r.remove());
  filtered.forEach(s => {
    const row = document.createElement('div');
    row.dataset.sid = s.id;
    row.className = 'session-row' + (s.id === currentSessionId ? ' active' : '');
    row.innerHTML = '<span class="session-row-title">' + escapeHtml(s.title || 'Untitled') + '</span>'
      + '<span class="session-row-meta">' + fmtDate(s.created) + (s.running ? ' \u25cf' : '') + '</span>'
      + '<button class="session-row-act ren" title="重命名">&#9998;</button>'
      + '<button class="session-row-act del" title="删除">&#10005;</button>';
    row.querySelector('.session-row-act.ren').addEventListener('click', e => {
      e.stopPropagation(); startRename(row, s);
    });
    row.querySelector('.session-row-act.del').addEventListener('click', e => {
      e.stopPropagation();
      showConfirm({
        title: '删除会话',
        message: '「' + (s.title || 'Untitled') + '」将被永久删除，不可恢复。',
        confirmText: '删除',
        danger: true,
        onConfirm: () => deleteSession(s.id)
      });
    });
    row.addEventListener('click', () => switchSession(s.id));
    list.appendChild(row);
    fresh.push(row);
  });
  if (filtered.length === 0) { empty.style.display = ''; empty.textContent = filter ? '没有匹配的会话。' : '还没有会话。'; }
  else empty.style.display = 'none';
  if (motionOn()) gsap.from(fresh, { opacity: 0, x: -26, duration: 0.35, stagger: 0.04, ease: 'power3.out', clearProps: 'all' });
}
function fmtDate(d) {
  if (!d) return '';
  const diff = Date.now() - new Date(d).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return 'now';
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h';
  const days = Math.floor(h / 24);
  if (days < 7) return days + 'd';
  return new Date(d).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
}

/* ---------- transcript render (full re-render = refresh-grade) ---------- */
function renderMessages(s) {
  _liveCount = 0;
  // Full re-render must replace: renderTranscript only appends (b8e3b65 contract).
  msgs.innerHTML = '';
  renderTranscript(rawMessages, s.asks || []);
  if (turn && turn.sessionId === currentSessionId) {
    if (turn.userText && rawMessages.some(m => m.role === 'user' && m.content === turn.userText)) {
      turn.userRendered = true; // disk copy already has it; live bubble would double it
    }
    renderTurnLive();
  }
  placeAskCards();
}
function renderTranscript(messages, asks) {
  let toolBlocks = {};
  const askQueue = (asks || []).slice();
  for (const m of messages || []) {
    if (m.role === 'user') addDiv('user', marked.parse(m.content || ''));
    else if (m.role === 'assistant') {
      if (m.reasoning) {
        const det = document.createElement('details');
        det.className = 'msg reasoning';
        det.innerHTML = '<summary>Thinking\u2026</summary><div>' + escapeHtml(m.reasoning) + '</div>';
        msgs.appendChild(det);
      }
      if (m.content) {
        const c = String(m.content);
        if (c.startsWith('(LLM error:') || c.startsWith('(Hit max tool rounds'))
          addDiv('error', '&#x26A0; ' + escapeHtml(c));
        else addDiv('assistant', marked.parse(m.content));
      }
      if (m.tool_calls) m.tool_calls.forEach(tc => {
        const d = document.createElement('div');
        d.className = 'msg tool'; d.id = 'tool-' + tc.id;
        const args = tc.function?.arguments || '';
        const argsHtml = args ? ' <code>' + escapeHtml(args.length > 60 ? args.slice(0, 60) + '...' : args) + '</code>' : '';
        d.innerHTML = '<div class="tool-label">&#x1F527; ' + escapeHtml(tc.function?.name || 'tool') + argsHtml + '</div><div class="tool-result"></div>';
        msgs.appendChild(d);
        if (tc.function?.name === 'inquire' || tc.function?.name === 'confirm' || tc.function?.name === 'ask_user') {
          const rec = askQueue.shift();
          if (rec) msgs.appendChild(buildAnsweredAskCard(rec));
        }
        toolBlocks[tc.id] = d;
      });
    } else if (m.role === 'tool') {
      const block = toolBlocks[m.tool_call_id];
      if (block) block.querySelector('.tool-result').innerHTML = '<pre>' + escapeHtml(m.content || '') + '</pre>';
      else addDiv('tool', '<pre>' + escapeHtml(m.content || '') + '</pre>');
    }
  }
  askQueue.forEach(rec => msgs.appendChild(buildAnsweredAskCard(rec)));
}

/* ---------- send / stream ---------- */
let abortCtrl = null;
let stopRequested = false;
let stopTimer = null;
let turn = null; // {sessionId, entries, userText, userRendered, aborted}

function setBusy(busy, stopping) {
  btn.classList.toggle('stopping', !!stopping);
  btn.innerHTML = busy ? '&#x25A0;' : '&#x2191;'; // ■ stop while running, ↑ send
  btn.disabled = false;
  btn.title = busy ? (stopping ? '再点一次强制断开' : '停止') : '发送';
}
async function send() {
  if (processing && turn) { // running: button = stop (graceful, then hard)
    const sid = turn.sessionId || currentSessionId;
    if (!stopRequested) {
      stopRequested = true; setBusy(true, true); status.textContent = '正在停止…';
      if (sid) fetch(api('/stop'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: sid }) }).catch(() => {});
      stopTimer = setTimeout(() => { if (abortCtrl) { abortCtrl.abort(); abortCtrl = null; } }, 15000);
    } else {
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      stopRequested = false;
      abortCtrl.abort(); abortCtrl = null;
      status.textContent = '已断开。';
    }
    return;
  }
  const text = input.value.trim();
  if (!text) return;
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  setBusy(true);
  if (!currentSessionId) {
    try {
      const r = await fetchJSON('/new', { method: 'POST', signal: abortCtrl.signal });
      const { id } = await r.json();
      setCurrentSession(id); loadSessions();
    } catch (e) { if (e.message !== 'unauthorized') console.error(e); }
  }
  const sid = currentSessionId;
  turn = { sessionId: sid, entries: [] };
  rawMessages.push({ role: 'user', content: text });
  sessionDirty = true;
  turn.userText = text;
  input.value = ''; autoGrow(); status.textContent = 'Thinking...';
  _liveCount = 0;
  renderTurnLive();
  await pumpStream(api('/chat'), { message: text, sessionId: sid });
}
/* A session's turn may still run server-side (reload / switch): /events
   replays the tape, then streams live. */
function reattachIfRunning(sid) {
  if (processing || turn || !sid) return;
  const s = allSessions.find(x => x.id === sid);
  if (!s || !s.running) return;
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  turn = { sessionId: sid, entries: [] };
  setBusy(true);
  status.textContent = '回合仍在服务器上运行，正在接续…';
  _liveCount = 0;
  renderTurnLive();
  pumpStream(api('/events?sessionId=' + encodeURIComponent(sid)), null, 'GET');
}
/* A stream that dies without a done event (user hard-abort, network drop,
   server crash) leaves live-node cards whose content never reached a disk-
   backed render. Reconcile with the persisted transcript immediately, then
   resume streaming if the turn still runs server-side — otherwise the cards
   vanish at the next renderTurnLive (new send / reattach), with no done ever
   arriving to reload them. */
async function recoverAfterDrop(sid) {
  if (!sid) return;
  await reloadSessionFromServer();
  if (turn || processing) return; // user already started something else
  await loadSessions();           // fresh s.running for the reattach check
  reattachIfRunning(sid);
}
async function pumpStream(url, body, method = 'POST') {
  const sid = turn ? turn.sessionId : null; // the stream may die mid-turn
  try {
    const opts = { method, signal: abortCtrl.signal };
    if (body !== null) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (resp.status === 403) { unauthorized(); turn = null; processing = false; setBusy(false); return; }
    if (!resp.ok) { status.textContent = '错误：HTTP ' + resp.status; turn = null; }
    else {
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let leftover = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        leftover += dec.decode(value, { stream: true });
        const lines = leftover.split('\n');
        leftover = lines.pop() || '';
        for (const line of lines) {
          if (!line) continue;
          let obj;
          try { obj = JSON.parse(line); } catch (e) { continue; }
          handleTurnEvent(obj);
        }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') status.textContent = '已中止。';
    else status.textContent = '错误：' + e.message;
    turn = null;
    recoverAfterDrop(sid);
  }
  if (turn) {
    // Stream ended without a done event: never leave a phantom live turn.
    status.textContent = '连接中断。';
    turn = null;
    recoverAfterDrop(sid);
  }
  processing = false; setBusy(false);
}

/* Turn events update the model first, then touch the DOM only when the
   turn's session is on screen (desktop contract, verbatim). */
function handleTurnEvent(obj) {
  const t = turn;
  if (!t) return;
  const visible = currentSessionId === t.sessionId && !friendView;
  switch (obj.type) {
    case 'text': {
      const last = t.entries[t.entries.length - 1];
      if (last && last.kind === 'text') last.content += obj.content;
      else t.entries.push({ kind: 'text', content: obj.content });
      status.textContent = 'Writing...';
      if (visible) updateLastText();
      break;
    }
    case 'reasoning_start':
      t.entries.push({ kind: 'reasoning', content: '', closed: false });
      if (visible) renderTurnLive();
      break;
    case 'reasoning': {
      const last = t.entries[t.entries.length - 1];
      if (last && last.kind === 'reasoning') last.content += obj.content;
      if (visible) updateLastReasoning();
      break;
    }
    case 'reasoning_end': {
      const idx = t.entries.map(x => x.kind).lastIndexOf('reasoning');
      if (idx >= 0) t.entries[idx].closed = true;
      if (visible) { const d = document.getElementById('live-details-' + idx); if (d) d.open = false; }
      break;
    }
    case 'tool':
      t.entries.push({ kind: 'tool', id: obj.content.id, name: obj.content.name, args: obj.content.args, result: '' });
      // A long tool used to leave a stale "Writing..." on the status bar.
      if (visible) { status.textContent = '⚙ ' + (obj.content.name || 'tool') + '…'; renderTurnLive(); }
      break;
    case 'tool_result': {
      const rec = t.entries.find(x => x.kind === 'tool' && x.id === obj.content.id)
        || [...t.entries].reverse().find(x => x.kind === 'tool' && !x.result);
      if (rec) rec.result = obj.content.content;
      if (visible) {
        status.textContent = 'Thinking...'; // tool done: the next LLM round starts
        const block = document.getElementById('tool-' + obj.content.id);
        if (block) block.querySelector('.tool-result').innerHTML = '<pre>' + escapeHtml(obj.content.content) + '</pre>';
        else renderTurnLive();
      }
      break;
    }
    case 'agent_spawn':
      agents[obj.content.id] = {
        layer: obj.content.layer, goal: obj.content.goal,
        replyFormat: obj.content.reply_format || '', status: 'running', history: [],
      };
      if (obj.content.call_id) specByCall[obj.content.call_id] = obj.content.id;
      agentBubble(obj.content.id);
      break;
    case 'agent_status': setAgentStatus(obj.content.id, obj.content.status); break;
    case 'agent_event': {
      const aid = obj.content.id, ev = obj.content.event;
      if (agents[aid]) agents[aid].history.push(ev);
      agentEvent(aid, ev);
      break;
    }
    case 'ask':
      t.entries.filter(x => x.kind === 'ask').forEach(a => a.active = false);
      t.entries.push({ kind: 'ask', id: obj.content.id, questions: obj.content.questions || [], answers: null, active: true });
      if (visible) renderTurnLive();
      break;
    case 'status':
      if (visible) status.textContent = obj.content;
      break;
    case 'error':
      if (obj.content === 'Aborted by user') t.aborted = true;
      t.entries.push({ kind: 'error', content: obj.content });
      if (visible) renderTurnLive();
      break;
    case 'sessionId':
      if (!t.sessionId) t.sessionId = obj.content;
      break;
    case 'done': {
      const viewing = currentSessionId === t.sessionId;
      const failed = t.entries.length && t.entries[t.entries.length - 1].kind === 'error';
      turn = null; abortCtrl = null; stopRequested = false;
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      status.textContent = t.aborted ? '已中止。'
        : (failed ? '回合失败。' : '');
      if (viewing) reloadSessionFromServer();
      else loadSessions();
      break;
    }
  }
  if (visible && currentSessionId === t.sessionId && isNearBottom(msgs)) msgs.scrollTop = msgs.scrollHeight;
}

function updateLastText() {
  const t = turn;
  const idx = t.entries.map(x => x.kind).lastIndexOf('text');
  if (idx < 0) return renderTurnLive();
  const el = document.getElementById('live-text-' + idx);
  if (el) {
    el.innerHTML = marked.parse(t.entries[idx].content) + '<span class="live-cursor"></span>';
    if (isNearBottom(msgs)) msgs.scrollTop = msgs.scrollHeight;
  } else renderTurnLive();
}
function updateLastReasoning() {
  const t = turn;
  const idx = t.entries.map(x => x.kind).lastIndexOf('reasoning');
  if (idx < 0) return;
  const el = document.getElementById('live-reasoning-' + idx);
  if (el) { el.textContent = t.entries[idx].content; el.scrollTop = el.scrollHeight; const d = document.getElementById('live-details-' + idx); if (d) d.open = true; }
  else renderTurnLive();
}
function renderTurnLive() {
  if (!turn || turn.sessionId !== currentSessionId || friendView) return;
  const saved = saveAskCardState();
  msgs.querySelectorAll('.live-node').forEach(n => n.remove());
  if (turn.userText && !turn.userRendered) {
    const u = document.createElement('div');
    u.className = 'msg user live-node';
    u.innerHTML = marked.parse(turn.userText);
    msgs.appendChild(u);
  }
  turn.entries.forEach((e, i) => {
    if (e.kind === 'reasoning') {
      const det = document.createElement('details');
      det.className = 'msg reasoning live-node'; det.id = 'live-details-' + i; det.open = !e.closed;
      det.innerHTML = '<summary>Thinking\u2026</summary><div id="live-reasoning-' + i + '"></div>';
      det.querySelector('#live-reasoning-' + i).textContent = e.content;
      msgs.appendChild(det);
    } else if (e.kind === 'text') {
      const ad = document.createElement('div');
      ad.className = 'msg assistant live-node'; ad.id = 'live-text-' + i;
      ad.innerHTML = marked.parse(e.content) + '<span class="live-cursor"></span>';
      msgs.appendChild(ad);
    } else if (e.kind === 'tool') {
      const d = document.createElement('div');
      d.className = 'msg tool live-node'; d.id = 'tool-' + e.id;
      const argsHtml = e.args ? ' <code>' + escapeHtml(e.args.length > 60 ? e.args.slice(0, 60) + '...' : e.args) + '</code>' : '';
      d.innerHTML = '<div class="tool-label">&#x1F527; ' + escapeHtml(e.name) + argsHtml + '</div><div class="tool-result">' + (e.result ? '<pre>' + escapeHtml(e.result) + '</pre>' : '') + '</div>';
      msgs.appendChild(d);
    } else if (e.kind === 'ask') {
      const card = e.active ? buildActiveAskCard(e, saved) : buildAnsweredAskCard(e);
      card.classList.add('live-node');
      msgs.appendChild(card);
    } else if (e.kind === 'error') {
      const d = document.createElement('div');
      d.className = 'msg error live-node';
      d.innerHTML = '&#x26A0; ' + escapeHtml(e.content);
      msgs.appendChild(d);
    }
  });
  msgs.scrollTop = msgs.scrollHeight;
  // Animate only nodes that appeared since the previous streaming re-render —
  // re-animating all live nodes per chunk would flicker (desktop contract).
  const live = msgs.querySelectorAll('.live-node');
  for (let li = _liveCount; li < live.length; li++) {
    const n = live[li];
    msgIn(n, n.classList.contains('user') ? 'user' : n.classList.contains('assistant') ? 'assistant' : 'other');
  }
  _liveCount = live.length;
}

/* ---------- ask cards (in-turn Inquire) ---------- */
function saveAskCardState() {
  const card = document.getElementById('ask-card');
  if (!card) return null;
  return (card._askQuestions || []).map((q, qi) => {
    const sel = card.querySelector('.ask-option.selected[data-q="' + qi + '"]');
    const inp = card.querySelector('.ask-input[data-q="' + qi + '"]');
    return { sel: sel ? sel.querySelector('b').textContent : null, val: inp ? inp.value : '' };
  });
}
function askQuestionHtml(q, qi) {
  const opts = (q.options || []).map(o =>
    '<button type="button" class="ask-option" data-q="' + qi + '"><b>' + escapeHtml(o.label) + '</b>'
    + (o.description ? '<br><span class="ask-desc">' + escapeHtml(o.description) + '</span>' : '') + '</button>').join('');
  return '<div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div>'
    + '<div class="ask-options">' + opts + '</div>'
    + (((q.options || []).length === 0 || q.allow_custom !== false)
      ? '<input class="ask-input" data-q="' + qi + '" placeholder="' + ((q.options || []).length ? '或者自己输入…' : '你的回答…') + '">'
      : '');
}
function buildActiveAskCard(a, saved) {
  const card = document.createElement('div');
  card.className = 'msg ask-card live-node'; card.id = 'ask-card';
  card._askId = a.id; card._askQuestions = a.questions;
  card.innerHTML = a.questions.map((q, qi) => '<div class="ask-block">' + askQuestionHtml(q, qi) + '</div>').join('')
    + '<div class="ask-actions"><button id="ask-submit">提交</button></div>';
  (saved || []).forEach((s, qi) => {
    if (s.sel) card.querySelectorAll('.ask-option[data-q="' + qi + '"]').forEach(b => {
      if (b.querySelector('b').textContent === s.sel) b.classList.add('selected');
    });
    if (s.val) { const inp = card.querySelector('.ask-input[data-q="' + qi + '"]'); if (inp) inp.value = s.val; }
  });
  card.querySelectorAll('.ask-option').forEach(btn => {
    btn.addEventListener('click', () => {
      const qi = btn.dataset.q;
      card.querySelectorAll('.ask-option[data-q="' + qi + '"]').forEach(b => b.classList.remove('selected'));
      btn.classList.add('selected');
    });
  });
  card.querySelector('#ask-submit').addEventListener('click', () => collectAskAnswers(card));
  return card;
}
function buildAnsweredAskCard(rec) {
  const card = document.createElement('div');
  card.className = 'msg ask-card answered';
  const qs = rec.questions || [];
  const ans = Array.isArray(rec.answers) ? rec.answers : (rec.answers != null ? [rec.answers] : null);
  card.innerHTML = qs.map((q, qi) => {
    const labels = (q.options || []).map(o => o.label);
    const a = ans ? (ans[qi] ?? '') : '';
    const isCustom = a && !labels.includes(a);
    const opts = (q.options || []).map(o =>
      '<button type="button" class="ask-option' + (!isCustom && o.label === a ? ' selected' : '') + '" style="cursor:default"><b>' + escapeHtml(o.label) + '</b>'
      + (o.description ? '<br><span class="ask-desc">' + escapeHtml(o.description) + '</span>' : '') + '</button>').join('');
    let row = '<div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div>'
      + '<div class="ask-options">' + opts + '</div>';
    if (rec.status && rec.status !== 'answered') row += '<div class="ask-a">\u23F3 未回答</div>';
    else if (isCustom || !labels.length) row += '<div class="ask-a">\u2705 ' + escapeHtml(a) + '</div>';
    return '<div class="ask-block">' + row + '</div>';
  }).join('');
  return card;
}
function collectAskAnswers(card) {
  const qs = card._askQuestions || [];
  const vals = [];
  for (let qi = 0; qi < qs.length; qi++) {
    const sel = card.querySelector('.ask-option.selected[data-q="' + qi + '"]');
    const inp = card.querySelector('.ask-input[data-q="' + qi + '"]');
    const label = sel ? sel.querySelector('b').textContent : '';
    const note = inp ? inp.value.trim() : '';
    // Option + typed note compose ("Label: note"); neither silently wipes the other.
    const v = label && note ? label + ': ' + note : (label || note);
    if (!v) { if (inp) { inp.focus(); inp.placeholder = '必填'; } return; }
    vals.push(v);
  }
  const rec = (turn && turn.entries || []).find(x => x.kind === 'ask' && x.id === card._askId);
  if (rec) { rec.answers = vals; rec.active = false; }
  const answered = buildAnsweredAskCard({ questions: qs, answers: vals, status: 'answered' });
  answered.classList.add('live-node');
  card.replaceWith(answered);
  fetch(api('/answer'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: card._askId, value: vals }) }).catch(() => {});
}

/* ---------- pending card asks (consent / cross-host, out-of-band) ----------
   Contract: register in pendingAskCards + re-mount after every full repaint
   (placeAskCards). Missing either makes cards vanish mid-stream. */
const pendingAskIds = new Set();
const pendingAskCards = new Map();
function placeAskCards() {
  pendingAskCards.forEach(({ rec, el }) => {
    // A pending ask belongs to the conversation that raised it: inline in the
    // open friend view, otherwise the global banner above the input.
    if (friendView && rec.conv === friendView) {
      msgs.appendChild(el);
    } else if (el.parentElement !== banner) {
      banner.appendChild(el);
    }
  });
}
function buildPendingAskCard(a) {
  const card = document.createElement('div');
  card.className = 'msg ask-card pending-ask';
  const from = escapeHtml(String(a.from || a.src || 'remote host').split(':')[0]);
  if (a.kind === 'consent') {
    const q = a.questions[0] || { question: '(consent request)' };
    card.innerHTML = '<div class="ask-from">\u{1F344} ' + from + ' \u00b7 consent</div>'
      + '<div class="ask-block"><div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div></div>'
      + '<div class="ask-consent-actions"><input placeholder="自定义回复（可选）">'
      + '<button class="ask-allow">允许</button>'
      + '<button class="ask-deny">禁止</button><button class="ask-send">发送</button></div>';
    const inp = card.querySelector('input');
    const send = v => answerPendingAsk(a, card, v);
    card.querySelector('.ask-allow').addEventListener('click', () => send(inp.value.trim() ? 'yes: ' + inp.value.trim() : 'yes'));
    card.querySelector('.ask-deny').addEventListener('click', () => send(inp.value.trim() ? 'no: ' + inp.value.trim() : 'no'));
    card.querySelector('.ask-send').addEventListener('click', () => { if (inp.value.trim()) send(inp.value.trim()); else inp.focus(); });
  } else {
    card.innerHTML = '<div class="ask-from">\u{1F344} ' + from + '</div>'
      + a.questions.map((q, qi) => '<div class="ask-block">' + askQuestionHtml(q, qi) + '</div>').join('')
      + '<div class="ask-actions"><button class="ask-send">提交</button></div>';
    card.querySelectorAll('.ask-option').forEach(btn => {
      btn.addEventListener('click', () => {
        const qi = btn.dataset.q;
        card.querySelectorAll('.ask-option[data-q="' + qi + '"]').forEach(b => b.classList.remove('selected'));
        btn.classList.add('selected');
      });
    });
    card.querySelector('.ask-send').addEventListener('click', () => {
      const qs = a.questions || [];
      const vals = [];
      for (let qi = 0; qi < qs.length; qi++) {
        const sel = card.querySelector('.ask-option.selected[data-q="' + qi + '"]');
        const inp = card.querySelector('.ask-input[data-q="' + qi + '"]');
        const label = sel ? sel.querySelector('b').textContent : '';
        const note = inp ? inp.value.trim() : '';
        const v = label && note ? label + ': ' + note : (label || note);
        if (!v) { if (inp) { inp.focus(); inp.placeholder = '必填'; } return; }
        vals.push(v);
      }
      answerPendingAsk(a, card, vals.length === 1 ? vals[0] : vals);
    });
  }
  return card;
}
function answerPendingAsk(a, card, value) {
  fetch(api('/answer'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: a.id, value }) }).catch(() => {});
  pendingAskIds.delete(a.id);
  pendingAskCards.delete(a.id);
  const done = document.createElement('div');
  done.className = 'msg ask-card answered';
  const verdict = value === 'no' ? '\u274C 已拒绝' : '\u2705 ' + (Array.isArray(value) ? value.join(', ') : value);
  done.textContent = (a.kind === 'consent' ? 'Consent ' : 'Ask ') + verdict;
  const finish = () => { card.replaceWith(done); setTimeout(() => done.remove(), 8000); };
  if (motionOn()) gsap.to(card, { opacity: 0, scale: 0.92, duration: 0.35, ease: 'power2.in', onComplete: finish });
  else finish();
}
async function pollPendingAsks() {
  try {
    const d = await (await fetchJSON('/asks')).json();
    (d.asks || []).forEach(a => {
      if (pendingAskIds.has(a.id)) return;
      pendingAskIds.add(a.id);
      const el = buildPendingAskCard(a);
      pendingAskCards.set(a.id, { rec: a, el });
      banner.appendChild(el);
      if (motionOn()) gsap.from(el, { opacity: 0, y: -14, duration: 0.4, ease: 'power3.out', clearProps: 'all' });
    });
  } catch (e) {}
}
setInterval(pollPendingAsks, 3000);

/* ---------- friends: room members + read-only comm clone conversations ---------- */
let friendView = null;   // host name while viewing a friend conversation
let allPeers = [];       // [{name, display}]
let lastFriendPayload = null; // rendered /comm-log JSON: skip no-change repaints

function peerName(p) { return typeof p === 'string' ? p : (p && p.name) || ''; }
function peerDisplay(p) { return typeof p === 'string' ? p : ((p && p.display) || peerName(p)); }
function displayOf(host) {
  for (const p of allPeers) if (peerName(p) === host) return peerDisplay(p);
  return host;
}

function leaveFriendView() {
  const wasViewing = friendView !== null;
  friendView = null;
  lastFriendPayload = null;
  document.getElementById('input-area').style.display = '';
  document.getElementById('friend-bar').classList.add('hidden');
  document.getElementById('btn-back').hidden = true;
  renderFriendList();
  if (wasViewing) {
    // openFriendChat wiped the message area without touching session state:
    // re-render the session that was on screen.
    msgs.innerHTML = '';
    if (currentSessionId) reloadSessionFromServer();
  }
}

async function loadPeers() {
  try {
    const r = await fetchJSON('/peers');
    if (!r.ok) return;
    const d = await r.json();
    allPeers = d.peers || [];
    document.getElementById('friends-count').textContent = allPeers.length ? '(' + allPeers.length + ')' : '';
    document.getElementById('friends-empty').style.display = allPeers.length ? 'none' : '';
    renderFriendList();
    if (friendView) refreshFriendChat();
  } catch (e) {}
}

function renderFriendList() {
  const list = document.getElementById('friend-list');
  list.querySelectorAll('.friend-row').forEach(r => r.remove());
  allPeers.forEach(p => {
    const name = peerName(p);
    const row = document.createElement('div');
    row.className = 'friend-row' + (name === friendView ? ' active' : '');
    row.innerHTML = '<span class="friend-dot"></span><span>' + escapeHtml(peerDisplay(p)) + '</span>';
    row.addEventListener('click', () => { openFriendChat(name); closeDrawer(); });
    list.appendChild(row);
  });
}

async function openFriendChat(host) {
  leaveFriendView();
  friendView = host;
  lastFriendPayload = null;
  msgs.innerHTML = '';
  document.getElementById('input-area').style.display = 'none';
  document.getElementById('btn-back').hidden = false;
  document.getElementById('friend-bar').classList.remove('hidden');
  document.getElementById('session-title').textContent = '@' + displayOf(host) + ' · 只读';
  renderFriendList();
  try {
    const cm = await (await fetchJSON('/consent-mode?host=' + encodeURIComponent(host))).json();
    setConsentSeg(cm.mode || 'ask');
  } catch (e) { setConsentSeg('ask'); }
  await refreshFriendChat();
}

function setConsentSeg(mode) {
  document.querySelectorAll('#consent-seg button').forEach(b =>
    b.classList.toggle('on', b.dataset.v === mode));
}
document.querySelectorAll('#consent-seg button').forEach(b => {
  b.addEventListener('click', () => {
    if (!friendView) return;
    setConsentSeg(b.dataset.v);
    fetch(api('/consent-mode'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: friendView, mode: b.dataset.v }) }).catch(() => {});
  });
});

async function refreshFriendChat() {
  const host = friendView;
  if (!host) return;
  try {
    const r = await fetchJSON('/comm-log?host=' + encodeURIComponent(host));
    if (!r.ok) return;
    const d = await r.json();
    if (friendView !== host) return; // raced a switch away: never paint here
    const payload = JSON.stringify(d);
    if (payload === lastFriendPayload) return; // unchanged: no flicker
    lastFriendPayload = payload;
    renderFriendChat(d);
  } catch (e) {}
}

/* Friend view renders the comm clone's transcript exactly like a local
   session, plus file-transfer envelope events that never produce turns. */
function renderFriendChat(d) {
  msgs.innerHTML = '';
  const messages = d.messages || [];
  const events = d.events || [];
  if (!messages.length && !events.length) {
    addDiv('friend-event', '<i>还没有和该好友的 clone 对话记录。</i>');
    return;
  }
  renderTranscript(messages, d.asks || []);
  events.forEach(row => {
    if (row.kind === 'transfer')
      addDiv('msg friend-event', '&#x1F4C4 ' + escapeHtml(row.text || 'file transfer'));
    else if (row.kind === 'task')
      addDiv('msg friend-event', '&#x1F4E5 delegated to ' + escapeHtml(row.dst || '?') + ': ' + escapeHtml((row.text || '').slice(0, 200)));
    else if (row.kind === 'result')
      addDiv('msg friend-event', '&#x2714 ' + escapeHtml(row.src || '?') + ' replied: ' + escapeHtml((row.text || '').slice(0, 200)));
  });
  placeAskCards(); // re-seat pending asks after the transcript repaint
  msgs.scrollTop = msgs.scrollHeight;
}

/* ---------- agent tray (live subagent bubbles + bottom-sheet replay) ---------- */
const agents = {};         // live spec id -> {layer, goal, replyFormat, status, history}
const specByCall = {};     // live tool_call id -> spec id

function agentBubble(id) {
  const tray = document.getElementById('agent-tray');
  let el = tray.querySelector('[data-agent="' + id + '"]');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'agent-bubble';
  el.dataset.agent = id;
  el.dataset.st = 'running';
  el.innerHTML = '<span class="st"></span><span class="nm">' + escapeHtml(agents[id].goal.slice(0, 24)) + '</span>';
  el.addEventListener('click', () => openAgentModal(id));
  tray.appendChild(el);
  if (motionOn()) gsap.from(el, { opacity: 0, y: 10, duration: 0.3, ease: 'power3.out', clearProps: 'all' });
  return el;
}
function setAgentStatus(id, st) {
  if (!agents[id]) return;
  agents[id].status = st;
  const el = agentBubble(id);
  el.dataset.st = st;
  if (st !== 'running') setTimeout(() => el.remove(), 12000); // finished: fade out of the tray
}
function agentEvent(id, ev) {
  const el = document.getElementById('agent-tray').querySelector('[data-agent="' + id + '"]');
  if (el && ev && ev.kind) el.querySelector('.nm').textContent = String(ev.text || ev.kind).slice(0, 24);
}
function openAgentModal(id) {
  const a = agents[id];
  if (!a) return;
  const ov = document.getElementById('agent-modal-overlay');
  const evs = (a.history || []).slice(-30).map(ev =>
    '<div class="am-ev">' + escapeHtml((ev.kind || '?') + ' · ' + String(ev.text || '').slice(0, 300)) + '</div>').join('');
  ov.innerHTML = '<div id="agent-modal"><h3>' + escapeHtml(a.layer || 'subagent') + '</h3>'
    + '<div class="am-goal">' + escapeHtml(a.goal) + '</div>'
    + '<div class="am-status">' + escapeHtml(a.status) + (a.replyFormat ? ' · 回复格式: ' + escapeHtml(a.replyFormat) : '') + '</div>'
    + (evs || '<div class="am-ev">（暂无事件）</div>') + '</div>';
  ov.classList.add('show');
  ov.onclick = e => { if (e.target === ov) { ov.classList.remove('show'); ov.innerHTML = ''; } };
}

/* ---------- drawer gestures (right-swipe open, left-swipe close) ---------- */
const drawer = document.getElementById('drawer'), scrim = document.getElementById('drawer-scrim');
const chatPage = document.getElementById('chat-page');
let drawerOpen = false, drag = null;
function drawerW() { return drawer.offsetWidth; }
function setDrawer(x, scrimOp) {
  gsap.set(drawer, { x });
  gsap.set(scrim, { opacity: scrimOp });
}
function applyDrawer(open, opts = {}) {
  const wasOpen = drawerOpen;
  drawerOpen = open;
  const dur = opts.instant ? 0 : 0.42;
  gsap.to(drawer, { x: open ? 0 : -drawerW(), duration: dur, ease: open ? 'power3.out' : 'power3.in', overwrite: 'auto' });
  gsap.to(scrim, {
    opacity: open ? 1 : 0, duration: dur, overwrite: 'auto',
    onStart: () => { if (open) scrim.style.pointerEvents = 'auto'; },
    onComplete: () => { if (!open) scrim.style.pointerEvents = 'none'; },
  });
  // Stagger only on a real closed->open transition: replaying it on every
  // drawer touchend would shift rows 24px under a finger and suppress the
  // synthesized click on the row buttons.
  if (open && !wasOpen && opts.instant !== true && motionOn()) {
    const rows = drawer.querySelectorAll('.session-row');
    if (rows.length) gsap.from(rows, { opacity: 0, x: -24, duration: 0.38, stagger: 0.035, ease: 'power3.out', clearProps: 'all', overwrite: 'auto' });
  }
}
function openDrawer() { applyDrawer(true); }
function closeDrawer() { applyDrawer(false); }
scrim.addEventListener('click', closeDrawer);
document.getElementById('btn-menu').addEventListener('click', openDrawer);
chatPage.addEventListener('touchstart', e => {
  const t = e.touches[0];
  // NO "if (drag) return" guard: a gesture the webview swallows (WeChat X5's
  // native edge handling often fires no end/cancel at all) used to wedge
  // drag truthy forever and silently kill every later swipe. A new touch
  // always supersedes stale state. The swipe may start ANYWHERE: the old 48px
  // edge wedge never triggered in real use and fought the phone's own edge
  // gesture; the vertical-lock in touchmove keeps normal scrolling intact.
  drag = { x0: t.clientX, y0: t.clientY, base: drawerOpen ? 0 : -drawerW(), w: drawerW(), locked: null, lastX: t.clientX, lastT: performance.now(), vx: 0 };
}, { passive: true });
chatPage.addEventListener('touchmove', e => {
  if (!drag) return;
  const t = e.touches[0];
  const dx = t.clientX - drag.x0, dy = t.clientY - drag.y0;
  if (drag.locked === null) {
    if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
    drag.locked = Math.abs(dx) > Math.abs(dy) ? 'h' : 'v';
    if (drag.locked === 'v') { drag = null; return; }
  }
  const x = Math.max(-drag.w, Math.min(0, drag.base + dx));
  const now = performance.now();
  drag.vx = (t.clientX - drag.lastX) / Math.max(1, now - drag.lastT);
  drag.lastX = t.clientX; drag.lastT = now;
  setDrawer(x, 1 + x / drag.w);
}, { passive: true });
function settleDrag(velSign) {
  // Shared finish for touchend AND touchcancel: position decides, but a fast
  // flick in the drag direction opens/closes even from a shallow drag —
  // touchcancel means the browser stole the gesture mid-flick, so position
  // alone would always settle closed.
  const d = drag; drag = null;
  if (!d) return;
  const x = parseFloat(gsap.getProperty(drawer, 'x'));
  const opened = x > -d.w / 2 || (d.vx || 0) * velSign > 0.35;
  applyDrawer(opened);
}
chatPage.addEventListener('touchend', () => { if (drag) settleDrag(1); }, { passive: true });
chatPage.addEventListener('touchcancel', () => { if (drag) settleDrag(1); }, { passive: true });
// drawer-side left swipe (finger starts on the drawer itself)
drawer.addEventListener('touchstart', e => {
  const t = e.touches[0];
  drawer._sw = { x0: t.clientX, y0: t.clientY, lastX: t.clientX, lastT: performance.now(), vx: 0 };
}, { passive: true });
drawer.addEventListener('touchmove', e => {
  const sw = drawer._sw;
  if (!sw || !drawerOpen) return;
  const t = e.touches[0];
  const dx = t.clientX - sw.x0, dy = t.clientY - sw.y0;
  if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 10) { drawer._sw = null; return; }
  const now = performance.now();
  sw.vx = (t.clientX - sw.lastX) / Math.max(1, now - sw.lastT);
  sw.lastX = t.clientX; sw.lastT = now;
  const x = Math.max(-drawerW(), Math.min(0, dx));
  if (x < 0) setDrawer(x, 1 + x / drawerW());
}, { passive: true });
drawer.addEventListener('touchend', () => {
  const sw = drawer._sw; drawer._sw = null;
  if (!sw || !drawerOpen) return;
  // A clean tap (row buttons!) must not re-run the drawer settle: the row
  // entrance shift would move the button away and swallow the click.
  if (Math.abs(sw.lastX - sw.x0) < 8 && Math.abs(sw.vx) < 0.1) return;
  const x = parseFloat(gsap.getProperty(drawer, 'x'));
  applyDrawer(!(x < -drawerW() / 2 || sw.vx < -0.35));
}, { passive: true });
drawer.addEventListener('touchcancel', () => { drawer._sw = null; }, { passive: true });

/* ---------- wiring ---------- */
document.getElementById('btn-new-session').addEventListener('click', newSession);
document.getElementById('session-filter').addEventListener('input', renderSessionList);
document.getElementById('btn-back').addEventListener('click', leaveFriendView);
btn.addEventListener('click', send);
function autoGrow() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 120) + 'px'; }
input.addEventListener('input', autoGrow);
/* ---------- file upload: phone picker -> PC inbox, path dropped in the box ---------- */
const fileInput = document.getElementById('file-input');
document.getElementById('btn-file').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', async () => {
  const files = Array.from(fileInput.files || []);
  fileInput.value = '';
  for (const f of files) {
    status.textContent = '上传中… ' + f.name;
    try {
      const fd = new FormData();
      fd.append('file', f, f.name);
      const d = await (await fetchJSON('/upload', { method: 'POST', body: fd })).json();
      if (d.path) {
        input.value = (input.value ? input.value + ' ' : '') + d.path;
        autoGrow();
      } else {
        status.textContent = '上传失败：' + f.name;
        return;
      }
    } catch (e) { return; } // 403: fetchJSON already showed the rescan overlay
  }
  status.textContent = '';
  input.focus();
});
// Keep the transcript pinned when the mobile keyboard resizes the viewport.
if (window.visualViewport) {
  visualViewport.addEventListener('resize', () => { if (isNearBottom(msgs)) msgs.scrollTop = msgs.scrollHeight; });
}
/* theme: light default, persisted (same key as desktop) */
function applyTheme(t) { document.documentElement.setAttribute('data-theme', t); document.querySelector('meta[name="theme-color"]').content = t === 'dark' ? '#12141c' : '#f0f2f8'; }
try { applyTheme(localStorage.getItem('fungi-theme') === 'dark' ? 'dark' : 'light'); } catch (e) { applyTheme('light'); }
document.getElementById('theme-switch').addEventListener('click', function () {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  const apply = () => {
    applyTheme(next);
    try { localStorage.setItem('fungi-theme', next); } catch (e) {}
  };
  // Day-night wash: an accent-tinted circle expands from the switch, the
  // color flip happens once covered, then the wash fades (desktop themeTo).
  if (!motionOn()) { apply(); return; }
  const wash = document.createElement('div');
  wash.className = 'motion-theme-wash ' + next;
  const r = this.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const R = Math.hypot(Math.max(cx, innerWidth - cx), Math.max(cy, innerHeight - cy)) + 40;
  wash.style.clipPath = 'circle(0px at ' + cx + 'px ' + cy + 'px)';
  document.body.appendChild(wash);
  gsap.to(wash, {
    clipPath: 'circle(' + R + 'px at ' + cx + 'px ' + cy + 'px)',
    duration: 0.34, ease: 'power2.in',
    onComplete: () => {
      apply();
      gsap.to(wash, { opacity: 0, duration: 0.3, ease: 'power1.out', delay: 0.06, onComplete: () => wash.remove() });
    }
  });
});

/* ---------- boot ---------- */
setBusy(false);
if (!TOKEN) unauthorized(); // no token in URL or storage: rescan required
else document.getElementById('rescan-overlay').classList.add('hide'); // valid token: drop the rescan card
fetchJSON('/model').then(r => r.json()).then(d => { document.getElementById('model-name').textContent = d.model || ''; }).catch(() => {});
loadSessions().then(() => {
  let saved = null;
  try { saved = localStorage.getItem('fungi-session-m'); } catch (e) {}
  if (saved && allSessions.some(s => s.id === saved)) switchSession(saved);
  else renderSessionList();
});
pollPendingAsks();
loadPeers();
setInterval(loadPeers, 5000);
