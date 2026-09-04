/* Fungi web UI */
marked.setOptions({ breaks: true, gfm: true });
const msgs = document.getElementById('messages'), input = document.getElementById('input'),
  btn = document.getElementById('send'), status = document.getElementById('status'),
  tray = document.getElementById('agent-tray');
let processing = false, currentSessionId = null, allSessions = [], sessionDirty = false;
let rawMessages = [];

/* ---------- helpers ---------- */
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function addDiv(cls, html, id) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  if (id) d.id = id;
  if (html) d.innerHTML = html;
  msgs.appendChild(d);
  if (isNearBottom(msgs)) msgs.scrollTop = msgs.scrollHeight;
  updateScrollBtn();
  return d;
}
function isNearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 60; }
function updateScrollBtn() {
  const b = document.getElementById('scroll-bottom');
  if (!b) return;
  if (isNearBottom(msgs)) b.classList.remove('visible'); else b.classList.add('visible');
}
document.getElementById('scroll-bottom').addEventListener('click', () => { msgs.scrollTop = msgs.scrollHeight; updateScrollBtn(); });
msgs.addEventListener('scroll', updateScrollBtn);

function getSessionTitle(list) {
  const u = list.find(m => m.role === 'user');
  if (!u) return 'Empty';
  const t = String(u.content).replace(/\s+/g, ' ').trim();
  return t.length > 55 ? t.slice(0, 52) + '...' : t;
}

/* ---------- confirm modal ---------- */
let _confirmState = null;
function showConfirm(opts) {
  const overlay = document.getElementById('confirm-overlay');
  const ok = document.getElementById('confirm-ok');
  document.getElementById('confirm-title').textContent = opts.title || 'Are you sure?';
  document.getElementById('confirm-message').textContent = opts.message || '';
  ok.textContent = opts.confirmText || 'OK';
  document.getElementById('confirm-cancel').textContent = opts.cancelText || 'Cancel';
  ok.classList.toggle('danger', !!opts.danger);
  _confirmState = { onConfirm: opts.onConfirm, danger: !!opts.danger };
  overlay.classList.add('show');
  document.getElementById('confirm-cancel').focus();
}
function closeConfirm(confirmed) {
  const overlay = document.getElementById('confirm-overlay');
  if (!overlay.classList.contains('show')) return;
  overlay.classList.remove('show');
  const state = _confirmState;
  _confirmState = null;
  if (confirmed && state && state.onConfirm) state.onConfirm();
}
(function initConfirmModal() {
  const overlay = document.getElementById('confirm-overlay');
  document.getElementById('confirm-ok').addEventListener('click', () => closeConfirm(true));
  document.getElementById('confirm-cancel').addEventListener('click', () => closeConfirm(false));
  overlay.addEventListener('mousedown', e => { if (e.target === overlay) closeConfirm(false); });
  document.addEventListener('keydown', e => {
    if (!overlay.classList.contains('show')) return;
    if (e.key === 'Escape') { e.preventDefault(); closeConfirm(false); }
    // Enter confirms non-danger prompts only; destructive actions need a real click.
    if (e.key === 'Enter' && _confirmState && !_confirmState.danger) {
      e.preventDefault();
      closeConfirm(true);
    }
  });
})();

/* ---------- sessions ---------- */
async function loadSessions() {
  try {
    const r = await fetch('/sessions');
    if (!r.ok) throw new Error(r.status);
    allSessions = (await r.json()).sessions || [];
    renderSessionList();
  } catch (e) { console.error('loadSessions:', e); }
}
async function reloadSessionFromServer() {
  if (!currentSessionId) return;
  try {
    const r = await fetch('/session?id=' + encodeURIComponent(currentSessionId));
    if (!r.ok) return;
    const s = await r.json();
    rawMessages = s.messages || [];
    registerArchived(s.subagents);
    renderMessages(s);
    loadSessions();
  } catch (e) {}
}
async function closeCurrentSession() {
  if (!currentSessionId) return;
  if (processing && turn && turn.sessionId === currentSessionId) {
    // A turn is running in this session: never delete or save over it. The
    // server persists the whole turn on its own at turn end — just detach.
    // (2026-09-04: switching sessions before the first token arrived deleted
    // the session out from under the running turn — the response vanished.)
    currentSessionId = null;
    return;
  }
  if (!sessionDirty) { currentSessionId = null; return; }
  const list = rawMessages;
  if (!list || list.length <= 1) {
    try { await fetch('/session?id=' + encodeURIComponent(currentSessionId), { method: 'DELETE' }); } catch (e) {}
    currentSessionId = null;
  } else {
    const title = getSessionTitle(list);
    try {
      await fetch('/save', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: currentSessionId, title, messages: list }) });
    } catch (e) {}
  }
  loadSessions();
}
async function switchSession(id) {
  leaveFriendView();
  if (id === currentSessionId) return;
  await closeCurrentSession();
  sessionDirty = false;
  try {
    const r = await fetch('/session?id=' + encodeURIComponent(id));
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    currentSessionId = s.id;
    rawMessages = s.messages || [];
    msgs.innerHTML = '';
    tray.innerHTML = '';
    registerArchived(s.subagents);
    renderMessages(s);
    renderSessionList(); loadSessions();
  } catch (e) { console.error('switchSession:', e); }
}

