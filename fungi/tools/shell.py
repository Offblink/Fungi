"""Shell tool: run a command via cmd.exe with UTF-8 codepage and a hard timeout.

Uses a temp .bat file (chcp 65001 trick from the PowerShell original) to avoid
quoting hell when the command contains quotes, pipes, or redirections.
"""

import codecs
import contextlib
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from pathlib import Path

BASH_TIMEOUT = 600
TRUNCATE_BASH = 8000


def _truncate(text: str, limit: int = TRUNCATE_BASH) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [truncated {len(text) - limit} chars] ...\n{text[-half:]}"


def tool_bash(
    command: str, cwd: str | None = None, should_abort: Callable[[], bool] | None = None
) -> str:
    bat = Path(tempfile.gettempdir()) / f"fungi-{os.getpid()}-{uuid.uuid4().hex[:8]}.bat"
    try:
        bat.write_text(f"chcp 65001 >nul\r\n{command}\r\n", encoding="utf-8", newline="")
        proc = subprocess.Popen(
            ["cmd.exe", "/c", str(bat)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # stdin=NUL: an interactive command (date/pause/...) reads EOF and
            # exits at once instead of holding the turn hostage for the full
            # timeout — fast-fail without banning any command.
            stdin=subprocess.DEVNULL,
            cwd=cwd or None,
            start_new_session=os.name != "nt",  # own group: killpg on abort
        )
    except OSError as exc:
        bat.unlink(missing_ok=True)
        return f"ERROR: {exc}"

    def _kill_tree() -> None:
        """The command may have its own children; kill the whole tree."""
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        with contextlib.suppress(OSError):
            proc.kill()

    try:
        # Poll instead of blocking communicate(): a stop press must take
        # effect within ~0.2s, not after the command finishes (up to 10min).
        deadline = BASH_TIMEOUT
        while True:
            try:
                out_b, err_b = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if should_abort is not None and should_abort():
                _kill_tree()
                return "ERROR: cancelled by user"
            deadline -= 0.2
            if deadline <= 0:
                _kill_tree()
                return f"ERROR: Timed out after {BASH_TIMEOUT}s"
        out = out_b.decode("utf-8", errors="replace")
        err = err_b.decode("utf-8", errors="replace")
        result = out
        if err:
            result += ("\n[stderr]\n" if result else "") + err
        if not result.strip():
            result = "(no output)"
        if proc.returncode != 0:
            result += f"\n[exit: {proc.returncode}]"
        return _truncate(result)
    except OSError as exc:
        return f"ERROR: {exc}"
    finally:
        bat.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Session bash: REPL-style read/write interaction with a child process.
# bash_start launches with stdin=PIPE and returns at once; bash_send feeds one
# line and collects what the child printed; the child's output is pumped into
# a buffer by reader threads (Windows pipes have no select()). Sessions die
# when the owning turn ends (the abort callable is held via WeakMethod — when
# the Agent is garbage-collected the session is orphaned), on /stop (abort
# fires), or after BASH_SESSION_IDLE seconds of silence. A daemon reaper
# enforces all three so no orphan cmd survives the turn.
# ---------------------------------------------------------------------------

BASH_SESSION_IDLE = 600
SEND_READ_WAIT = 2.0

_SESSIONS: dict[str, "_BashSession"] = {}
_SESSIONS_LOCK = threading.Lock()
_REAPER: threading.Thread | None = None


class _BashSession:
    def __init__(
        self,
        sid: str,
        proc: subprocess.Popen,
        bat: Path,
        interactive: bool,
        should_abort: Callable[[], bool] | None,
    ) -> None:
        self.id = sid
        self.proc = proc
        self.bat = bat
        self.interactive = interactive
        self.lock = threading.Lock()  # serializes bash_send per session
        self.buf_lock = threading.Lock()
        self.chunks: list[str] = []
        self.last_activity = time.monotonic()
        # WeakMethod: a bound method (Agent._aborted) dies with its Agent, so
        # a finished turn orphans the session and the reaper collects it. A
        # bare lambda (tests, callers without an Agent) can't be weak-ref'd:
        # fall back to a strong ref, the idle timeout still bounds it.
        if should_abort is None:
            self._abort = None
        else:
            try:
                self._abort = weakref.WeakMethod(should_abort)
            except TypeError:
                self._abort = should_abort

    def aborted(self) -> bool:
        if self._abort is None:
            return False
        if isinstance(self._abort, weakref.WeakMethod):
            fn = self._abort()
            if fn is None:
                return True  # owning agent gone: turn ended
            return bool(fn())
        return bool(self._abort())

    def snapshot(self, pos: int) -> tuple[str, int]:
        with self.buf_lock:
            text = "".join(self.chunks)
        return text[pos:], len(text)

    def _pump(self, stream) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            for raw in iter(lambda: stream.read1(65536), b""):
                piece = decoder.decode(raw)
                if piece:
                    with self.buf_lock:
                        self.chunks.append(piece)
            piece = decoder.decode(b"", True)
            if piece:
                with self.buf_lock:
                    self.chunks.append(piece)
        except (OSError, ValueError):
            pass  # pipe broke on kill: normal exit path for a killed session


def _kill_session_tree(s: _BashSession) -> None:
    proc = s.proc
    try:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    with contextlib.suppress(OSError):
        proc.kill()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        with contextlib.suppress(OSError, ValueError):
            if stream is not None:
                stream.close()
    s.bat.unlink(missing_ok=True)


def _reap_sessions_once(force_idle_check: bool = False) -> None:
    """Kill + drop sessions that exited, lost their turn, or idled out."""
    now = time.monotonic()
    with _SESSIONS_LOCK:
        victims = [
            s
            for s in _SESSIONS.values()
            if s.proc.poll() is not None
            or s.aborted()
            or (force_idle_check and now - s.last_activity > BASH_SESSION_IDLE)
        ]
        for s in victims:
            _SESSIONS.pop(s.id, None)
    for s in victims:
        _kill_session_tree(s)


def _ensure_reaper() -> None:
    global _REAPER  # noqa: PLW0603 (module-level singleton thread)
    if _REAPER is not None and _REAPER.is_alive():
        return

    def _loop() -> None:
        while True:
            time.sleep(2)
            _reap_sessions_once(force_idle_check=True)

    _REAPER = threading.Thread(target=_loop, name="bash-session-reaper", daemon=True)
    _REAPER.start()


def _start_session(
    command: str,
    cwd: str | None,
    stdin_arg: str,
    should_abort: Callable[[], bool] | None,
) -> str:
    if stdin_arg not in ("nul", "pipe"):
        return "ERROR: stdin_arg must be 'nul' or 'pipe'"
    bat = Path(tempfile.gettempdir()) / f"fungi-{os.getpid()}-{uuid.uuid4().hex[:8]}.bat"
    try:
        bat.write_text(f"@echo off\r\nchcp 65001 >nul\r\n{command}\r\n", encoding="utf-8", newline="")
        proc = subprocess.Popen(
            ["cmd.exe", "/c", str(bat)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL if stdin_arg == "nul" else subprocess.PIPE,
            cwd=cwd or None,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        bat.unlink(missing_ok=True)
        return f"ERROR: {exc}"
    sid = uuid.uuid4().hex[:8]
    s = _BashSession(sid, proc, bat, stdin_arg == "pipe", should_abort)
    threading.Thread(target=s._pump, args=(proc.stdout,), daemon=True).start()
    threading.Thread(target=s._pump, args=(proc.stderr,), daemon=True).start()
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = s
    _ensure_reaper()
    _reap_sessions_once()
    # Brief window for the first output: the caller usually wants the
    # initial prompt/state, not a guaranteed "(no output yet)".
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if s.proc.poll() is not None:
            break
        out, _ = s.snapshot(0)
        if out.strip():
            break
        time.sleep(0.1)
    out, _ = s.snapshot(0)
    kind = "interactive (bash_send can feed input)" if s.interactive else "stdin=NUL: interactive input sees EOF"
    return f"id={sid} ({kind})\n{out.strip() or '(no output yet)'}"


def tool_bash_start(
    command: str,
    cwd: str | None = None,
    stdin_arg: str = "nul",
    should_abort: Callable[[], bool] | None = None,
) -> str:
    """Start a command as a background session; returns the session id and
    whatever it printed so far. See module docstring for lifecycle."""
    return _start_session(command, cwd, stdin_arg, should_abort)


def tool_bash_send(id: str, text: str = "") -> str:
    sid = str(id or "")
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
    if s is None:
        return f"ERROR: no such bash session: {sid} (it exited or was reaped; re-run bash_start)"
    if not s.interactive:
        return "ERROR: session was started with stdin=nul — start a new one with stdin_arg='pipe' to feed input"
    _, pos = s.snapshot(0)
    with s.lock:
        if s.proc.poll() is not None:
            extra, _ = s.snapshot(pos)
            return (extra.strip() or "(no output)") + f"\n[session {sid} exited]"
        try:
            s.proc.stdin.write((text + "\n").encode("utf-8"))
            s.proc.stdin.flush()
        except OSError:
            extra, _ = s.snapshot(pos)
            return (extra.strip() or "(no output)") + f"\n[session {sid} exited (stdin closed)]"
        s.last_activity = time.monotonic()
        deadline = time.monotonic() + SEND_READ_WAIT
        last_len = pos
        while time.monotonic() < deadline:
            time.sleep(0.15)
            cur, _ = s.snapshot(pos)
            if len(cur) > last_len:
                last_len = len(cur)
                deadline = min(deadline + 0.3, time.monotonic() + 0.5)
            if s.proc.poll() is not None:
                break
        extra, _ = s.snapshot(pos)
    s.last_activity = time.monotonic()
    result = extra.strip() or "(no new output)"
    if s.proc.poll() is not None:
        with _SESSIONS_LOCK:
            _SESSIONS.pop(sid, None)
        _kill_session_tree(s)
        result += f"\n[session {sid} exited, exit code {s.proc.returncode}]"
    return _truncate(result)


def tool_bash_kill(id: str) -> str:
    sid = str(id or "")
    with _SESSIONS_LOCK:
        s = _SESSIONS.pop(sid, None)
    if s is None:
        return f"ERROR: no such bash session: {sid}"
    _kill_session_tree(s)
    return f"session {sid} killed"
