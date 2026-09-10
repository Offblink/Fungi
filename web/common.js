/* Fungi shared web layer — one implementation for the pieces app.js (desktop)
   and m.js (mobile) used to maintain in parallel. Exposed as window.FungiCommon.
   Contracts preserved here:
   - pendingAskCards registration + placeAskCards re-mount (cards vanish mid-
     stream if either half is missing — see initPendingAsks).
   - buildToolCard fills #tool-<id> blocks by selector; fillToolResult patches
     .tool-result in place so streaming never re-renders the transcript. */
(function () {
  'use strict';

  /* Build marker: bump per web/ change so any WebUI instance can self-identify
     (console + window.__FUNGI_WEB_VER) — stale cache vs new server is otherwise
     indistinguishable from the outside. */
  window.__FUNGI_WEB_VER = 'web-friend-timeline';
  try { console.info('[fungi-web]', window.__FUNGI_WEB_VER); } catch (e) {}
  /* ---------- http ---------- */
  /* One fetch wrapper. Mobile inits a token prefix + 403 hook; desktop inits
     nothing and gets plain relative fetches back. */
  let _prefix = p => p;
  let _onUnauthorized = null;

  function initHttp(opts) {
    opts = opts || {};
    if (opts.prefix) _prefix = opts.prefix;
    if (opts.onUnauthorized) _onUnauthorized = opts.onUnauthorized;
  }
  function url(path) { return _prefix(path); }
  async function fetchJSON(path, opts) {
    const r = await fetch(_prefix(path), opts);
    if (r.status === 403 && _onUnauthorized) { _onUnauthorized(); throw new Error('unauthorized'); }
    return r;
  }
  function postJSON(path, body) {
    return fetchJSON(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }

  /* ---------- helpers ---------- */
  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function fmtDate(d, locale) {
    if (!d) return '';
    const diff = Date.now() - new Date(d).getTime();
    const m = Math.floor(diff / 60000);
    if (m < 1) return 'now';
    if (m < 60) return m + 'm';
    const h = Math.floor(m / 60);
    if (h < 24) return h + 'h';
    const days = Math.floor(h / 24);
    if (days < 7) return days + 'd';
    return new Date(d).toLocaleDateString(locale || 'en-US', { month: 'short', day: 'numeric' });
  }
  function getSessionTitle(list, wrap, keep) {
    const u = (list || []).find(m => m.role === 'user');
    if (!u) return 'Empty';
    const t = String(u.content).replace(/\s+/g, ' ').trim();
    return t.length > wrap ? t.slice(0, keep) + '...' : t;
  }

  /* ---------- confirm modal (基建) ----------
     Same #confirm-overlay markup on both shells; desktop adds Escape/Enter
     keys + mousedown-outside close (Enter never confirms danger), mobile
     closes on backdrop tap. */
  let _confirmState = null;
  let _confirmDefaults = { title: 'Are you sure?', confirmText: 'OK', cancelText: 'Cancel' };

  function showConfirm(opts) {
    const overlay = document.getElementById('confirm-overlay');
    const ok = document.getElementById('confirm-ok');
    document.getElementById('confirm-title').textContent = opts.title || _confirmDefaults.title;
    document.getElementById('confirm-message').textContent = opts.message || '';
    ok.textContent = opts.confirmText || _confirmDefaults.confirmText;
    document.getElementById('confirm-cancel').textContent = opts.cancelText || _confirmDefaults.cancelText;
    ok.classList.toggle('danger', !!opts.danger);
    _confirmState = { onConfirm: opts.onConfirm, danger: !!opts.danger };
    overlay.classList.add('show');
    const cancel = document.getElementById('confirm-cancel');
    if (cancel.focus) cancel.focus();
  }
  function closeConfirm(confirmed) {
    const overlay = document.getElementById('confirm-overlay');
    if (!overlay.classList.contains('show')) return;
    overlay.classList.remove('show');
    const state = _confirmState;
    _confirmState = null;
    if (confirmed && state && state.onConfirm) state.onConfirm();
  }
  function initConfirmModal(opts) {
    opts = opts || {};
    _confirmDefaults = {
      title: opts.title || 'Are you sure?',
      confirmText: opts.confirmText || 'OK',
      cancelText: opts.cancelText || 'Cancel',
    };
    const overlay = document.getElementById('confirm-overlay');
    document.getElementById('confirm-ok').addEventListener('click', () => closeConfirm(true));
    document.getElementById('confirm-cancel').addEventListener('click', () => closeConfirm(false));
    if (opts.keyboard === 'desktop') {
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
    } else {
      overlay.addEventListener('click', e => { if (e.target.id === 'confirm-overlay') closeConfirm(false); });
    }
  }

  /* ---------- tool card rendering ----------
     spec: { id?, name, args?, result? } — id renders the #tool-<id> hook the
     streaming tool_result patches via fillToolResult; result pre-fills the
     output (live turn replay path). opts.argsMax: args snippet cap (80 desktop
     / 60 mobile — trimmed content, both styles come from CSS). */
  function buildToolCard(spec, opts) {
    const argsMax = (opts && opts.argsMax) || 80;
    const d = document.createElement('div');
    d.className = 'msg tool';
    if (spec.id) d.id = 'tool-' + spec.id;
    const args = spec.args || '';
    const argsHtml = args ? ' <code>' + escapeHtml(args.length > argsMax ? args.slice(0, argsMax) + '...' : args) + '</code>' : '';
    d.innerHTML = '<div class="tool-label">&#x1F527; ' + escapeHtml(spec.name || 'tool') + argsHtml + '</div><div class="tool-result">' + (spec.result ? '<pre>' + escapeHtml(spec.result) + '</pre>' : '') + '</div>';
    return d;
  }
  function fillToolResult(block, content) {
    block.querySelector('.tool-result').innerHTML = '<pre>' + escapeHtml(content) + '</pre>';
  }
  /* Spawn/background cards deep-link into the client's agent replay store.
     lookup(callId) -> spec id; the modal itself stays client-side (different
     data shapes on desktop vs mobile). */
  function attachSpawnClick(el, callId, lookup, title) {
    el.classList.add('spawn-block');
    el.title = title || 'Click to view subagent details';
    el.addEventListener('click', () => {
      const id = lookup(callId);
      if (id && typeof window.openAgentModal === 'function') window.openAgentModal(id);
    });
  }

  /* ---------- ask cards (in-turn Inquire) ---------- */
  /* labels: { submit, required, customPlaceholder, answerPlaceholder, noAnswer } */
  function initAsks(opts) {
    const http = opts.http;
    const t = opts.labels;

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
      const opts2 = (q.options || []).map(o =>
        '<button type="button" class="ask-option" data-q="' + qi + '"><b>' + escapeHtml(o.label) + '</b>'
        + (o.description ? '<br><span class="ask-desc">' + escapeHtml(o.description) + '</span>' : '') + '</button>').join('');
      return '<div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div>'
        + '<div class="ask-options">' + opts2 + '</div>'
        + (((q.options || []).length === 0 || q.allow_custom !== false)
          ? '<input class="ask-input" data-q="' + qi + '" placeholder="' + ((q.options || []).length ? t.customPlaceholder : t.answerPlaceholder) + '">'
          : '');
    }

    /* Clicking the selected option clears it: an answer may be typed text alone
       (see collectVals), so every choice has to be undoable — a second click on
       the picked item must not be a no-op. */
    function bindOptionToggles(card) {
      card.querySelectorAll('.ask-option').forEach(btn => {
        btn.addEventListener('click', () => {
          const was = btn.classList.contains('selected');
          card.querySelectorAll('.ask-option[data-q="' + btn.dataset.q + '"]').forEach(b => b.classList.remove('selected'));
          if (!was) btn.classList.add('selected');
        });
      });
    }

    /* "Required" normally lands in the input's placeholder; a question that
       allow_custom:false left option-only has no input to focus, so highlight
       its block for a beat instead. */
    function flagMissing(block) {
      if (!block) return;
      block.classList.add('ask-missing');
      setTimeout(() => block.classList.remove('ask-missing'), 1600);
    }

    /* Read every answer off a card, or null after flagging the first
       unanswered question. Selected option + typed note compose ("Label:
       note"); either alone stands as-is. No silent wiping in either
       direction. */
    function collectVals(card, questions) {
      const vals = [];
      for (let qi = 0; qi < questions.length; qi++) {
        const sel = card.querySelector('.ask-option.selected[data-q="' + qi + '"]');
        const inp = card.querySelector('.ask-input[data-q="' + qi + '"]');
        const label = sel ? sel.querySelector('b').textContent : '';
        const note = inp ? inp.value.trim() : '';
        const v = label && note ? label + ': ' + note : (label || note);
        if (!v) {
          if (inp) { inp.focus(); inp.placeholder = t.required; }
          else flagMissing(card.querySelectorAll('.ask-block')[qi]);
          return null;
        }
        vals.push(v);
      }
      return vals;
    }

    function buildActiveAskCard(a, saved) {
      const card = document.createElement('div');
      card.className = 'msg ask-card live-node'; card.id = 'ask-card';
      card._askId = a.id; card._askQuestions = a.questions;
      card.innerHTML = a.questions.map((q, qi) => '<div class="ask-block">' + askQuestionHtml(q, qi) + '</div>').join('')
        + '<div class="ask-actions"><button id="ask-submit">' + escapeHtml(t.submit) + '</button></div>';
      (saved || []).forEach((s, qi) => {
        if (s.sel) card.querySelectorAll('.ask-option[data-q="' + qi + '"]').forEach(b => {
          if (b.querySelector('b').textContent === s.sel) b.classList.add('selected');
        });
        if (s.val) { const inp = card.querySelector('.ask-input[data-q="' + qi + '"]'); if (inp) inp.value = s.val; }
      });
      bindOptionToggles(card);
      card.querySelectorAll('.ask-input').forEach(inp => {
        inp.addEventListener('keydown', e => { if (e.key === 'Enter') collectAskAnswers(card); });
      });
      card.querySelector('#ask-submit').addEventListener('click', () => collectAskAnswers(card));
      return card;
    }

    function buildAnsweredAskCard(rec) {
      const card = document.createElement('div');
      card.className = 'msg ask-card answered';
      if (rec.id) card.dataset.askId = rec.id;
      const qs = rec.questions || [];
      const ans = Array.isArray(rec.answers) ? rec.answers : (rec.answers != null ? [rec.answers] : null);
      card.innerHTML = qs.map((q, qi) => {
        const labels = (q.options || []).map(o => o.label);
        const a = ans ? (ans[qi] ?? '') : '';
        const isCustom = a && !labels.includes(a);
        const opts2 = (q.options || []).map(o =>
          '<button type="button" class="ask-option' + (!isCustom && o.label === a ? ' selected' : '') + '" style="cursor:default"><b>' + escapeHtml(o.label) + '</b>'
          + (o.description ? '<br><span class="ask-desc">' + escapeHtml(o.description) + '</span>' : '') + '</button>').join('');
        let row = '<div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div>'
          + '<div class="ask-options">' + opts2 + '</div>';
        if (rec.status && rec.status !== 'answered') row += '<div class="ask-a">' + t.noAnswer + '</div>';
        else if (isCustom || !labels.length) row += '<div class="ask-a">' + (a === 'no' ? '\u274C ' : '\u2705 ') + escapeHtml(a) + '</div>';
        return '<div class="ask-block">' + row + '</div>';
      }).join('');
      return card;
    }

    function collectAskAnswers(card) {
      const qs = card._askQuestions || [];
      const vals = collectVals(card, qs);
      if (!vals) return;
      const turn = opts.getTurn ? opts.getTurn() : null;
      const rec = (turn && turn.entries || []).find(x => x.kind === 'ask' && x.id === card._askId);
      if (rec) { rec.answers = vals; rec.active = false; }
      const answered = buildAnsweredAskCard({ questions: qs, answers: vals, status: 'answered' });
      answered.classList.add('live-node');
      card.replaceWith(answered);
      http.postJSON('/answer', { id: card._askId, value: vals }).catch(() => {});
    }

    return { saveAskCardState, askQuestionHtml, buildActiveAskCard, buildAnsweredAskCard, bindOptionToggles, collectVals };
  }

  /* ---------- pending card asks (consent / cross-host asks, out-of-band) ----------
     Contract: every new ask is registered in pendingAskCards AND re-mounted by
     placeAskCards after every full repaint. Missing either makes cards vanish
     mid-stream. ctx: {
       http,                 // {fetchJSON, postJSON}
       banner,               // () -> #asks-banner element
       inlineHost,           // () -> host name while a friend view is open, else null
       msgs,                 // () -> #messages element
       isNearBottom,         // fn(el) -> bool (client-specific threshold)
       displayOf,            // fn(host) -> display name
       motion,               // { cardIn?(el), resolved?(card, ok, settle) } — both optional
       animatePlace,         // animate remounts during place() (desktop yes, mobile no)
       labels: { fromFallback, consentPlaceholder, consentHint?, allow, deny, consentSend, submit, required, customPlaceholder, answerPlaceholder }
     } */
  function initPendingAsks(ctx) {
    const asks = initAsks({ http: ctx.http, labels: { submit: ctx.labels.submit, required: ctx.labels.required, customPlaceholder: ctx.labels.customPlaceholder, answerPlaceholder: ctx.labels.answerPlaceholder, noAnswer: '' } });
    const pendingAskIds = new Set();
    const pendingAskCards = new Map(); // ask id -> {rec, el} while the card is live
    const resolvedAskCards = new Map(); // answered card asks: verdict stays visible

    function mount(el, conv, animate) {
      const inline = ctx.inlineHost() && conv === ctx.inlineHost();
      if (inline) {
        const stick = ctx.isNearBottom(ctx.msgs()); // measure before the card changes layout
        ctx.msgs().appendChild(el);
        if (stick) ctx.msgs().scrollTop = ctx.msgs().scrollHeight;
      } else if (el.parentElement !== ctx.banner()) {
        ctx.banner().appendChild(el);
      }
      if (animate && ctx.motion && ctx.motion.cardIn) ctx.motion.cardIn(el);
    }

    function place() {
      // A pending ask belongs to the conversation that raised it: inline in the
      // matching open friend view, otherwise the global banner. Answered cards
      // follow the same rule until the durable transcript record (saved by the
      // server) renders — that copy then replaces the floating one.
      pendingAskCards.forEach(({ rec, el }) => mount(el, rec.conv, ctx.animatePlace));
      resolvedAskCards.forEach(({ rec, el }) => {
        if (ctx.inlineHost() && rec.conv === ctx.inlineHost()) {
          if (ctx.msgs().querySelector('.ask-card[data-ask-id="' + rec.id + '"]')) {
            el.remove();
            resolvedAskCards.delete(rec.id);
          } else mount(el, rec.conv, false);
        } else if (el.parentElement !== ctx.banner()) {
          ctx.banner().appendChild(el);
        }
      });
    }

    function buildPendingAskCard(a) {
      const card = document.createElement('div');
      card.className = 'msg ask-card pending-ask';
      const fromRaw = String(a.from || a.src || ctx.labels.fromFallback);
      const from = escapeHtml(ctx.displayOf(fromRaw.split(':')[0]));
      if (a.kind === 'consent') {
        const q = a.questions[0] || { question: '(consent request)' };
        card.innerHTML = '<div class="ask-from">\u{1F344} ' + from + ' \u00b7 consent</div>'
          + '<div class="ask-block"><div class="ask-q">\u2753 ' + escapeHtml(q.question) + '</div></div>'
          + '<div class="ask-consent-actions"><input placeholder="' + escapeHtml(ctx.labels.consentPlaceholder) + '">'
          + '<button class="ask-allow">' + escapeHtml(ctx.labels.allow) + '</button>'
          + '<button class="ask-deny">' + escapeHtml(ctx.labels.deny) + '</button><button class="ask-send">' + escapeHtml(ctx.labels.consentSend) + '</button></div>'
          + (ctx.labels.consentHint ? '<div class="ask-hint">' + escapeHtml(ctx.labels.consentHint) + '</div>' : '');
        const inp = card.querySelector('input');
        const send = v => answerPendingAsk(a, card, v);
        card.querySelector('.ask-allow').addEventListener('click', () => send(inp.value.trim() ? 'yes: ' + inp.value.trim() : 'yes'));
        card.querySelector('.ask-deny').addEventListener('click', () => send(inp.value.trim() ? 'no: ' + inp.value.trim() : 'no'));
        const custom = () => { if (inp.value.trim()) send(inp.value.trim()); else inp.focus(); };
        card.querySelector('.ask-consent-actions input').addEventListener('keydown', e => { if (e.key === 'Enter') custom(); });
        card.querySelector('.ask-send').addEventListener('click', custom);
      } else {
        card.innerHTML = '<div class="ask-from">\u{1F344} ' + from + '</div>'
          + a.questions.map((q, qi) => '<div class="ask-block">' + asks.askQuestionHtml(q, qi) + '</div>').join('')
          + '<div class="ask-actions"><button class="ask-send">' + escapeHtml(ctx.labels.submit) + '</button></div>';
        asks.bindOptionToggles(card);
        card.querySelector('.ask-send').addEventListener('click', () => {
          const vals = asks.collectVals(card, a.questions || []);
          if (!vals) return;
          answerPendingAsk(a, card, vals.length === 1 ? vals[0] : vals);
        });
      }
      return card;
    }

    function answerPendingAsk(a, card, value) {
      ctx.http.postJSON('/answer', { id: a.id, value }).catch(() => {});
      pendingAskIds.delete(a.id);
      pendingAskCards.delete(a.id);
      // The verdict stays as a real answered card (same builder as the in-turn
      // replay path) instead of evaporating after 8 seconds; the server also
      // files it into the friend transcript so it survives reload.
      const done = asks.buildAnsweredAskCard({
        id: a.id,
        questions: a.questions,
        answers: Array.isArray(value) ? value : [value],
        status: 'answered',
      });
      const settle = () => {
        card.replaceWith(done);
        resolvedAskCards.set(a.id, { rec: a, el: done });
      };
      if (ctx.motion && ctx.motion.resolved) ctx.motion.resolved(card, value !== 'no', settle);
      else settle();
    }

    async function poll() {
      try {
        const d = await (await ctx.http.fetchJSON('/asks')).json();
        (d.asks || []).forEach(a => {
          if (pendingAskIds.has(a.id)) return;
          pendingAskIds.add(a.id);
          const el = buildPendingAskCard(a);
          pendingAskCards.set(a.id, { rec: a, el });
          mount(el, a.conv, true);
        });
      } catch (e) {}
    }

    return { poll, place, pendingAskIds, pendingAskCards, resolvedAskCards };
  }

  /* ---------- mail unread (留言未读计数, per-peer) ----------
     Polls this host's mailbox and exposes per-peer unread counts for the
     friend list. Reading happens in the friend view: markPeerRead clears
     everything a peer sent.
     opts: { http, onChange(map) }
     Backend: GET /mail -> {host, mails:[{id, from, peer, body, ts, read, mine}], unread};
     POST /mail/read {id} -> {ok}. */
  function initMailUnread(opts) {
    let mails = [];
    let timer = null;

    function byPeer() {
      const map = {};
      for (const m of mails) {
        if (!m.mine && !m.read) {
          const k = m.peer || String(m.from || "").split(":")[0];
          map[k] = (map[k] || 0) + 1;
        }
      }
      return map;
    }

    function changed() { if (opts.onChange) opts.onChange(byPeer()); }

    async function poll() {
      try {
        const d = await (await opts.http.fetchJSON("/mail")).json();
        mails = d.mails || [];
        changed();
      } catch (e) {} // backend not up yet / transient: retry on the next tick
    }

    async function markPeerRead(peer) {
      const targets = mails.filter(m => !m.mine && !m.read
        && (m.peer || String(m.from || "").split(":")[0]) === peer);
      if (!targets.length) return;
      await Promise.all(targets.map(async m => {
        try { await opts.http.postJSON("/mail/read", { id: m.id }); m.read = true; }
        catch (e) {}
      }));
      changed();
    }

    function start() { poll(); timer = setInterval(poll, 2000); } // badge latency: 2s is plenty on LAN
    function stop() { if (timer) { clearInterval(timer); timer = null; } }
    return { start, stop, poll, byPeer, markPeerRead };
  }

  /* ---------- #messages ownership ----------
     One container, one owner at a time. Every render path used to write into
     the same #messages/#tray and trust separate entry guards to keep the other
     view out; a guard someone forgot painted the session transcript over an
     open friend conversation, and the friend poll would not repaint it (its
     payload had not changed) — the thread only came back on a page refresh
     (2026-09-10 real-machine finding).

     Now the writes go through a view-bound writer: `pane.of('friend').add(...)`
     writes only while the friend view owns the pane, and is a no-op otherwise
     (the node is built but never attached). Ownership is one flag instead of a
     convention spread across the render sites, and `pane.owner()` can be
     asserted directly.

     ctx: { msgs() -> el, tray() -> el, isNearBottom(el) -> bool, onPaint?() } */
  function initPane(ctx) {
    let owner = 'session';
    const refused = []; // view:paint pairs dropped for not owning the pane
    const painted = () => { if (ctx.onPaint) ctx.onPaint(); };
    // The mobile page has no persistent tray (its bubbles live in a bottom
    // sheet it manages itself), so the tray side is optional.
    const clearTray = () => {
      const t = ctx.tray && ctx.tray();
      if (t) t.innerHTML = '';
    };
    function of(view) {
      const active = () => owner === view;
      const attach = (node, ref) => {
        if (!active()) { refused.push(view + ':' + (node.nodeName || '?') + '.' + (node.className || '')); return node; }
        const el = ctx.msgs();
        if (ref) el.insertBefore(node, ref); else el.appendChild(node);
        painted();
        return node;
      };
      return {
        view,
        active,
        /* The live container. Reads (children, querySelectorAll) are safe from
           any view; a write through it is not — use the methods below. */
        el: () => ctx.msgs(),
        clear() {
          if (!active()) { refused.push(view + ':clear'); return false; }
          ctx.msgs().innerHTML = '';
          clearTray();
          painted();
          return true;
        },
        /* Messages only: a transcript repaint must not wipe the live subagent
           bubbles sitting in the tray. */
        clearMsgs() {
          if (!active()) { refused.push(view + ':clear-msgs'); return false; }
          ctx.msgs().innerHTML = '';
          painted();
          return true;
        },
        /* Build a .msg node and append it, keeping the reader pinned to the
           bottom the way a chat should. A refused write still hands the node
           back (detached), so callers can decorate what they get. pinOnAdd
           false = the caller pins once at the end of its own render. */
        add(cls, html, id) {
          const d = document.createElement('div');
          d.className = 'msg ' + cls;
          if (id) d.id = id;
          if (html) d.innerHTML = html;
          const pin = ctx.pinOnAdd !== false && active() && ctx.isNearBottom(ctx.msgs());
          attach(d);
          if (pin) { const el = ctx.msgs(); el.scrollTop = el.scrollHeight; }
          return d;
        },
        append: node => attach(node),
        before: (node, ref) => attach(node, ref),
        prepend(node) {
          if (!active()) { refused.push(view + ':prepend'); return node; }
          ctx.msgs().prepend(node);
          painted();
          return node;
        },
        /* Keep the bottom pinned after a repaint that replaced the pane. */
        stick() {
          if (!active()) return false;
          const el = ctx.msgs();
          el.scrollTop = el.scrollHeight;
          return true;
        },
        inTray(node) {
          const t = ctx.tray && ctx.tray();
          if (!active() || !t) { refused.push(view + ':tray'); return node; }
          t.appendChild(node);
          return node;
        },
      };
    }
    return {
      owner: () => owner,
      is: view => owner === view,
      /* Hand the pane to a view and wipe it: whoever takes over starts empty. */
      take(view) {
        owner = view;
        ctx.msgs().innerHTML = '';
        clearTray();
        painted();
      },
      of,
      refused,
    };
  }

  /* A human message shows up twice: the mailbox (authoritative — both sides, the
     sender's nickname, a hub timestamp) and the courier's transcript of the turn
     it woke (`[来自 <host> 的用户] text`, built by clone/base.py render_input).
     The transcript copy carries the WIRE name, so with a nickname set the same
     line looked like two messages signed by two different people (2026-09-10
     user report). The mailbox copy is the one to keep; this recognises the echo. */
  function humanEcho(content) {
    const m = /^\[来自 (.+?) 的用户\]\s?([\s\S]*)$/.exec(String(content || ''));
    return m ? { who: m[1], text: m[2].trim() } : null;
  }

  /* The courier's abstention marker is a delivery control token, not text to
     read: `<<SILENT>>` ends a comm turn silently (clone/comm.py). When one is
     left in a transcript — an older file, or the model adding it after real
     words — the browser throws the unknown tag away and the reader sees `<>`
     (2026-09-10 user report). Strip it wherever a body is rendered. */
  const SILENT_MARKER = '<<SILENT>>';
  function stripSilent(text) {
    const s = String(text == null ? '' : text);
    return s.includes(SILENT_MARKER) ? s.split(SILENT_MARKER).join('').trim() : s;
  }

  /* ---------- transcript rendering (both clients) ----------
     Session-style rendering: markdown text, reasoning details, tool blocks and
     answered ask cards — plus the friend thread's timeline, where rows carrying
     a `ts` merge into the transcript by time and ask cards sit at the tool call
     that raised them. The two clients differ only in cosmetics, so those come
     in through opts:

       asks            the client's FC.initAsks() bundle (answered cards)
       spawnLookup     callId -> subagent spec (the client's replay store)
       friend          true for the friend thread (mail echo dedupe on)
       side            {user, agent} extra classes for the friend thread
       mailBodies      Set of mailbox bodies already on screen (echo dedupe)
       argsMax         tool-card argument preview length
       spawnTitle      tooltip wording for spawn cards (mobile says 点按…)
       reasoningHtml   (text) -> inner html of the reasoning <details>
       liveText        (run) -> html for a streaming text run
  */
  function markTs(el, ts) {
    if (el && typeof ts === 'number') el.dataset.ts = String(ts);
    return el;
  }
  function insertByTs(p, el, ts) {
    if (!el || !p.active()) return el;
    if (typeof ts !== 'number') return el; // no stamp: keep it where it landed
    for (const kid of [...p.el().children]) {
      const kts = kid.dataset && kid.dataset.ts ? parseFloat(kid.dataset.ts) : null;
      if (kts !== null && kts > ts) { p.before(el, kid); return el; }
    }
    return el;
  }
  /* The question text a tool call itself carries — the only way to place an ask
     record from before records carried `call_id`. Exact match, no fuzzy. */
  function askTextOfCall(tc) {
    try {
      const args = JSON.parse(tc.function && tc.function.arguments || '{}');
      const first = Array.isArray(args.questions) && args.questions.length ? args.questions[0] : args;
      return String((first && first.question) || args.question || '').trim();
    } catch (e) { return ''; }
  }

  function renderTranscript(p, messages, asks, opts) {
    const side = opts.side || {};
    const userSide = side.user || '';
    const agentSide = side.agent || '';
    const mailBodies = opts.mailBodies;
    let toolBlocks = {};
    const askByCall = new Map();  // ask record -> the tool call that raised it
    const askQueue = [];          // records without a call id, in stored order
    (asks || []).forEach(rec => {
      if (rec && rec.call_id) askByCall.set(rec.call_id, rec);
      else if (rec) askQueue.push(rec);
    });
    for (const m of messages || []) {
      if (m.role === 'user') {
        const c = String(m.content || '');
        const echo = opts.friend ? humanEcho(c) : null; // only the friend thread merges mail
        if (echo) {
          if (mailBodies && mailBodies.has(echo.text)) continue; // the mailbox copy is already on screen
          const bubble = markTs(p.add('user' + userSide, marked.parse(echo.text)), m.ts);
          const lab = document.createElement('div');
          lab.className = 'human-label';
          lab.textContent = '来自 ' + echo.who + ' 的用户';
          bubble.prepend(lab);
        }
        else if (c.startsWith('[background report]')) markTs(p.add('sys-note', escapeHtml(c)), m.ts);
        else if (m.sender === 'human' && !m.mine) {
          const bubble = markTs(p.add('user' + userSide, marked.parse(c)), m.ts);
          const lab = document.createElement('div');
          lab.className = 'human-label';
          lab.textContent = '来自 ' + (m.sender_name || '?') + ' 的用户';
          bubble.prepend(lab);
        }
        else markTs(p.add('user' + userSide, marked.parse(c)), m.ts);
      }
      else if (m.role === 'assistant') {
        if (m.reasoning) {
          const det = document.createElement('details');
          det.className = 'msg reasoning';
          det.innerHTML = '<summary>Thinking\u2026</summary>' + opts.reasoningHtml(m.reasoning);
          p.append(markTs(det, m.ts));
        }
        const text = stripSilent(m.content);
        if (text) {
          if (text.startsWith('(LLM error:') || text.startsWith('(Hit max tool rounds'))
            markTs(p.add('error', '&#x26A0; ' + escapeHtml(text)), m.ts);
          else markTs(p.add('assistant' + agentSide, marked.parse(text)), m.ts);
        }
        if (m.tool_calls) m.tool_calls.forEach(tc => {
          const d = buildToolCard({ id: tc.id, name: tc.function?.name, args: tc.function?.arguments || '' }, { argsMax: opts.argsMax || 80 });
          p.append(markTs(d, m.ts));
          if (tc.function?.name === 'spawn' || tc.function?.name === 'background') attachSpawnClick(d, tc.id, opts.spawnLookup, opts.spawnTitle);
          if (tc.function?.name === 'inquire' || tc.function?.name === 'confirm' || tc.function?.name === 'ask_user') { // ask_user: pre-rename transcripts
            // Anchored by the tool call that raised it; a record without a call
            // id falls back to stored order, then to the call's own question text
            // (records written before `call_id` existed).
            let rec = askByCall.get(tc.id);
            if (rec) askByCall.delete(tc.id);
            else {
              const qtext = askTextOfCall(tc);
              const idx = qtext ? askQueue.findIndex(r => String(((r.questions || [])[0] || {}).question || '').trim() === qtext) : -1;
              rec = idx >= 0 ? askQueue.splice(idx, 1)[0] : (askQueue.length ? askQueue.shift() : null);
            }
            if (rec) p.append(markTs(opts.asks.buildAnsweredAskCard(rec), rec.ts));
          }
          toolBlocks[tc.id] = d;
        });
      } else if (m.role === 'tool') {
        const block = toolBlocks[m.tool_call_id];
        if (block) fillToolResult(block, m.content || '');
        else markTs(p.add('tool', '<pre>' + escapeHtml(m.content || '') + '</pre>'), m.ts);
      }
    }
    // Leftovers: their tool call is gone from the transcript, so they belong to
    // a turn older than anything on screen (legacy records carry no ts) — a
    // timestamped one (card asks) slots into the timeline like any other row.
    const leftover = [...askQueue, ...askByCall.values()];
    leftover.forEach(rec => {
      const node = markTs(opts.asks.buildAnsweredAskCard(rec), rec.ts);
      if (typeof rec.ts === 'number') insertByTs(p, node, rec.ts);
      else p.prepend(node);
    });
  }

  /* In-flight turn tape (a comm clone's live events, friend view): merge
     adjacent text/reasoning deltas into runs so streaming reads as paragraphs
     instead of one row per fragment. */
  function renderLiveEvents(live, p, opts) {
    const runs = [];
    for (const ev of live || []) {
      const k = ev && ev.kind;
      if (k === 'reasoning_start' || k === 'reasoning_end') continue;
      const last = runs[runs.length - 1];
      if ((k === 'text' || k === 'reasoning') && last && last.kind === k) {
        last.text += liveEvText(ev);
        continue;
      }
      runs.push({ kind: k, text: liveEvText(ev), ev });
    }
    for (const r of runs) {
      if (r.kind === 'text') {
        const html = opts.liveText(r);
        if (html) p.add('friend-live' + (opts.side ? opts.side.agent : ''), html);
      } else if (r.kind === 'reasoning') {
        const det = document.createElement('details');
        det.className = 'msg reasoning';
        det.innerHTML = '<summary>Thinking\u2026</summary>' + opts.reasoningHtml(r.text);
        p.append(det);
      } else if (r.kind === 'tool') {
        const c = (r.ev && r.ev.content) || {};
        p.add('tool', '<div class="tool-label">&#x1F527; ' + escapeHtml(c.name || 'tool')
          + (c.args ? ' <code style="font-size:0.82rem;opacity:0.7">' + escapeHtml(String(c.args).slice(0, 80)) + '</code>' : '') + '</div>');
      } else if (r.kind === 'tool_result') {
        const t = String(liveEvText(r.ev) || '');
        p.add('friend-live', '<pre>' + escapeHtml(t.slice(0, 400)) + (t.length > 400 ? '...' : '') + '</pre>');
      } else if (r.kind === 'status') {
        p.add('friend-event', '⏳ ' + escapeHtml(r.text || 'running…'));
      } else if (r.kind === 'error') {
        p.add('friend-event', '⚠ ' + escapeHtml(r.text || 'error'));
      }
    }
  }
  function liveEvText(ev) {
    const c = ev && ev.content;
    if (typeof c === 'string') return c;
    if (c && typeof c === 'object') return c.text || c.content || c.name || '';
    return '';
  }

  window.FungiCommon = {
    initHttp, url, fetchJSON, postJSON,
    escapeHtml, fmtDate, getSessionTitle,
    initConfirmModal, showConfirm, closeConfirm,
    buildToolCard, fillToolResult, attachSpawnClick,
    initAsks, initPendingAsks, initMailUnread,
    initPane, stripSilent, humanEcho,
    markTs, insertByTs, askTextOfCall, renderTranscript, renderLiveEvents,
  };
})();