function renderMessages(s) {
  msgs.querySelectorAll('.live-node').forEach(n => n.remove());
  renderTranscript(rawMessages, s.asks || []);
  if (turn && turn.sessionId === currentSessionId) renderTurnLive();
  placeAskCards(); // friend-view inline cards move back to the banner here
}

/* Session-style rendering shared by local sessions and friend transcripts:
   markdown text, reasoning details, tool blocks, answered ask cards. */
function renderTranscript(messages, asks) {
  let toolBlocks = {};
  const askQueue = (asks || []).slice(); // replayed in order at their confirm call site (legacy ask_user too)
  for (const m of messages || []) {
    if (m.role === 'user') addDiv('user', marked.parse(m.content || ''));
    else if (m.role === 'assistant') {
      if (m.reasoning) {
        const det = document.createElement('details');
        det.className = 'msg reasoning';
        det.innerHTML = '<summary>Thinking\u2026</summary><div style="white-space:pre-wrap;max-height:200px;overflow-y:auto">' + escapeHtml(m.reasoning) + '</div>';
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
        const argsHtml = args ? ' <code style="font-size:0.82rem;opacity:0.7">' + escapeHtml(args.length > 80 ? args.slice(0, 80) + '...' : args) + '</code>' : '';
        d.innerHTML = '<div class="tool-label">&#x1F527; ' + escapeHtml(tc.function?.name || 'tool') + argsHtml + '</div><div class="tool-result"></div>';
        msgs.appendChild(d);
        if (tc.function?.name === 'spawn') makeSpawnBlockClickable(d, tc.id);
        if (tc.function?.name === 'confirm' || tc.function?.name === 'ask_user') { // ask_user: pre-rename transcripts
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
  askQueue.forEach(renderArchivedAsk); // leftovers (e.g. turn died mid-ask)
}
async function newSession() {
  leaveFriendView();
  // Any untouched "(new session)" on disk? Focus it instead of creating
  // another one — repeated clicks and switches must not litter the list.
  const empty = allSessions.find(s => s.title === '(new session)' && (s.msgCount || 0) <= 1);
  if (empty) {
    if (empty.id !== currentSessionId) await switchSession(empty.id);
    return;
  }
  await closeCurrentSession();
  try {
    const r = await fetch('/new', { method: 'POST' });
    if (!r.ok) throw new Error(r.status);
    const { id } = await r.json();
    currentSessionId = id; rawMessages = []; msgs.innerHTML = ''; tray.innerHTML = '';
    sessionDirty = false;
    await loadSessions();
  } catch (e) { console.error('newSession:', e); }
}
async function deleteSession(id) {
  try {
    await fetch('/session?id=' + encodeURIComponent(id), { method: 'DELETE' });
    if (id === currentSessionId) { currentSessionId = null; msgs.innerHTML = ''; tray.innerHTML = ''; }
    document.getElementById('session-filter').value = '';
    await loadSessions();
  } catch (e) {}
}
function renderSessionList() {
  const list = document.getElementById('session-list');
  const empty = document.getElementById('session-list-empty');
  const filter = (document.getElementById('session-filter')?.value || '').trim().toLowerCase();
  const filtered = filter ? allSessions.filter(s => (s.title || '').toLowerCase().includes(filter)) : allSessions;
  list.querySelectorAll('.session-row').forEach(r => r.remove());
  if (filtered.length === 0) { empty.style.display = ''; empty.textContent = filter ? 'No matches.' : 'No sessions yet.'; return; }
  empty.style.display = 'none';
  filtered.forEach(s => {
    const row = document.createElement('div');
    row.className = 'session-row' + (s.id === currentSessionId ? ' active' : '');
    row.innerHTML = '<span class="session-row-title">' + escapeHtml(s.title || 'Untitled') + '</span>'
      + '<span class="session-row-meta">' + fmtDate(s.created) + '</span>'
      + '<span class="session-row-actions"><button class="session-row-act" title="Rename">&#9998;</button>'
      + '<button class="session-row-act del" title="Delete">&#10005;</button></span>';
    row.querySelector('.session-row-act.del').addEventListener('click', e => {
      e.stopPropagation();
      showConfirm({
        title: 'Delete session',
        message: '"' + (s.title || 'Untitled') + '" will be permanently removed. This cannot be undone.',
        confirmText: 'Delete',
        danger: true,
        onConfirm: () => deleteSession(s.id)
      });
    });
    row.querySelector('.session-row-act:not(.del)').addEventListener('click', e => { e.stopPropagation(); startRename(row, s); });
    row.addEventListener('click', () => switchSession(s.id));
    list.appendChild(row);
  });
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
  return new Date(d).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}
function startRename(row, s) {
  const titleEl = row.querySelector('.session-row-title');
  const old = titleEl.textContent;
  const inp = document.createElement('input');
  inp.className = 'rename-input'; inp.value = old;
  inp.addEventListener('blur', () => finishRename(row, s, inp));
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') finishRename(row, s, inp);
    if (e.key === 'Escape') { row.replaceChild(titleEl, inp); titleEl.textContent = old; }
  });
  row.replaceChild(inp, titleEl); inp.focus(); inp.select();
}
async function finishRename(row, s, inp) {
  const newTitle = inp.value.trim() || 'Untitled';
  try {
    await fetch('/save', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: s.id, title: newTitle, messages: [] }) });
  } catch (e) {}
  const titleEl = document.createElement('span');
  titleEl.className = 'session-row-title'; titleEl.textContent = newTitle;
  row.replaceChild(titleEl, inp); s.title = newTitle; renderSessionList();
}

