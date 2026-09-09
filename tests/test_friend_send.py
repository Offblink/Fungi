"""Human direct sends from the friend view (composer -> /comm-send).

The contract: the envelope bypasses the local courier entirely and lands on
the peer's comm clone address with explicit human attribution; how it is
received is decided by the PEER's courier switch (on -> courier relays it
faithfully; off -> straight into their transcript / consent cards, no agent).
The transcript write path must merge, never replace, and stay lock-protected.
"""

import itertools
import threading

from fungi import config as config_mod
from fungi import room as room_mod
from fungi.clone.base import Clone
from fungi.protocol import Envelope
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
