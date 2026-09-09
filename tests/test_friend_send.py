"""Human direct sends from the friend view (composer -> /comm-send).

The contract: the envelope bypasses the local courier entirely and lands on
the peer's comm clone address with explicit human attribution; how it is
received is decided by the PEER's courier switch (on -> courier relays it
faithfully; off -> straight into their transcript / consent cards, no agent).
The transcript write path must merge, never replace, and stay lock-protected.
"""

import itertools
import threading
import time
from pathlib import Path

from fungi import config as config_mod
from fungi import room as room_mod
from fungi.clone.base import Clone
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.protocol import Envelope
from fungi.room import RoomClient, RoomServer
from fungi.session import SessionStore


class _FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, env):
        self.sent.append(env)

    def poll(self, after, timeout):  # noqa: ARG002
        return [], after

    def fs(self, op, path, **kw):  # noqa: ARG002
        return {"error": "no hub"}

    def download_transfer(self, transfer_id, dest):  # noqa: ARG002
        dest.write_text("bytes")


class _FakeClone:
    def __init__(self, addr, transport):
        self.addr = addr
        self.transport = transport


class _Rules:
    @staticmethod
    def allows(src):  # noqa: ARG004
        return False

def _room(tmp_path):
    room = object.__new__(room_mod.RoomBase)
    room.host = "alice"
    room.display = "小爱"
    room.cfg = config_mod.Config()
    room.local_addr = "alice:local"
    room._guard = threading.Lock()
    room._comm_locks = {}
    room._comm_store = SessionStore(tmp_path / "comm-sessions")
    room._live_tapes = {}
    room._live_lock = threading.Lock()
    room._direct_transfers = {}
    room._direct_transfers = {}
    room.rules = _Rules()
    return room


def _wire_clone(room, peer="bob"):
    transport = _FakeTransport()
    room._clones = {peer: _FakeClone(f"alice:comm-{peer}", transport)}
    return transport


def test_comm_send_human_chat_envelope_and_own_transcript(tmp_path):
    room = _room(tmp_path)
    transport = _wire_clone(room)
    out = room.comm_send_human("bob", text="  自由留言  ")
    assert out == {"ok": True, "kind": "chat"}
    (env,) = transport.sent
    assert env.type == "chat"
    assert env.src == "alice:comm-bob"
    assert env.dst == "bob:comm-alice"
    assert env.body["from_human"] is True
    assert env.body["sender_name"] == "小爱"
    assert env.body["text"] == "自由留言"
    data = room._comm_store.load("comm-bob")
    (msg,) = data["messages"]
    assert msg["sender"] == "human" and msg["mine"] is True
    assert msg["content"] == "自由留言"


def test_comm_send_human_transfer_stages_and_sends(tmp_path):
    room = _room(tmp_path)
    transport = _wire_clone(room)
    src = tmp_path / "plan.txt"
    src.write_text("hello")
    transport.upload_transfer = lambda path, name, to_host: {  # noqa: ARG005
        "id": "t9", "name": name, "size": src.stat().st_size
    }
    out = room.comm_send_human("bob", file_path=str(src))
    assert out == {"ok": True, "kind": "transfer", "name": "plan.txt"}
    (env,) = transport.sent
    assert env.type == "transfer"
    assert env.dst == "bob:comm-alice"
    assert env.body["id"] == "t9"
    assert env.body["from_human"] is True
    assert env.body["from"] == "alice:local"


def test_comm_send_human_without_clone_fails(tmp_path):
    room = _room(tmp_path)
    room._clones = {}
    assert "error" in room.comm_send_human("ghost", text="hi")
    assert "error" in room.comm_send_human("bob", file_path=str(tmp_path / "nope.txt"))


def test_direct_download_lands_in_repo_root_even_from_foreign_cwd(tmp_path, monkeypatch):
    """Relative Path("inbox") broke when the WebUI HTTP thread ran from another
    cwd; the fallback must be PROJECT_ROOT/inbox/<src_host>/."""
    monkeypatch.chdir(tmp_path)  # cwd is NOT the repo root
    room = _room(tmp_path)
    staged = {}

    def _download(tid, dest):
        staged[tid] = dest
        dest.write_bytes(b"payload")

    room._local = type("L", (), {"transport": type("T", (), {
        "download_transfer": staticmethod(_download),
        "discard_transfer": staticmethod(lambda tid: staged.pop(tid, None)),
    })()})()
    env = Envelope(id="t1", src="bob:comm-alice", dst="alice:local", type="transfer",
                   body={"id": "t1", "name": "报告.txt", "size": 7})
    out = room._direct_download(env, "bob")
    assert out["ok"] is True
    saved = Path(out["saved"])
    try:
        assert saved == config_mod.PROJECT_ROOT / "inbox" / "bob" / saved.name
        assert saved.parent == config_mod.PROJECT_ROOT / "inbox" / "bob"
        assert saved.read_bytes() == b"payload"
        assert "t1" not in staged  # hub staged copy discarded
    finally:
        saved.unlink(missing_ok=True)


def test_courier_off_human_chat_lands_attributed_without_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=False))
    room = _room(tmp_path)
    # Pre-existing clone conversation must survive the direct write.
    room._comm_store.save("comm-bob", "comm: bob", [
        {"role": "assistant", "content": "earlier turn"},
    ])
    chat = Envelope(src="bob:comm-alice", dst="alice:comm-bob", type="chat",
                    body={"text": "我是人类", "from_human": True, "sender_name": "阿宝"})
    assert room._courier_direct(chat) is True
    msgs = room._comm_store.load("comm-bob")["messages"]
    assert len(msgs) == 2  # history merged, not replaced
    assert msgs[0]["content"] == "earlier turn"
    assert msgs[1]["sender"] == "human"
    assert msgs[1]["sender_name"] == "阿宝"
    assert msgs[1]["content"] == "我是人类"