/* ---------- sidebar wiring ---------- */
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('collapsed'); }
document.getElementById('sidebar-toggle').addEventListener('click', toggleSidebar);
document.getElementById('hamburger-sidebar').addEventListener('click', toggleSidebar);
document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 'b') { e.preventDefault(); toggleSidebar(); } });
document.getElementById('session-filter').addEventListener('input', renderSessionList);
document.getElementById('btn-new-session').addEventListener('click', () => newSession());

/* ---------- agent tray (bubbles while running) + replayable modal ---------- */
const agents = {};         // live spec id -> {layer, goal, replyFormat, status, history, eventsEl}
const specByCall = {};     // live tool_call id -> spec id
let archived = {};         // persisted spec id -> same shape as live (history = events)
const archivedByCall = {}; // persisted tool_call id -> spec id

function agentBubble(id) {
  const a = agents[id];
  const b = document.createElement('div');
  b.className = 'agent-bubble ' + (a.layer === 3 ? 'layer3' : '') + ' ' + a.status;
  b.id = 'bubble-' + id;
  b.title = (a.layer === 3 ? 'L3' : 'L2') + ': ' + a.goal;
  b.innerHTML = 'L' + a.layer + '<span class="agent-status-dot"></span>';
  b.addEventListener('click', () => openAgentModal(id));
  tray.appendChild(b);
  return b;
}
function setAgentStatus(id, st) {
  const a = agents[id];
  if (!a) return;
  a.status = st;
  const b = document.getElementById('bubble-' + id);
  if (b) {
    b.classList.remove('running', 'done', 'failed');
    b.classList.add(st);
    if (st === 'done' || st === 'failed') {
      // bubbles are transient: only visible while the subagent runs
      setTimeout(() => { b.remove(); }, 1500);
    }
  }
  if (a.eventsEl) {
    const label = { done: '\u2714 finished', failed: '\u2718 failed' }[st];
    if (label) a.eventsEl.insertAdjacentHTML('beforeend', '<div class="ev final-ev">' + label + '</div>');
  }
}
function agentEvent(id, ev) {
  const a = agents[id] || archived[id];
  if (!a || !a.eventsEl) return;
  const body = a.eventsEl;
  if (ev.type === 'text' || ev.type === 'reasoning') {
    let last = body.lastElementChild;
    if (!last || !last.classList.contains('stream-ev')) {
      body.insertAdjacentHTML('beforeend', '<div class="ev stream-ev"></div>');
      last = body.lastElementChild;
    }
    last.textContent += ev.content || '';
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'tool') {
    body.insertAdjacentHTML('beforeend', '<div class="ev tool-ev">\u{1F527} ' + escapeHtml(ev.content.name) + ' ' + escapeHtml((ev.content.args || '').slice(0, 120)) + '</div>');
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'tool_result') {
    const text = String(ev.content.content || '');
    body.insertAdjacentHTML('beforeend', '<div class="ev">' + escapeHtml(text.slice(0, 400)) + (text.length > 400 ? '...' : '') + '</div>');
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'agent_spawn') {
    body.insertAdjacentHTML('beforeend', '<div class="ev tool-ev">\u{1F9E9} spawn L' + (ev.content.layer || '?') + ': ' + escapeHtml((ev.content.goal || '').slice(0, 120)) + '</div>');
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'agent_status') {
    body.insertAdjacentHTML('beforeend', '<div class="ev">' + escapeHtml(ev.content.status) + '</div>');
    body.scrollTop = body.scrollHeight;
  } else if (ev.type === 'error') {
    body.insertAdjacentHTML('beforeend', '<div class="ev err-ev">\u26A0 ' + escapeHtml(ev.content) + '</div>');
    body.scrollTop = body.scrollHeight;
  }
}
function openAgentModal(id) {
  const a = agents[id] || archived[id];
  if (!a) return;
  const overlay = document.getElementById('agent-modal-overlay');
  overlay.innerHTML = '<div id="agent-modal">'
    + '<div class="agent-modal-head"><h3>' + (a.layer === 3 ? 'L3 Worker' : 'L2 Task Agent') + ' \u00B7 ' + escapeHtml((a.goal || '').slice(0, 60)) + '</h3>'
    + '<button id="agent-modal-close">\u2715</button></div>'
    + '<div class="agent-modal-taskspec"><b>Goal:</b> ' + escapeHtml(a.goal || '')
    + '<br><b>Reply format:</b> ' + escapeHtml(a.replyFormat || '(free)')
    + '<br><b>Status:</b> ' + escapeHtml(a.status || 'unknown') + '</div>'
    + '<div class="agent-modal-body"></div></div>';
  const bodyEl = overlay.querySelector('.agent-modal-body');
  a.eventsEl = bodyEl;
  (a.history || []).forEach(ev => agentEvent(id, ev));
  overlay.querySelector('#agent-modal-close').addEventListener('click', () => {
    overlay.classList.remove('show'); a.eventsEl = null;
  });
  overlay.classList.add('show');
}
function makeSpawnBlockClickable(el, callId) {
  el.classList.add('spawn-block');
  el.title = 'Click to view subagent details';
  el.addEventListener('click', () => {
    const id = specByCall[callId] || archivedByCall[callId];
    if (id) openAgentModal(id);
  });
}
function registerArchived(subs) {
  archived = {};
  for (const k of Object.keys(archivedByCall)) delete archivedByCall[k];
  (subs || []).forEach(r => {
    archived[r.id] = {
      layer: r.layer, goal: r.goal, replyFormat: r.reply_format,
      status: r.status, history: r.events || [],
    };
    if (r.call_id) archivedByCall[r.call_id] = r.id;
  });
}

