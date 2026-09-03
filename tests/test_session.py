"""Tests for session storage (schema compatibility with the PowerShell original)."""

import json

import pytest

from fungi import session


@pytest.fixture(autouse=True)
def tmp_sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path)
    return tmp_path


def test_save_and_load_roundtrip():
    msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    session.save_session("20260901-120000", "hello", msgs)
    loaded = session.load_session("20260901-120000")
    assert loaded is not None
    assert loaded["id"] == "20260901-120000"
    assert loaded["title"] == "hello"
    assert loaded["messages"] == msgs
    assert loaded["created"] == loaded["updated"]


def test_save_preserves_created():
    session.save_session("s1", "t1", [])
    path = session.SESSIONS_DIR / "s1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["created"] = "2000-01-01T00:00:00"
    path.write_text(json.dumps(data), encoding="utf-8")

    session.save_session("s1", "t2", [{"role": "user", "content": "x"}])
    reloaded = session.load_session("s1")
    assert reloaded["created"] == "2000-01-01T00:00:00"
    assert reloaded["title"] == "t2"


def test_title_from_first_user_message():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "  a\nb   c  "}]
    assert session.get_session_title(msgs) == "a b c"


def test_title_truncated():
    long = "x" * 80
    title = session.get_session_title([{"role": "user", "content": long}])
    assert title == "x" * 47 + "..."


def test_title_empty():
    assert session.get_session_title([]) == "(empty)"


def test_list_sessions_newest_first():
    session.save_session("aaa", "first", [{"role": "user", "content": "a"}])
    session.save_session("bbb", "second", [{"role": "user", "content": "b"}])
    ids = [s["id"] for s in session.list_sessions()]
    assert ids == ["bbb", "aaa"]
    assert session.list_sessions()[0]["msgCount"] == 1


def test_list_sessions_skips_broken_files():
    session.ensure_dir()
    (session.SESSIONS_DIR / "broken.json").write_text("{not json", encoding="utf-8")
    assert session.list_sessions() == []


def test_load_missing_returns_none():
    assert session.load_session("nope") is None


def test_delete():
    session.save_session("del1", "t", [])
    session.delete_session("del1")
    assert session.load_session("del1") is None
    session.delete_session("del1")  # idempotent


def test_subagents_roundtrip():
    subs = [
        {
            "id": "a1",
            "call_id": "t1",
            "layer": 2,
            "goal": "g",
            "reply_format": "r",
            "status": "done",
            "events": [{"type": "text", "content": "hi"}],
        }
    ]
    session.save_session("sub1", "t", [{"role": "user", "content": "x"}], subagents=subs)
    loaded = session.load_session("sub1")
    assert loaded["subagents"] == subs

    # merging: second save with different subagents replaces the list
    session.save_session("sub1", "t", [], subagents=[*subs, dict(subs[0], id="a2")])
    assert len(session.load_session("sub1")["subagents"]) == 2

    # sessions saved without the field read back as empty list
    session.save_session("sub2", "t", [])
    assert session.load_session("sub2")["subagents"] == []