def test_courier_off_plain_chat_keeps_clone_prefix_and_merges(tmp_path, monkeypatch):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=False))
    room = _room(tmp_path)
    room._comm_store.save("comm-bob", "comm: bob", [
        {"role": "user", "content": "[bob:comm-alice] earlier"},
    ])
    chat = Envelope(src="bob:comm-alice", dst="alice:comm-bob", type="chat",
                    body={"text": "hello"})
    assert room._courier_direct(chat) is True
    msgs = room._comm_store.load("comm-bob")["messages"]
    assert len(msgs) == 2
    assert msgs[1]["content"] == "[bob:comm-alice] hello"
    assert "sender" not in msgs[1]


def test_courier_off_human_transfer_card_names_the_human(tmp_path, monkeypatch):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=False))
    room = _room(tmp_path)

    class _Cards:
        @staticmethod
        def record(env):
            room.last_ask_body = env.body
            return True

    room.cards = _Cards()
    env = Envelope(src="bob:comm-alice", dst="alice:comm-bob", type="transfer",
                   body={"id": "t1", "name": "doc.md", "size": 5, "reason": "hi",
                         "from": "bob:comm-alice", "from_human": True,
                         "sender_name": "阿宝"})
    assert room._courier_direct(env) is True
    assert "来自 阿宝 的用户" in room.last_ask_body["question"]


def test_courier_on_render_input_attributes_the_human():
    cfg = config_mod.Config()
    clone = Clone("alice:comm-bob", _FakeTransport(), cfg, sink=lambda *a, **k: None)  # noqa: ARG005
    env = Envelope(src="bob:comm-alice", dst="alice:comm-bob", type="chat",
                   body={"text": "在吗", "from_human": True, "sender_name": "阿宝"})
    assert clone.render_input(env) == "[来自 阿宝 的用户] 在吗"
    plain = Envelope(src="bob:comm-alice", dst="alice:comm-bob", type="chat",
                     body={"text": "在吗"})
    assert clone.render_input(plain) == "[bob:comm-alice] 在吗"


def test_concurrent_transcript_writers_keep_every_message(tmp_path):
    room = _room(tmp_path)
    n = 40
    counter = itertools.count()

    def _writer():
        for _ in range(n // 4):
            room._append_comm_message("bob", {"role": "user",
                                              "content": f"m{next(counter)}"})

    threads = [threading.Thread(target=_writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    msgs = room._comm_store.load("comm-bob")["messages"]
    assert len(msgs) == n
    assert len({m["content"] for m in msgs}) == n  # no lost update


# ── end-to-end: two real rooms, human direct chat ──


class _Scripted:
    def __init__(self, results):
        self.results = list(results)

    def __call__(self, _messages, tool_defs):  # noqa: ARG002
        if self.results:
            return self.results.pop(0)
        return LLMResult(content="(idle)")


def _wait(fn, timeout_s=15.0):
    end = time.time() + timeout_s
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def _two_rooms(tmp_path, monkeypatch, courier):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=courier))
    llm_alpha = _Scripted([LLMResult(content="alpha idle")])
    llm_beta = _Scripted([LLMResult(content="beta here")])
    server = RoomServer(
        "alpha", config_mod.Config(api_key="k", endpoint="e", model="m"), NullSink(), "tok",
        tmp_path / "d1", llm=llm_alpha, rules_path=tmp_path / "r1.json",
    )
    server.start()
    client = RoomClient(
        "beta", config_mod.Config(api_key="k", endpoint="e", model="m"), NullSink(),
        f"http://127.0.0.1:{server.hub.port}", "tok",
        llm=llm_beta, sessions_dir=tmp_path / "cs", rules_path=tmp_path / "r2.json",
    )
    client.start()
    return server, client, llm_beta


def test_human_direct_chat_courier_on_reply_loop(tmp_path, monkeypatch):
    server, client, _llm_beta = _two_rooms(tmp_path, monkeypatch, courier=True)
    try:
        assert _wait(lambda: server._clones.get("beta") is not None)
        assert _wait(lambda: client._clones.get("alpha") is not None)
        assert server.comm_send_human("beta", text="human ping")["ok"]
        # beta's courier relays it: a turn runs and the reply lands back here
        assert _wait(lambda: any(
            m.get("role") == "assistant"
            for m in (server._comm_store.load("comm-beta") or {}).get("messages", [])
        )), "beta's reply never landed on alpha's friend view"
        # courier-on: the human message rides the clone history with the
        # faithful human prefix (render_input), not a sender field
        msgs = client._comm_store.load("comm-alpha")["messages"]
        assert any("[来自 alpha 的用户] human ping" in str(m.get("content")) for m in msgs)
    finally:
        client.stop()
        server.stop()


def test_human_direct_chat_courier_off_no_agent(tmp_path, monkeypatch):
    server, client, llm_beta = _two_rooms(tmp_path, monkeypatch, courier=False)
    try:
        assert _wait(lambda: server._clones.get("beta") is not None)
        assert _wait(lambda: client._clones.get("alpha") is not None)
        assert server.comm_send_human("beta", text="人类直发")["ok"]
        assert _wait(lambda: (client._comm_store.load("comm-alpha") or {}).get("messages"))
        msgs = client._comm_store.load("comm-alpha")["messages"]
        (human,) = [m for m in msgs if m.get("sender") == "human"]
        assert human["content"] == "人类直发"
        assert human["sender_name"] == "alpha"
        assert llm_beta.results, "courier-off receiving must not wake the agent"
    finally:
        client.stop()
        server.stop()
