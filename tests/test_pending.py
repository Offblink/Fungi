"""PendingAsks: dual wake sources (in-process resolve / answer envelope)."""

import threading

from fungi.hub.relay import Relay
from fungi.pending import PendingAsks
from fungi.protocol import Envelope, new_id


def test_resolve_wakes_with_value():
    pending = PendingAsks()
    pending.register("a1")
    out: list = []

    def waiter():
        out.append(pending.wait("a1", timeout_s=5))

    t = threading.Thread(target=waiter)
    t.start()
    threading.Event().wait(0.05)
    assert pending.resolve("a1", "yes") is True
    t.join(timeout=2)
    assert out == [(True, "yes")]


def test_timeout_branch():
    pending = PendingAsks()
    pending.register("a2")
    assert pending.wait("a2", timeout_s=0.05) == (False, None)
    pending.discard("a2")
    # expired id no longer resolvable
    assert pending.resolve("a2", "late") is False
    assert len(pending) == 0


def test_denied_is_passthrough():
    pending = PendingAsks()
    pending.register("a3")
    pending.resolve("a3", "no")
    assert pending.wait("a3", timeout_s=1) == (True, "no")


def test_custom_answer_passthrough():
    pending = PendingAsks()
    pending.register("a4")
    value = ["option B", "自由输入"]
    pending.resolve("a4", value)
    assert pending.wait("a4", timeout_s=1) == (True, value)


def test_resolve_unknown_id():
    assert PendingAsks().resolve("nope", "x") is False


def test_heartbeat_called_while_blocked():
    pending = PendingAsks()
    pending.register("a5")
    beats: list = []
    result: list = []

    def waiter():
        result.append(
            pending.wait(
                "a5", timeout_s=0.3, heartbeat_s=0.05, on_heartbeat=lambda: beats.append(1)
            )
        )

    t = threading.Thread(target=waiter)
    t.start()
    t.join(timeout=2)
    assert result == [(False, None)]
    assert len(beats) >= 2  # 0.3s window with 0.05s heartbeat


def test_answer_envelope_wakes_via_message_plane():
    """Full plane loop: ask envelope out -> answer envelope back -> wake."""
    relay = Relay("srv")
    pending = PendingAsks()
    ask_id = new_id()

    # asker: local clone of alpha hosts the ask; its inbox loop dispatches answers
    alpha_inbox = relay.host_buffer("alpha")
    result: list = []

    def asker():
        pending.register(ask_id)
        try:
            result.append(pending.wait(ask_id, timeout_s=5))
        finally:
            pending.discard(ask_id)

    def responder():
        # beta's local clone picks up the ask envelope; the user answers
        beta_inbox = relay.host_buffer("beta")
        messages, _cursor = beta_inbox.after(0, 2.0)
        ask = messages[0]
        assert ask.type == "ask"
        answer = Envelope(
            src=ask.dst,
            dst=ask.src,
            type="answer",
            body={"value": "allowed with changes"},
            reply_to=ask.id,
        )
        relay.deliver(answer)

    def inbox_loop():
        # simulates the clone's inbox thread: answer envelopes wake pending asks
        while True:
            messages, _cursor = alpha_inbox.after(0, 0.05)
            for env in messages:
                if env.type == "answer" and env.reply_to:
                    pending.resolve(env.reply_to, env.body.get("value"))
            if result:
                return

    threads = [
        threading.Thread(target=asker),
        threading.Thread(target=responder),
        threading.Thread(target=inbox_loop),
    ]
    for t in threads:
        t.start()

    # asker side: send the ask envelope onto the plane
    ask_env = Envelope(
        src="alpha:comm-beta",
        dst="beta:local",
        type="ask",
        body={"question": "write homes/beta/x?"},
        id=ask_id,
    )
    assert relay.deliver(ask_env) == "queued"

    for t in threads:
        t.join(timeout=5)
    assert result == [(True, "allowed with changes")]
    assert len(pending) == 0