/* ---------- send / stream ---------- */
let abortCtrl = null;
let stopRequested = false; // first Esc: graceful stop via /stop; second Esc: hard disconnect
let stopTimer = null;
let turn = null; // active turn: {sessionId, buffer, reasoning, reasoningOpen, tools, asks, errors}

async function send() {
  const text = input.value.trim();
  if (!text || processing) return;
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
  if (!currentSessionId) {
    try {
      const r = await fetch('/new', { method: 'POST', signal: abortCtrl.signal });
      const { id } = await r.json();
      currentSessionId = id; loadSessions();
    } catch (e) {}
  }
  const sid = currentSessionId;
  turn = { sessionId: sid, entries: [] }; // chronological: reasoning/text/tool/ask/error
  rawMessages.push({ role: 'user', content: text });
  sessionDirty = true;
  turn.userText = text;
  input.value = ''; btn.disabled = true; status.textContent = 'Thinking...';
  renderTurnLive();
  await pumpStream('/chat', { message: text, sessionId: sid });
  if (currentSessionId === sid) input.focus();
}

/* Alt+R: rerun the failed/stopped turn with no new prompt. */
async function retryTurn() {
  if (processing || !currentSessionId) return;
  processing = true;
  abortCtrl = new AbortController(); stopRequested = false;
  if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
  const sid = currentSessionId;
  turn = { sessionId: sid, entries: [] };
  sessionDirty = true;
  btn.disabled = true;
  status.textContent = 'Retrying...';
  renderTurnLive();
  await pumpStream('/retry', { sessionId: sid });
  if (currentSessionId === sid) input.focus();
}

