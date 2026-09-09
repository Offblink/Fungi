"""Shell tool: run a command via cmd.exe with UTF-8 codepage and a hard timeout.

Uses a temp .bat file (chcp 65001 trick from the PowerShell original) to avoid
quoting hell when the command contains quotes, pipes, or redirections.
"""

import contextlib
import os
import signal
import subprocess
import tempfile
import uuid
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
