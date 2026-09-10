"""Friend view: pane ownership and the merged timeline, against real rooms.

Three bugs shipped while pytest stayed green (2026-09-10) because every one of
them lived in web/*.js and nothing loaded that file. So these run the real
thing: two real rooms with a scripted LLM, the room's own WebUI server, and a
real browser sampling what the page actually paints.

The rooms are module-scoped because stopping one costs seconds (each clone's
poll loop wakes on its own timeout); every test seeds the state it needs and the
page state is per-test, so sharing them stays honest.

Skips (never fails) where playwright or its chromium is missing, so CI without
a browser stays green.
"""

import contextlib
import time

import pytest

from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.room import RoomClient, RoomServer
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CFG = Config(api_key="k", endpoint="e", model="m")
T0 = 1_700_000_000.0


class ScriptedLLM:
    """Scripted replies, then abstention. The tail is what keeps two couriers
    from answering each other forever: an empty queue means <<SILENT>>, which
    the delivery path swallows."""

    def __init__(self, results=(), delay: float = 0.0):
        self.results = list(results)
        self.delay = delay
        self.calls = 0

    def __call__(self, _messages, _tool_defs):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.results:
            return self.results.pop(0)
        return LLMResult(content="<<SILENT>>")


def _wait(predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@contextlib.contextmanager
def _rooms(tmp_path):
    """Host room + a joiner, both real, both scripted. The session turn sleeps,
    so a test can act while it runs."""
    server = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        tmp_path / "d1",
        llm=ScriptedLLM([LLMResult(content="收到 我看看")], delay=1.2),
        rules_path=tmp_path / "r1.json",
    )
    server.start()
    try:
        client = RoomClient(
            "beta",
            CFG,
            NullSink(),
            f"http://127.0.0.1:{server.hub.port}",
            "tok",
            llm=ScriptedLLM(),
            sessions_dir=tmp_path / "cs",
            rules_path=tmp_path / "r2.json",
        )
        client.start()
        try:
            assert _wait(lambda: "beta" in server.hub.roster.peers("alpha")), "peer never joined"
            yield server, client
        finally:
            client.stop()
    finally:
        server.stop()


@pytest.fixture(scope="module")
def rooms(tmp_path_factory):
    with _rooms(tmp_path_factory.mktemp("rooms")) as pair:
        yield pair


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:  # any launch failure means "no browser here"
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@contextlib.contextmanager
def _page(browser, room, path="/"):
    url = f"{room.open_webui(False)}{path}?t={WEBUI_TOKEN}"
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_function("() => typeof openFriendChat === 'function'")
    # The API-key overlay only exists on a machine without config.json; a modal
    # covering the pane would make every sample lie.
    page.evaluate("() => document.getElementById('config-overlay')?.classList.remove('show')")
    try:
        yield page
    finally:
        ctx.close()


@pytest.fixture()
def page(browser, rooms):
    server, _client = rooms
    with _page(browser, server) as pg:
        yield pg


@pytest.fixture()
def mobile_page(browser, rooms):
    """The mobile twin (m.html/m.js) — a parallel implementation of the same
    contract, so it gets the same guarantee."""
    server, _client = rooms
    with _page(browser, server, path="/m") as pg:
        yield pg


# The pane probe: what is in #messages right now, and who owns it. `friend-*`
# side classes belong to the friend thread; bare user/assistant rows belong to
# the session (a live turn node does too, by construction).
PANE_PROBE = """
() => {
  const kids = Array.from(msgs.children);
  const rows = kids.map(n => ({
    cls: n.className,
    ask: (n.dataset && n.dataset.askId) || '',
    text: (n.textContent || '').slice(0, 80),
  }));
  const isFriend = c => /friend-(peer|mine|event|live)/.test(c);
  const isSession = c => /live-node/.test(c) || (!isFriend(c) && /(^| )(user|assistant)( |$)/.test(c));
  return {
    view: friendView,
    owner: pane.owner(),
    rows,
    friendRows: rows.filter(r => isFriend(r.cls)).length,
    sessionRows: rows.filter(r => isSession(r.cls)).length,
    running: processing === true || turn !== null,
    // the bar's class list, verbatim: desktop toggles `visible`, mobile `hidden`
    friendBar: document.getElementById('friend-bar').className,
  };
}
"""


def _probe(page) -> dict:
    return page.evaluate(PANE_PROBE)


