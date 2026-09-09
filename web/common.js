/* Fungi shared web layer — one implementation for the pieces app.js (desktop)
   and m.js (mobile) used to maintain in parallel. Exposed as window.FungiCommon.
   Contracts preserved here:
   - pendingAskCards registration + placeAskCards re-mount (cards vanish mid-
     stream if either half is missing — see initPendingAsks).
   - buildToolCard fills #tool-<id> blocks by selector; fillToolResult patches
     .tool-result in place so streaming never re-renders the transcript. */
(function () {
  'use strict';

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
      const vals = [];
      for (let qi = 0; qi < qs.length; qi++) {
        const sel = card.querySelector('.ask-option.selected[data-q="' + qi + '"]');
        const inp = card.querySelector('.ask-input[data-q="' + qi + '"]');
        const label = sel ? sel.querySelector('b').textContent : '';
        const note = inp ? inp.value.trim() : '';
        // Selected option + typed note compose ("Label: note"); either alone
        // stands as-is. No silent wiping in either direction.
        const v = label && note ? label + ': ' + note : (label || note);
        if (!v) { if (inp) { inp.focus(); inp.placeholder = t.required; } return; }
        vals.push(v);
      }
      const turn = opts.getTurn ? opts.getTurn() : null;
      const rec = (turn && turn.entries || []).find(x => x.kind === 'ask' && x.id === card._askId);
      if (rec) { rec.answers = vals; rec.active = false; }
      const answered = buildAnsweredAskCard({ questions: qs, answers: vals, status: 'answered' });
      answered.classList.add('live-node');
      card.replaceWith(answered);
      http.postJSON('/answer', { id: card._askId, value: vals }).catch(() => {});
    }

    return { saveAskCardState, askQuestionHtml, buildActiveAskCard, buildAnsweredAskCard };
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
            if (!v) { if (inp) { inp.focus(); inp.placeholder = ctx.labels.required; } return; }
            vals.push(v);
          }
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

  /* ---------- mail (amail 文字邮件) ----------
     Unified text-message inbox: centered mail cards from different people,
     one per mail; clicking a card expands its detail inline (accordion).
     opts: {
       http,            // {fetchJSON, postJSON}
       badgeEl,         // unread badge element on the nav entry (hidden when 0)
       displayOf,       // fn(host) -> display name for the sender line
       locale,          // fmtDate locale ('en-US' desktop / 'zh-CN' mobile)
       strings: { title, markRead, empty, close }
     }
     Backend contract: GET /mail -> {host, mails:[{id, from, peer, subject, body, ts, read, mine}], unread};
     POST /mail/read {id} -> {ok:true}. Polls every 5s. */
  function initMail(opts) {
    const http = opts.http;
    const t = Object.assign({ title: 'Mail', markRead: 'Mark read', empty: 'No mail yet.', close: '\u2715' }, opts.strings);
    let mails = [];
    let unread = 0;
    let openMailId = null; // expanded card while set
    let viewing = false;
    let timer = null;

    const overlay = document.createElement('div');
    overlay.id = 'mail-modal-overlay';
    overlay.innerHTML = '<div id="mail-modal">'
      + '<div class="mail-head"><h3 id="mail-modal-title">' + escapeHtml(t.title) + '</h3>'
      + '<button id="mail-close">' + escapeHtml(t.close) + '</button></div>'
      + '<div id="mail-list" class="mail-list"></div>'
      + '</div></div>';
    document.body.appendChild(overlay);

    function renderBadge() {
      if (opts.badgeEl) {
        opts.badgeEl.hidden = unread === 0;
        opts.badgeEl.textContent = unread;
      }
    }

    function senderLine(m) {
      const host = String(m.from || '');
      if (m.mine) return '我 → ' + (opts.displayOf ? opts.displayOf(m.peer) : m.peer);
      if (host.endsWith(':human')) return (opts.displayOf ? opts.displayOf(m.peer) : m.peer) + ' 的用户';
      return (opts.displayOf ? opts.displayOf(m.peer) : m.peer) + ' 的 Agent';
    }

    function render() {
      const listEl = overlay.querySelector('#mail-list');
      listEl.innerHTML = '';
      if (!mails.length) {
        listEl.innerHTML = '<div class="mail-empty">' + escapeHtml(t.empty) + '</div>';
        return;
      }
      mails.forEach(m => {
        const card = document.createElement('div');
        card.className = 'mail-card' + (m.read ? '' : ' unread') + (m.id === openMailId ? ' open' : '');
        const subject = m.subject && m.subject !== '(no subject)' && m.subject !== m.body
          ? escapeHtml(m.subject) : '';
        card.innerHTML = '<span class="mail-unread-dot"></span>'
          + '<div class="mail-card-main"><div class="mail-card-top">'
          + '<span class="mail-from">' + escapeHtml(senderLine(m)) + '</span>'
          + '<span class="mail-time">' + escapeHtml(fmtDate(m.ts, opts.locale)) + '</span></div>'
          + (subject ? '<div class="mail-subject">' + subject + '</div>' : '')
          + '<div class="mail-preview">' + escapeHtml(String(m.body || '').slice(0, 80)) + '</div>'
          + '<div class="mail-card-body" hidden></div>'
          + '<div class="mail-card-actions" hidden><button class="mail-mark-read">' + escapeHtml(t.markRead) + '</button></div>'
          + '</div>';
        const bodyEl = card.querySelector('.mail-card-body');
        const actionsEl = card.querySelector('.mail-card-actions');
        bodyEl.textContent = m.body || '';
        card.addEventListener('click', () => {
          openMailId = openMailId === m.id ? null : m.id;
          const open = openMailId === m.id;
          bodyEl.hidden = !open;
          actionsEl.hidden = !open;
          card.classList.toggle('open', open);
        });
        card.querySelector('.mail-mark-read').addEventListener('click', e => {
          e.stopPropagation();
          markRead(m.id);
        });
        listEl.appendChild(card);
      });
    }

    async function markRead(id) {
      try {
        await http.postJSON('/mail/read', { id });
        const m = mails.find(x => x.id === id);
        if (m && !m.read) { m.read = true; unread = Math.max(0, unread - 1); }
        renderBadge();
        render();
      } catch (e) {}
    }

    async function poll() {
      try {
        const d = await (await http.fetchJSON('/mail')).json();
        mails = d.mails || [];
        unread = d.unread != null ? d.unread : mails.filter(m => !m.read).length;
        renderBadge();
        if (viewing) render(); // keep the open modal fresh
      } catch (e) {} // backend not up yet / transient: retry on the next tick
    }

    function open() {
      viewing = true;
      openMailId = null;
      overlay.classList.add('show');
      render();
      poll();
    }
    function close() {
      viewing = false;
      openMailId = null;
      overlay.classList.remove('show');
    }

    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    overlay.querySelector('#mail-close').addEventListener('click', close);

    function start() {
      poll();
      timer = setInterval(poll, 5000);
    }
    function stop() { if (timer) { clearInterval(timer); timer = null; } }

    return { start, stop, open, close, poll };
  }

  window.FungiCommon = {
    initHttp, url, fetchJSON, postJSON,
    escapeHtml, fmtDate, getSessionTitle,
    initConfirmModal, showConfirm, closeConfirm,
    buildToolCard, fillToolResult, attachSpawnClick,
    initAsks, initPendingAsks, initMail,
  };
})();