async function pumpStream(url, body) {
  try {
    const resp = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), signal: abortCtrl.signal });
    if (!resp.ok) { status.textContent = 'Error: ' + resp.status; turn = null; return; }
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
  } catch (e) {
    if (e.name === 'AbortError') status.textContent = 'Aborted.';
    else status.textContent = 'Error: ' + e.message;
    turn = null;
  }
  if (turn) {
    // Stream ended without a done event (server died mid-turn): the UI used
    // to stay stuck on "Writing..." with a phantom live turn.
    status.textContent = 'Connection lost. Press Alt+R to retry.';
    turn = null;
  }
  processing = false; btn.disabled = false;
}

/* Turn events update the model first, then touch the DOM only when the
   turn's session is on screen — switching sessions mid-turn keeps the turn
   running in the background (no detached-node errors). */
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
      if (visible) renderTurnLive();
      break;
    case 'tool_result': {
      const rec = t.entries.find(x => x.kind === 'tool' && x.id === obj.content.id)
        || [...t.entries].reverse().find(x => x.kind === 'tool' && !x.result);
      if (rec) rec.result = obj.content.content;
      if (visible) { const block = document.getElementById('tool-' + obj.content.id); if (block) block.querySelector('.tool-result').innerHTML = '<pre>' + escapeHtml(obj.content.content) + '</pre>'; else renderTurnLive(); }
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
      status.textContent = t.aborted ? 'Aborted. Press Alt+R to continue.'
        : (failed ? 'Turn failed. Press Alt+R to retry.' : '');
      if (viewing) reloadSessionFromServer();
      else loadSessions(); // the finished turn landed in a background session
      break;
    }
  }
  if (visible && currentSessionId === t.sessionId) { if (isNearBottom(msgs)) msgs.scrollTop = msgs.scrollHeight; updateScrollBtn(); }
}

function updateLastText() {
  const t = turn;
  const idx = t.entries.map(x => x.kind).lastIndexOf('text');
  if (idx < 0) return renderTurnLive();
  const el = document.getElementById('live-text-' + idx);
  if (el) { el.innerHTML = marked.parse(t.entries[idx].content); if (isNearBottom(msgs)) msgs.scrollTop = msgs.scrollHeight; }
  else renderTurnLive();
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
  if (turn.userText) {
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
      ad.innerHTML = marked.parse(e.content);
      msgs.appendChild(ad);
    } else if (e.kind === 'tool') {
      const d = document.createElement('div');
      d.className = 'msg tool live-node'; d.id = 'tool-' + e.id;
      const argsHtml = e.args ? ' <code style="font-size:0.82rem;opacity:0.7">' + escapeHtml(e.args.length > 80 ? e.args.slice(0, 80) + '...' : e.args) + '</code>' : '';
      d.innerHTML = '<div class="tool-label">&#x1F527; ' + escapeHtml(e.name) + argsHtml + '</div><div class="tool-result">' + (e.result ? '<pre>' + escapeHtml(e.result) + '</pre>' : '') + '</div>';
      msgs.appendChild(d);
      if (e.name === 'spawn') makeSpawnBlockClickable(d, e.id);
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
}
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  if (e.key === 'Escape' && abortCtrl && turn) {
    const sid = turn.sessionId || currentSessionId;
    if (!stopRequested) {
      // First Esc: ask the server to stop the turn and keep the stream open —
      // the persisted partial reply comes back via done -> reload.
      stopRequested = true;
      status.textContent = 'Stopping...';
      if (sid) fetch('/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: sid }) }).catch(() => {});
      stopTimer = setTimeout(() => { if (abortCtrl) { abortCtrl.abort(); abortCtrl = null; } }, 15000);
    } else {
      // Second Esc: server is stuck (e.g. long tool call) — disconnect hard.
      if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
      stopRequested = false;
      abortCtrl.abort(); abortCtrl = null;
      status.textContent = 'Aborted.';
    }
  }
});
btn.addEventListener("click", send);
document.addEventListener('keydown', e => {
  if (e.altKey && (e.key === 'r' || e.key === 'R')) {
    if (processing || !currentSessionId) return;
    if (document.getElementById('confirm-overlay').classList.contains('show')) return;
    e.preventDefault();
    retryTurn();
  }
});
document.getElementById('btn-browse').addEventListener('click', async () => {
  try {
    const r = await fetch('/pickfile', { method: 'POST' });
    const d = await r.json();
    if (d.path) { input.value = (input.value ? input.value + ' ' : '') + d.path; input.focus(); }
  } catch (e) {}
});
fetch('/model').then(r => r.json()).then(d => { document.getElementById('model-name').textContent = ' \u2014 ' + d.model; });
loadSessions();

