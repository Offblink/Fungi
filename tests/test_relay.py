"""Relay: single delivery function — local direct, host buffer, dedup, bounce."""

import threading

from fungi.hub.relay import BOUNCED, DUPLICATE, QUEUED, Relay
from fungi.protocol import Envelope


def _env(src: str, dst: str, text: str, mid: str = "") -> Envelope:
    return Envelope(src=src, dst=dst, type="chat", body={"text": text}, id=mid)


def test_local_direct_delivery():
    relay = Relay("srv")
    inbox = relay.register_local("srv:local")
    assert relay.deliver(_env("srv:comm-beta", "srv:local", "hi")) == QUEUED
    messages, cursor = inbox.after(0, 0)
    assert len(messages) == 1
    assert messages[0].body == {"text": "hi"}
    assert cursor > 0


def test_host_buffer_and_dedup():
    relay = Relay("srv")
    inbox = relay.host_buffer("beta")
    env = _env("srv:local", "beta:comm-srv", "hi", mid="m1")
    assert relay.deliver(env) == QUEUED
    assert relay.deliver(env) == DUPLICATE  # same id → dropped
    messages, cursor = inbox.after(0, 0)
    assert len(messages) == 1
    # drained: re-poll at cursor gets nothing
    messages, _cursor = inbox.after(cursor, 0)
    assert messages == []


def test_longpoll_wakes_on_push():
    relay = Relay("srv")
    inbox = relay.host_buffer("beta")

    def push_later():
        threading.Event().wait(0.05)
        relay.deliver(_env("srv:local", "beta:comm-srv", "wake"))

    threading.Thread(target=push_later).start()
    messages, _cursor = inbox.after(0, 5.0)
    assert len(messages) == 1


def test_unreachable_bounces_err_to_src():
    relay = Relay("srv")
    src_inbox = relay.register_local("srv:local")
    status = relay.deliver(_env("srv:local", "ghost:local", "hi"))
    assert status == BOUNCED  # original not delivered...
    messages, _cursor = src_inbox.after(0, 0)
    assert len(messages) == 1
    err = messages[0]
    assert err.type == "err"
    assert err.src == "ghost:local"
    assert err.dst == "srv:local"
    assert "unreachable" in err.body["error"]


def test_err_to_unreachable_is_dropped():
    relay = Relay("srv")
    err = _env("ghost:x", "ghost:y", "n/a")
    err = Envelope(src=err.src, dst=err.dst, type="err", body={})
    assert relay.deliver(err) == BOUNCED


def test_unknown_src_bounce_is_silent():
    relay = Relay("srv")
    # dst unreachable AND src unreachable → plain BOUNCED, no crash
    assert relay.deliver(_env("ghost:local", "nowhere:local", "hi")) == BOUNCED


def test_unregister_local():
    relay = Relay("srv")
    relay.register_local("srv:local")
    relay.unregister_local("srv:local")
    assert relay.deliver(_env("x:local", "srv:local", "hi")) == BOUNCED
