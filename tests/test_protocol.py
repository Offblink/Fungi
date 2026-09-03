"""Envelope addressing + wire format contract tests."""

import pytest

from fungi.protocol import (
    ENVELOPE_VERSION,
    Envelope,
    ProtocolError,
    deserialize,
    error_envelope,
    parse_addr,
)


def test_parse_addr_valid():
    assert parse_addr("alpha:local") == ("alpha", "local", None)
    assert parse_addr("alpha:comm-beta") == ("alpha", "comm", "beta")


@pytest.mark.parametrize(
    "addr", ["", "alpha", "alpha:", "alpha:comm-", "alpha:foo", "a:b:c", "alpha:local:x", ":local"]
)
def test_parse_addr_invalid(addr):
    with pytest.raises(ProtocolError):
        parse_addr(addr)


def test_roundtrip_and_defaults():
    env = Envelope(src="alpha:comm-beta", dst="beta:comm-alpha", type="chat", body={"text": "hi"})
    assert env.id and env.ts and env.v == ENVELOPE_VERSION
    data = env.serialize()
    out = deserialize(data)
    assert out.id == env.id
    assert out.ts == env.ts
    assert out.src == env.src
    assert out.dst == env.dst
    assert out.type == "chat"
    assert out.body == {"text": "hi"}


def test_deserialize_rejects_bad_fields():
    base = {
        "v": 1,
        "id": "x",
        "src": "a:local",
        "dst": "b:local",
        "type": "chat",
        "body": {},
    }
    for mutate in (
        {"v": 2},
        {"type": "nope"},
        {"body": "text"},
        {"src": "bad"},
        {"dst": "b:comm-"},
        {"reply_to": 7},
    ):
        with pytest.raises(ProtocolError):
            deserialize({**base, **mutate})
    with pytest.raises(ProtocolError):
        deserialize("not a dict")


def test_error_envelope_routes_back():
    env = Envelope(src="alpha:comm-beta", dst="nowhere:local", type="chat", body={})
    err = error_envelope(env, "unreachable destination")
    assert err.src == env.dst
    assert err.dst == env.src
    assert err.type == "err"
    assert err.reply_to == env.id
    assert err.body["error"] == "unreachable destination"
    deserialize(err.serialize())  # wire-valid