def _seed_transcript(room, peer, messages, asks=None) -> None:
    """Write the peer's comm transcript the way a finished turn would."""
    room._comm_store.save("comm-" + peer, f"comm: {peer}", messages, subagents=[], asks=asks or [])


def _seed_session(room, sid, messages, title="seeded session") -> None:
    room.webui_runtime().sessions_save(sid, title, messages)


def _transcript_messages():
    return [
        {"role": "system", "content": "You are the comm agent."},
        {"role": "user", "content": "早 晚上一起吃饭吗", "ts": T0},
        {
            "role": "assistant",
            "content": None,
            "ts": T0 + 1,
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {
                        "name": "inquire",
                        "arguments": '{"questions": [{"question": "去吗"}]}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "USER:\n1. 去", "ts": T0 + 2},
        {"role": "assistant", "content": "好 那就六点老地方见", "ts": T0 + 3},
    ]


def _asks():
    return [
        {
            "id": "ask_a",
            "call_id": "call_a",
            "ts": T0 + 1.5,
            "questions": [
                {
                    "question": "去吗",
                    "options": [{"label": "去"}, {"label": "不去"}],
                    "allow_custom": True,
                }
            ],
            "answers": ["去"],
            "status": "answered",
        }
    ]


def _open_friend(page, host="beta") -> None:
    page.evaluate("async (host) => { await openFriendChat(host); }", host)
    page.wait_for_function("() => friendView !== null")


# ── pane ownership ──


def test_session_reload_never_paints_over_the_friend_view(page, rooms):
    """The 2026-09-10 bug: a finished session turn repainted #messages while a
    friend conversation was open — the thread vanished until a page refresh.
    Reloading the session is that exact code path."""
    server, _client = rooms
    _seed_transcript(server, "beta", _transcript_messages(), _asks())
    _seed_session(
        server,
        "20260101-000000",
        [
            {"role": "user", "content": "session talk"},
            {"role": "assistant", "content": "session reply"},
        ],
    )

    page.evaluate("async () => { await switchSession('20260101-000000'); }")
    session_pane = _probe(page)
    assert session_pane["sessionRows"] == 2 and session_pane["friendRows"] == 0, session_pane

    _open_friend(page)
    painted = _probe(page)
    assert painted["friendRows"] > 0 and painted["sessionRows"] == 0, painted

    page.evaluate("async () => { await reloadSessionFromServer(); }")
    page.wait_for_timeout(300)
    after = _probe(page)
    assert after["rows"] == painted["rows"], f"session reload painted over the friend view: {after}"

    # Positive control: the same call does repaint once it owns the pane.
    page.evaluate("async () => { leaveFriendView(); await reloadSessionFromServer(); }")
    page.wait_for_function("() => friendView === null")
    page.wait_for_timeout(300)
    restored = _probe(page)
    assert restored["sessionRows"] == 2 and restored["friendRows"] == 0, restored


def test_a_finished_turn_does_not_steal_the_friend_pane(page, rooms):
    """The same bug, driven end to end: send a message, walk into the friend
    view mid-turn, and sample the pane until `done` lands."""
    server, _client = rooms
    _seed_transcript(server, "beta", _transcript_messages(), _asks())

    page.evaluate(
        "() => { document.getElementById('input').value = '在吗'; document.getElementById('send').click(); }"
    )
    page.wait_for_timeout(400)
    _open_friend(page)
    opened = _probe(page)
    assert opened["view"] == "beta" and opened["friendRows"] > 0, opened

    worst = 0
    state = opened
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        state = _probe(page)
        worst = max(worst, state["sessionRows"])
        if not state["running"]:
            break
        time.sleep(0.1)

    assert worst == 0, f"a session turn painted into the friend pane: {state}"
    assert state["view"] == "beta" and state["friendRows"] > 0, state
    assert "visible" in state["friendBar"]


# ── no-wipe guard ──


def test_an_empty_comm_log_does_not_wipe_the_pane(page, rooms):
    """A blank/missing transcript used to clear #messages on every poll: the
    thread looked lost until the file came back."""
    server, _client = rooms
    _seed_transcript(server, "beta", _transcript_messages(), _asks())
    _open_friend(page)
    painted = _probe(page)
    assert painted["friendRows"] > 0, painted

    (server._comm_store.dir / "comm-beta.json").unlink()  # the payload goes empty
    page.evaluate("async () => { await refreshFriendChat(); }")
    page.wait_for_timeout(200)
    assert _probe(page)["rows"] == painted["rows"], "an empty /comm-log wiped the friend pane"


def test_a_failed_repaint_recovers_on_the_next_poll(page, rooms):
    """renderFriendChat throwing must not freeze the view: the payload is
    uncached so the next poll retries instead of skipping it as unchanged."""
    server, _client = rooms
    _seed_transcript(server, "beta", _transcript_messages(), _asks())
    _open_friend(page)
    assert _probe(page)["friendRows"] > 0

    # A changed payload (so the poll cannot skip the render) whose last row only
    # appears if the render runs to the end, plus a seam that blows up in the
    # middle of it: the shared transcript renderer the friend view calls.
    _seed_transcript(
        server,
        "beta",
        [*_transcript_messages(), {"role": "assistant", "content": "再来一条", "ts": T0 + 4}],
        _asks(),
    )
    page.evaluate("""() => {
      msgs.innerHTML = '';
      window.__realRender = FC.renderTranscript;
      FC.renderTranscript = () => { throw new Error('boom'); };
    }""")
    page.evaluate("async () => { await refreshFriendChat(); }")
    after_throw = _probe(page)
    assert not any("再来一条" in r["text"] for r in after_throw["rows"]), after_throw
    assert page.evaluate("() => lastFriendPayload === null") is True, (
        "the failed payload stayed cached"
    )

    page.evaluate("() => { FC.renderTranscript = window.__realRender; }")
    page.evaluate("async () => { await refreshFriendChat(); }")
    recovered = _probe(page)
    assert any("再来一条" in r["text"] for r in recovered["rows"]), (
        f"the next poll never repainted: {recovered}"
    )


# ── timeline ──


def test_an_ask_card_sits_with_its_tool_call(page, rooms):
    """The answer card belongs at the call that raised it, not at the end of
    the thread (which is where the stored-order guess put it)."""
    server, _client = rooms
    _seed_transcript(server, "beta", _transcript_messages(), _asks())
    _open_friend(page)
    page.wait_for_function("() => msgs.querySelector('.ask-card') !== null")

    rows = _probe(page)["rows"]
    tool_i = next(i for i, r in enumerate(rows) if " tool" in r["cls"])
    ask_i = next(i for i, r in enumerate(rows) if "ask-card" in r["cls"])
    last_talk = max(i for i, r in enumerate(rows) if "assistant" in r["cls"])
    assert rows[ask_i]["ask"] == "ask_a"
    assert tool_i < ask_i < last_talk, rows


def test_a_legacy_ask_lands_before_the_transcript(page, rooms):
    """Records written before asks carried ts/call_id cannot be placed, so
    they go in front — never appended after messages that predate them."""
    server, _client = rooms
    legacy = [
        {
            "id": "ask_old",
            "questions": [{"question": "旧问题", "options": [], "allow_custom": True}],
            "answers": ["好"],
            "status": "answered",
        }
    ]
    _seed_transcript(server, "beta", _transcript_messages(), legacy + _asks())
    _open_friend(page)
    page.wait_for_function("() => msgs.querySelector('.ask-card') !== null")

    rows = _probe(page)["rows"]
    assert rows[0]["ask"] == "ask_old", rows
    tool_i = next(i for i, r in enumerate(rows) if " tool" in r["cls"])
    assert tool_i > 0


def test_a_stored_abstention_marker_never_renders(page, rooms):
    """A transcript written before the marker was filtered at the store (or a
    model that appends it after real words) must not put `<>` on screen: the
    browser throws the unknown tag away and leaves the angle brackets."""
    server, _client = rooms
    _seed_transcript(
        server,
        "beta",
        [
            *_transcript_messages(),
            {"role": "assistant", "content": "<<SILENT>>", "ts": T0 + 4},
            {"role": "assistant", "content": "好 <<SILENT>>", "ts": T0 + 5},
        ],
        _asks(),
    )
    _open_friend(page)

    rows = _probe(page)["rows"]
    assert not any("SILENT" in r["text"] for r in rows), rows
    assert not any("<>" in r["text"] for r in rows), rows
    # …while the words around the marker survive.
    assert any(r["text"].strip() == "好" for r in rows), rows


def test_a_human_message_is_rendered_once(page, rooms):
    """An incoming human line lives in two places: the mailbox (nickname, hub
    timestamp) and the courier's transcript of the turn it woke (wire name).
    Both used to render — one message, two bubbles signed by two different
    names (2026-09-10 user report)."""
    server, client = rooms
    client.set_display("小乙")  # the sender's nickname, as the roster sees it
    server.hub.mail.deliver_pair("beta:human", "alpha", "", "六点见")
    _seed_transcript(
        server,
        "beta",
        [
            {"role": "system", "content": "comm agent"},
            {"role": "user", "content": "[来自 beta 的用户] 六点见", "ts": T0},
            {"role": "assistant", "content": "我转告一下", "ts": T0 + 1},
        ],
    )
    page.evaluate("async () => { await loadPeers(); }")
    assert page.evaluate("() => displayOf('beta')") == "小乙"

    _open_friend(page)
    rows = _probe(page)["rows"]
    assert sum("六点见" in r["text"] for r in rows) == 1, rows
    assert not any("[来自 " in r["text"] for r in rows), rows  # no raw echo prefix
    assert any("小乙" in r["text"] and "六点见" in r["text"] for r in rows), rows


def test_a_human_echo_without_its_mail_still_renders(page, rooms):
    """Courier-off history: the transcript has the human line and the mailbox
    no longer does. Dropping the echo would silently lose the message, so it
    renders as a labelled bubble instead."""
    server, _client = rooms
    _seed_transcript(
        server,
        "beta",
        [
            {"role": "system", "content": "comm agent"},
            {"role": "user", "content": "[来自 beta 的用户] 只此一份", "ts": T0},
            {"role": "assistant", "content": "好", "ts": T0 + 1},
        ],
    )
    _open_friend(page)
    rows = _probe(page)["rows"]
    assert sum("只此一份" in r["text"] for r in rows) == 1, rows
    assert any(r["text"].startswith("来自 beta 的用户") for r in rows), rows


# ── the mobile twin ──


def test_the_mobile_transcript_uses_the_shared_opts(mobile_page, rooms):
    """Sharing the renderer only holds if the mobile opts are wired: the mail
    echo is deduped here too, and the ask card still sits with its tool call."""
    server, _client = rooms
    server.hub.mail.deliver_pair("beta:human", "alpha", "", "手机端老地方见")
    _seed_transcript(
        server,
        "beta",
        [
            *_transcript_messages(),
            {"role": "user", "content": "[来自 beta 的用户] 手机端老地方见", "ts": T0 + 4},
        ],
        _asks(),
    )
    mobile_page.evaluate("async () => { await openFriendChat('beta'); }")
    mobile_page.wait_for_function("() => pane.owner() === 'friend'")
    mobile_page.wait_for_function("() => msgs.querySelector('.ask-card') !== null")

    rows = _probe(mobile_page)["rows"]
    assert sum("手机端老地方见" in r["text"] for r in rows) == 1, rows
    assert not any("[来自 " in r["text"] for r in rows), rows
    tool_i = next(i for i, r in enumerate(rows) if " tool" in r["cls"])
    ask_i = next(i for i, r in enumerate(rows) if "ask-card" in r["cls"])
    assert tool_i < ask_i, rows


def test_the_mobile_pane_keeps_its_owner(mobile_page, rooms):
    """m.js is a parallel implementation of the same contract, and the friend
    view bug was fixed twice for the same reason. Same guarantee, same board."""
    server, _client = rooms
    _seed_transcript(server, "beta", _transcript_messages(), _asks())
    _seed_session(
        server,
        "20260101-000000",
        [
            {"role": "user", "content": "session talk"},
            {"role": "assistant", "content": "session reply"},
        ],
    )

    mobile_page.evaluate("async () => { await switchSession('20260101-000000'); }")
    assert _probe(mobile_page)["owner"] == "session"

    mobile_page.evaluate("async () => { await openFriendChat('beta'); }")
    mobile_page.wait_for_function("() => pane.owner() === 'friend'")
    painted = _probe(mobile_page)
    assert painted["rows"], painted
    assert "hidden" not in painted["friendBar"], painted

    mobile_page.evaluate("async () => { await reloadSessionFromServer(); }")
    mobile_page.wait_for_timeout(200)
    after = _probe(mobile_page)
    assert after["rows"] == painted["rows"], f"the mobile session reload stole the pane: {after}"

    mobile_page.evaluate("async () => { leaveFriendView(); await reloadSessionFromServer(); }")
    mobile_page.wait_for_function("() => pane.owner() === 'session'")
    mobile_page.wait_for_timeout(200)
    restored = _probe(mobile_page)
    assert restored["rows"], restored
    assert restored["rows"] != painted["rows"], restored  # the session came back, not the thread
