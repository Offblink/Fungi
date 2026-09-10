"""Tests for session storage (schema compatibility with the PowerShell original)."""

import json
from pathlib import Path

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


def test_a_save_that_dies_leaves_the_old_file_intact(monkeypatch, tmp_path):
    """A torn save is what made a healthy conversation read back as nothing:
    the new file may only appear once it is complete."""
    session.save_session("s1", "old title", [{"role": "user", "content": "old"}])
    real_write = Path.write_text

    def dies_after_writing(self, *args, **kwargs):
        real_write(self, *args, **kwargs)  # the bytes land, then the save dies
        raise OSError("interrupted")

    with monkeypatch.context() as mp:
        mp.setattr(Path, "write_text", dies_after_writing)
        with pytest.raises(OSError):
            session.save_session("s1", "new title", [{"role": "user", "content": "new"}])

    loaded = session.load_session("s1")
    assert loaded["title"] == "old title"
    assert loaded["messages"] == [{"role": "user", "content": "old"}]
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_corrupt_file_is_kept_as_evidence():
    session.ensure_dir()
    path = session.SESSIONS_DIR / "torn.json"
    path.write_text('{"id": "torn", "messages": [{"role"', encoding="utf-8")

    assert session.load_session("torn") is None
    assert not path.exists()
    kept = path.with_name("torn.json.corrupt")
    assert kept.read_text(encoding="utf-8").startswith('{"id": "torn"')  # bytes kept, not dropped
    assert session.list_sessions() == []  # quarantined: no longer listed as a session


def test_a_listing_during_a_save_never_loses_the_session(tmp_path):
    """Windows refuses the atomic rename while a reader holds the file open
    (Python opens without FILE_SHARE_DELETE), and a listing that globbed while a
    rename landed could miss the file — which is how a session vanished from
    /sessions mid-turn (test_room's running-flag test failed exactly this way).
    Our readers and writers are serialized per file."""
    import threading
    import time

    store = session.SessionStore(tmp_path / "sessions")
    store.save("s1", "t", [{"role": "user", "content": "x"}])

    stop = threading.Event()
    failures: list[str] = []
    missing = 0

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            try:
                store.save("s1", "t", [{"role": "user", "content": f"x{n}"}])
            except Exception as exc:
                failures.append(repr(exc))
            time.sleep(0.001)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    deadline = time.monotonic() + 0.6
    try:
        while time.monotonic() < deadline:
            if "s1" not in [row["id"] for row in store.list_sessions()]:
                missing += 1
    finally:
        stop.set()
        thread.join()

    assert failures == []
    assert missing == 0


def test_a_save_waits_out_an_outside_reader(monkeypatch, tmp_path):
    """An outside holder (editor, scanner, second Fungi) has the file open with
    no share-delete, so the rename is refused — wait briefly instead of losing
    the save."""
    store = session.SessionStore(tmp_path / "sessions")
    store.save("s1", "old", [{"role": "user", "content": "old"}])

    holder = (tmp_path / "sessions" / "s1.json").open("rb")
    try:
        import threading

        def release():
            import time

            time.sleep(0.05)
            holder.close()

        threading.Thread(target=release, daemon=True).start()
        store.save("s1", "new", [{"role": "user", "content": "new"}])
    finally:
        if not holder.closed:
            holder.close()

    assert store.load("s1")["title"] == "new"