/* ---------- ask card (Inquire) ---------- */
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
      ? '<input class="ask-input" data-q="' + qi + '" placeholder="' + ((q.options || []).length ? 'Or type your own...' : 'Your answer...') + '">'
      : '');
}

function buildActiveAskCard(a, saved) {
  const card = document.createElement('div');
  card.className = 'msg ask-card live-node'; card.id = 'ask-card';
  card._askId = a.id; card._askQuestions = a.questions;
  card.innerHTML = a.questions.map((q, qi) => '<div class="ask-block">' + askQuestionHtml(q, qi) + '</div>').join('')
    + '<div class="ask-actions"><button id="ask-submit">Submit</button></div>';
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
  card.querySelectorAll('.ask-input').forEach(inp => {
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') collectAskAnswers(card); });
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
    if (rec.status && rec.status !== 'answered') row += '<div class="ask-a">\u23F3 No answer</div>';
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
    // Selected option + typed note compose ("Label: note"); either alone
    // stands as-is. No silent wiping in either direction.
    const v = label && note ? label + ': ' + note : (label || note);
    if (!v) { if (inp) { inp.focus(); inp.placeholder = 'Required'; } return; }
    vals.push(v);
  }
  const rec = (turn && turn.entries || []).find(x => x.kind === 'ask' && x.id === card._askId);
  if (rec) { rec.answers = vals; rec.active = false; }
  const answered = buildAnsweredAskCard({ questions: qs, answers: vals, status: 'answered' });
  answered.classList.add('live-node');
  card.replaceWith(answered);
  fetch('/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: card._askId, value: vals }) }).catch(() => {});
}

function renderArchivedAsk(rec) {
  msgs.appendChild(buildAnsweredAskCard(rec));
}


/* ---------- pending card asks (consent / cross-host asks, out-of-band) ---------- */
const pendingAskIds = new Set();
const pendingAskCards = new Map(); // ask id -> {rec, el} while the card is live
function placeAskCards() {
  // A pending ask belongs to the conversation that raised it: inline in the
  // matching open friend view, otherwise the global banner.
  const banner = document.getElementById('asks-banner');
  pendingAskCards.forEach(({ rec, el }) => {
    if (friendView && rec.conv === friendView) msgs.appendChild(el);
    else if (el.parentElement !== banner) banner.appendChild(el);
  });
}
function buildPendingAskCard(a) {
  const card = document.createElement('div');
  card.className = 'msg ask-card pending-ask';
  const fromRaw = String(a.from || a.src || 'remote host');
  const from = escapeHtml(displayOf(fromRaw.split(':')[0]));
  if (a.kind === 'consent') {
    const q = a.questions[0] || { question: '(consent request)' };
    card.innerHTML = '<div class="ask-from">\u{1F344} ' + from + ' \u00b7 consent</div>'
      + '<div class="ask-block"><div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div></div>'
      + '<div class="ask-consent-actions"><input placeholder="自定义回复（可选，留空直接点允许/禁止）">'
      + '<button class="ask-allow">允许</button>'
      + '<button class="ask-deny">禁止</button><button class="ask-send">Send</button></div>'
      + '<div class="ask-hint">· 需要长期放行？在好友会话顶部把滑块拨到「允许」</div>';
    const inp = card.querySelector('input');
    const send = v => answerPendingAsk(a, card, v);
    card.querySelector('.ask-allow').addEventListener('click', () => send(inp.value.trim() ? 'yes: ' + inp.value.trim() : 'yes'));
    card.querySelector('.ask-deny').addEventListener('click', () => send(inp.value.trim() ? 'no: ' + inp.value.trim() : 'no'));
    const custom = () => { if (inp.value.trim()) send(inp.value.trim()); else inp.focus(); };
    card.querySelector('.ask-consent-actions input').addEventListener('keydown', e => { if (e.key === 'Enter') custom(); });
    card.querySelector('.ask-send').addEventListener('click', custom);
  } else {
    card.innerHTML = '<div class="ask-from">\u{1F344} ' + from + '</div>'
      + a.questions.map((q, qi) => '<div class="ask-block">' + askQuestionHtml(q, qi) + '</div>').join('')
      + '<div class="ask-actions"><button class="ask-send">Submit</button></div>';
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
        // Same compose rule as live ask cards: option + typed note both
        // survive ("Label: note"); no silent wiping in either direction.
        const v = label && note ? label + ': ' + note : (label || note);
        if (!v) { if (inp) { inp.focus(); inp.placeholder = 'Required'; } return; }
        vals.push(v);
      }
      answerPendingAsk(a, card, vals.length === 1 ? vals[0] : vals);
    });
  }
  return card;
}
function answerPendingAsk(a, card, value) {
  fetch('/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: a.id, value }) }).catch(() => {});
  pendingAskIds.delete(a.id);
  pendingAskCards.delete(a.id);
  const done = document.createElement('div');
  done.className = 'msg ask-card answered';
  const verdict = value === 'no' ? '\u274C denied'
    : '\u2705 ' + (Array.isArray(value) ? value.join(', ') : value);
  done.textContent = (a.kind === 'consent' ? 'Consent ' : 'Ask ') + verdict;
  card.replaceWith(done);
  setTimeout(() => done.remove(), 8000);
}
async function pollPendingAsks() {
  try {
    const d = await (await fetch('/asks')).json();
    (d.asks || []).forEach(a => {
      if (pendingAskIds.has(a.id)) return;
      pendingAskIds.add(a.id);
      const el = buildPendingAskCard(a);
      pendingAskCards.set(a.id, { rec: a, el });
      if (friendView && a.conv === friendView) msgs.appendChild(el);
      else document.getElementById('asks-banner').appendChild(el);
    });
  } catch (e) {}
}
setInterval(pollPendingAsks, 3000);
pollPendingAsks();

/* ---------- config modal ---------- */
(async () => {
  try {
    const r = await fetch('/config-status');
    const d = await r.json();
    if (!d.configured) {
      document.getElementById('config-overlay').classList.add('show');
      document.getElementById('cfg-save').addEventListener('click', async () => {
        const api_key = document.getElementById('cfg-apikey').value.trim();
        const endpoint = document.getElementById('cfg-endpoint').value.trim();
        const model = document.getElementById('cfg-model').value.trim();
        if (!api_key) {
          const err = document.getElementById('cfg-err');
          err.textContent = 'API key is required.'; err.style.display = 'block';
          return;
        }
        try {
          const r2 = await fetch('/configure', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key, endpoint, model }) });
          const d2 = await r2.json();
          if (d2.ok) {
            document.getElementById('config-overlay').classList.remove('show');
            fetch('/model').then(r3 => r3.json()).then(d3 => {
              document.getElementById('model-name').textContent = ' \u2014 ' + d3.model;
            });
          }
        } catch (e) {}
      });
      document.getElementById('cfg-skip').addEventListener('click', () => {
        document.getElementById('config-overlay').classList.remove('show');
      });
    }
  } catch (e) {}
})();

