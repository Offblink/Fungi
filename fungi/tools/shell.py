"""Shell tool: run a command via cmd.exe with UTF-8 codepage and a hard timeout.

Uses a temp .bat file (chcp 65001 trick from the PowerShell original) to avoid
quoting hell when the command contains quotes, pipes, or redirections.
"""

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

BASH_TIMEOUT = 120
TRUNCATE_BASH = 8000


def _truncate(text: str, limit: int = TRUNCATE_BASH) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [truncated {len(text) - limit} chars] ...\n{text[-half:]}"


def tool_bash(command: str, cwd: str | None = None) -> str:
    bat = Path(tempfile.gettempdir()) / f"fungi-{os.getpid()}-{uuid.uuid4().hex[:8]}.bat"
    try:
        bat.write_text(f"chcp 65001 >nul\r\n{command}\r\n", encoding="utf-8", newline="")
        proc = subprocess.run(
            ["cmd.exe", "/c", str(bat)],
            check=False,
            capture_output=True,
            timeout=BASH_TIMEOUT,
            cwd=cwd or None,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        result = out
        if err:
            result += ("\n[stderr]\n" if result else "") + err
        if not result.strip():
            result = "(no output)"
        if proc.returncode != 0:
            result += f"\n[exit: {proc.returncode}]"
        return _truncate(result)
    except subprocess.TimeoutExpired:
        return f"ERROR: Timed out after {BASH_TIMEOUT}s"
    except OSError as exc:
        return f"ERROR: {exc}"
    finally:
        bat.unlink(missing_ok=True)
