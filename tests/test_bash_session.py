"""Session bash (bash_start/bash_send/bash_kill): REPL-style interaction.

Acceptance: a two-input script answers both prompts; interactive `date` sees
EOF and exits on stdin=nul; sessions die on abort (=/stop), when the owning
agent is garbage-collected (turn end), on kill, and on the idle cap.
"""

import gc
import sys
import time

import pytest

from fungi.tools import dispatch
from fungi.tools import shell as shell_mod
from fungi.tools.shell import tool_bash_kill, tool_bash_send, tool_bash_start


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    with shell_mod._SESSIONS_LOCK:
        sessions = list(shell_mod._SESSIONS.values())
        shell_mod._SESSIONS.clear()
    for s in sessions:
        shell_mod._kill_session_tree(s)


def _sid(out: str) -> str:
    return out.split(" ", 1)[0].removeprefix("id=")


def _alive(sid: str) -> bool:
    with shell_mod._SESSIONS_LOCK:
        s = shell_mod._SESSIONS.get(sid)
    return s is not None and s.proc.poll() is None


def _wait_exit(sid: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(sid):
            return True
        time.sleep(0.1)
    return False


def test_two_question_script_full_interaction(tmp_path):
    script = tmp_path / "two.py"
    script.write_text(
        "print('Q1', flush=True)\n"
        "input()\n"
        "print('Q2', flush=True)\n"
        "input()\n"
        "print('DONE', flush=True)\n",
        encoding="utf-8",
    )
    out = tool_bash_start(f'"{sys.executable}" "{script}"', stdin_arg="pipe")
    sid = _sid(out)
    assert "interactive" in out
    assert "Q1" in out

    second = tool_bash_send(id=sid, text="answer1")
    assert "Q2" in second

    final = tool_bash_send(id=sid, text="answer2")
    assert "DONE" in final
    assert "[session" in final and "exited" in final


def test_interactive_date_eof_exits_on_nul():
    out = tool_bash_start("date")  # bare date waits for input
    sid = _sid(out)
    assert sid
    assert _wait_exit(sid), "interactive date should read EOF and exit"
    shell_mod._reap_sessions_once()
    assert "ERROR: no such bash session" in tool_bash_send(id=sid, text="x")


def test_send_rejects_nul_session():
    out = tool_bash_start("ping -n 10 127.0.0.1 >nul")  # default stdin=nul
    sid = _sid(out)
    assert "stdin=nul" in tool_bash_send(id=sid, text="x")
    tool_bash_kill(id=sid)


def test_abort_kills_session():
    flag = {"on": False}
    out = tool_bash_start(
        "ping -n 30 127.0.0.1 >nul", should_abort=lambda: flag["on"]
    )
    sid = _sid(out)
    assert _alive(sid)
    flag["on"] = True  # /stop pressed
    shell_mod._reap_sessions_once()
    assert sid not in shell_mod._SESSIONS
    assert not _alive(sid)


def test_turn_end_orphans_session_via_weakref():
    class Agent:
        def aborted(self):
            return False

    agent = Agent()
    out = tool_bash_start("ping -n 30 127.0.0.1 >nul", should_abort=agent.aborted)
    sid = _sid(out)
    shell_mod._reap_sessions_once()
    assert _alive(sid)  # owning agent alive: session survives
    del agent
    gc.collect()
    shell_mod._reap_sessions_once()  # turn ended: agent GC'd
    assert sid not in shell_mod._SESSIONS


def test_bash_kill():
    out = tool_bash_start("ping -n 30 127.0.0.1 >nul")
    sid = _sid(out)
    assert "killed" in tool_bash_kill(id=sid)
    assert sid not in shell_mod._SESSIONS


def test_idle_cap_reaps(monkeypatch):
    monkeypatch.setattr(shell_mod, "BASH_SESSION_IDLE", 0)
    out = tool_bash_start("ping -n 30 127.0.0.1 >nul")
    sid = _sid(out)
    shell_mod._reap_sessions_once(force_idle_check=True)
    assert sid not in shell_mod._SESSIONS


def test_dispatch_wiring():
    out = dispatch("bash_start", {"command": "echo hi"})
    sid = _sid(out)
    assert "hi" in out
    assert "unknown" not in tool_bash_kill(id=sid).lower()
