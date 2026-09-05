"""Mood kaomoji: tiny parallel LLM call per turn, taped as a sink event,
gated by Config.kaomoji. Purely decorative — failures never touch the turn."""

import json
import threading
import time
import urllib.request

import pytest

from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.room import RoomServer
from fungi.server import _TURN_TAPES, _kaomoji_call, make_webui_server


def _wait(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _fake_llm(calls: list):
    def fake_llm(messages, _tools):
        calls.append(list(messages))
        if messages and messages[0].get("role") == "system" and "kaomoji" in messages[0]["content"].lower():
            return LLMResult(content="(=^･ω･^=)")
        return LLMResult(content="reply")

    return fake_llm


def _build_room(tmp_path, calls, kaomoji: bool) -> RoomServer:
    cfg = Config(api_key="k", endpoint="e", model="m", kaomoji=kaomoji)
    room = RoomServer(
        "alpha", cfg, NullSink(), "tok", tmp_path / "data",
        llm=_fake_llm(calls), rules_path=tmp_path / "rules.json",
    )
    room.start()
    return room


def _serve(room):
    server = make_webui_server(0, room.webui_runtime())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_turn_emits_kaomoji_event(tmp_path):
    """The parallel mood call lands in the tape as a kaomoji event."""
    calls: list[list] = []
    room = _build_room(tmp_path, calls, kaomoji=True)
    server = _serve(room)
    try:
        port = server.server_address[1]
        with _post(port, "/chat", {"message": "hello mood", "sessionId": None}) as resp:
            resp.read()
        sid = room.webui_runtime().sessions_list()[0]["id"]
        assert _wait(
            lambda: any(
                ev.get("type") == "kaomoji" and ev.get("content") == "(=^･ω･^=)"
                for ev in _TURN_TAPES.get(sid, [])
            )
        ), "kaomoji event never reached the tape"
        # Both calls went through the same injected llm: mood + main turn.
        assert len(calls) == 2
    finally:
        room.stop()
        server.shutdown()
        server.server_close()


def test_kaomoji_disabled_skips_the_call(tmp_path):
    """Config.kaomoji=False: no mood call, no event — the brand name stays."""
    calls: list[list] = []
    room = _build_room(tmp_path, calls, kaomoji=False)
    server = _serve(room)
    try:
        port = server.server_address[1]
        with _post(port, "/chat", {"message": "no mood", "sessionId": None}) as resp:
            resp.read()
        sid = room.webui_runtime().sessions_list()[0]["id"]
        assert _wait(lambda: any(
            m.get("role") == "assistant" and m.get("content") == "reply"
            for m in (room.webui_runtime().sessions_load(sid) or {}).get("messages", [])
        )), "turn never completed"
        assert not any(
            c and c[0].get("role") == "system" and "kaomoji" in c[0]["content"].lower()
            for c in calls
        ), "mood call ran despite kaomoji=False"
        assert not any(ev.get("type") == "kaomoji" for ev in _TURN_TAPES.get(sid, []))
    finally:
        room.stop()
        server.shutdown()
        server.server_close()


def test_kaomoji_call_guards_length_and_empty():
    cfg = Config(api_key="k", endpoint="e", model="m")
    assert _kaomoji_call(cfg, lambda _m, _t: LLMResult(content="(=^･ω･^=)"), "hi", None) == "(=^･ω･^=)"
    assert _kaomoji_call(cfg, lambda _m, _t: LLMResult(content="x" * 64), "hi", None) is None
    assert _kaomoji_call(cfg, lambda _m, _t: LLMResult(content=""), "hi", None) is None


def test_config_roundtrip(tmp_path):
    """The toggle persists: default on, off survives save/load."""
    from fungi.config import load_config, save_config

    path = tmp_path / "config.json"
    cfg = Config(api_key="k", kaomoji=False)
    save_config(cfg, path)
    assert load_config(path).kaomoji is False
    save_config(Config(api_key="k"), path)
    assert load_config(path).kaomoji is True


def test_config_page_has_kaomoji_switch(qapp):
    """GUI config page: the toggle exists and mirrors the default."""
    from fungi.gui import ConfigPage

    page = ConfigPage(None)
    assert page.kaomoji_switch.isChecked() is True
    page.kaomoji_switch.setChecked(False)
    assert page.kaomoji_switch.isChecked() is False