/* ---------- friends: room members + read-only comm clone conversations ---------- */
let friendView = null; // host name while viewing a friend conversation (wire identity)
let allPeers = []; // [{name, display}] — display is the nickname, "" falls back to name
let lastFriendPayload = null; // rendered /comm-log JSON: skip no-change repaints

function peerName(p) { return typeof p === 'string' ? p : (p && p.name) || ''; }
function peerDisplay(p) { return typeof p === 'string' ? p : ((p && p.display) || peerName(p)); }
function displayOf(host) {
  for (const p of allPeers) if (peerName(p) === host) return peerDisplay(p);
  return host; // no display -> wire name
}

function leaveFriendView() {
  const wasViewing = friendView !== null;
  friendView = null;
  lastFriendPayload = null;
  document.getElementById('input-area').style.display = '';
  document.getElementById('friend-title').textContent = '';
  document.getElementById('friend-bar').classList.remove('visible');
  renderFriendList();
  if (wasViewing) {
    // openFriendChat wiped the message area without touching session state:
    // drop the friend rows, then re-render the session that was on screen.
    msgs.innerHTML = ''; tray.innerHTML = '';
    if (currentSessionId) reloadSessionFromServer();
  }
}

async function loadPeers() {
  try {
    const r = await fetch('/peers');
    if (!r.ok) return;
    const d = await r.json();
    allPeers = d.peers || [];
    document.getElementById('friends-count').textContent = allPeers.length ? '(' + allPeers.length + ')' : '';
    renderFriendList();
    if (friendView) refreshFriendChat();
  } catch (e) {}
}

function renderFriendList() {
  const list = document.getElementById('friend-list');
  const empty = document.getElementById('friends-empty');
  list.querySelectorAll('.friend-row').forEach(r => r.remove());
  if (!allPeers.length) { empty.style.display = ''; return; }
  empty.style.display = 'none';
  allPeers.forEach(p => {
    const name = peerName(p);
    const row = document.createElement('div');
    row.className = 'friend-row' + (name === friendView ? ' active' : '');
    row.title = name;
    row.innerHTML = '<span class="friend-dot"></span><span class="friend-name">' + escapeHtml(peerDisplay(p)) + '</span>';
    row.addEventListener('click', () => openFriendChat(name));
    list.appendChild(row);
  });
}

async function openFriendChat(host) {
  friendView = host;
  lastFriendPayload = null;
  msgs.innerHTML = '';
  tray.innerHTML = '';
  document.getElementById('input-area').style.display = 'none';
  document.getElementById('friend-title').textContent = ' \u2014 @' + displayOf(host) + ' \u00b7 clone conversation (read-only)';
  document.getElementById('friend-bar').classList.add('visible');
  renderFriendList();
  initConsentSlider();
  try {
    const cm = await (await fetch('/consent-mode?host=' + encodeURIComponent(host))).json();
    setConsentSlider(cm.mode || 'ask');
  } catch (e) { setConsentSlider('ask'); }
  await refreshFriendChat();
}

function setConsentSlider(mode) {
  const s = document.getElementById('consent-slider');
  if (!s) return;
  s.dataset.mode = mode;
  s._mode = mode;
}

function initConsentSlider() {
  const s = document.getElementById('consent-slider');
  if (!s || s._wired) return;
  s._wired = true;
  const apply = clientX => {
    const rect = s.getBoundingClientRect();
    const mode = (clientX - rect.left) < rect.width / 2 ? 'allow' : 'ask';
    if (mode === s._mode || !friendView) return;
    setConsentSlider(mode);
    fetch('/consent-mode', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: friendView, mode }) }).catch(() => {});
  };
  s.addEventListener('pointerdown', e => { s.setPointerCapture(e.pointerId); apply(e.clientX); });
  s.addEventListener('pointermove', e => { if (s.hasPointerCapture && s.hasPointerCapture(e.pointerId)) apply(e.clientX); });
}

async function refreshFriendChat() {
  const host = friendView;
  if (!host) return;
  try {
    const r = await fetch('/comm-log?host=' + encodeURIComponent(host));
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
   session (markdown, tool blocks, spawn clickables), plus file-transfer
   envelope events that never produce agent turns. */
function renderFriendChat(d) {
  msgs.innerHTML = '';
  tray.innerHTML = '';
  registerArchived(d.subagents || []);
  const messages = d.messages || [];
  const events = d.events || [];
  if (!messages.length && !events.length) {
    addDiv('friend-empty', '<i>No clone-to-clone conversation with this host yet.</i>');
    return;
  }
  renderTranscript(messages, d.asks || []);
  events.forEach(row => {
    if (row.kind === 'transfer')
      addDiv('msg friend-event file', '&#x1F4C4 ' + escapeHtml(row.text || 'file transfer'));
    else if (row.kind === 'task')
      addDiv('msg friend-event task', '&#x1F4E5 delegated to ' + escapeHtml(row.dst || '?') + ': ' + escapeHtml((row.text || '').slice(0, 200)));
    else if (row.kind === 'result')
      addDiv('msg friend-event result', '&#x2714 ' + escapeHtml(row.src || '?') + ' replied: ' + escapeHtml((row.text || '').slice(0, 200)));
  });
  placeAskCards(); // re-seat pending asks after the transcript repaint
  msgs.scrollTop = msgs.scrollHeight;
}

setInterval(loadPeers, 5000);
loadPeers();
