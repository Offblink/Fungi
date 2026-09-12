"""Windows Firewall: may THIS program receive LAN connections?

The phone reaches the desktop WebUI on a LAN address, so Windows Firewall must
let inbound traffic through **for the program holding the listening socket**.
That allowance is per program: a source install was granted it long ago
(python.exe/pythonw.exe have Public-profile rules), while a freshly extracted
Fungi.exe usually has none — the phone then simply times out, with no error
visible anywhere on the desktop. This module answers that question and can ask
Windows for the rule through UAC (same one-click repair shape as the segno and
VidSense buttons).

Everything here degrades to "unknown" instead of raising: a GUI page must never
break because a firewall query did.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

RULE_PREFIX = "Fungi mobile WebUI"
_FALSE_TTL = 30.0  # a "no rule yet" answer goes stale: the user may have just allowed it
_CACHE: dict[str, tuple[bool, float]] = {}


def supported() -> bool:
    return os.name == "nt"


def program_path() -> str:
    """The image the firewall filters on: the exe itself when frozen."""
    return os.path.realpath(sys.executable)


def program_label(program: str | None = None) -> str:
    return Path(program or program_path()).name


def rule_name(program: str | None = None) -> str:
    return f"{RULE_PREFIX} ({program_label(program)})"


def _powershell() -> str:
    return "powershell.exe"


def _encoded(script: str) -> str:
    """`-EncodedCommand` payload: quoted and CJK paths survive intact."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _check_script(program: str) -> str:
    quoted = program.replace("'", "''")
    return (
        f"$p = '{quoted}';"
        " $n = 0;"
        " Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |"
        " Where-Object { $_.Program -eq $p } | ForEach-Object {"
        "  $r = $_ | Get-NetFirewallRule;"
        "  if ($r.Enabled -eq 'True' -and $r.Direction -eq 'Inbound'"
        " -and $r.Action -eq 'Allow') { $n++ } };"
        " Write-Output $n"
    )


def check_command(program: str | None = None) -> list[str]:
    """The probe: prints how many enabled inbound-allow rules name this program."""
    return [
        _powershell(),
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        _encoded(_check_script(program or program_path())),
    ]


def parse_count(output: str) -> bool | None:
    """The probe prints how many matching rules it found: 0 -> blocked, >0 ->
    allowed, anything else -> unknown (PowerShell may prefix module warnings).

    Counting matters: a program can own several matching rules at once
    (python.exe has two), so only the last non-empty line is read and compared
    as a number."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return None
    token = lines[-1]
    if token.isdigit():
        return int(token) > 0
    return None


def _remember(program: str, allowed: bool) -> None:
    _CACHE[program] = (allowed, time.monotonic())


def forget() -> None:
    """Drop the cached verdict (after the user granted the rule)."""
    _CACHE.clear()


def cached(program: str | None = None) -> bool | None:
    """Last verdict, if it is still trustworthy; None means "probe again"."""
    prog = program or program_path()
    hit = _CACHE.get(prog)
    if hit is None:
        return None
    allowed, at = hit
    if allowed or time.monotonic() - at < _FALSE_TTL:
        return allowed
    return None


def start_check(program: str | None = None) -> subprocess.Popen | None:
    """Launch the probe; None when there is nothing to check (or no PowerShell)."""
    if not supported():
        return None
    try:
        return subprocess.Popen(
            check_command(program),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError:
        return None


def finish_check(proc: subprocess.Popen, program: str | None = None) -> bool | None:
    """Read a finished probe (the caller polls `proc.poll()` first)."""
    out = ""
    with contextlib.suppress(OSError, ValueError):
        if proc.stdout is not None:
            out = proc.stdout.read()
            proc.stdout.close()
    verdict = parse_count(out)
    if verdict is not None:
        _remember(program or program_path(), verdict)
    return verdict


def _allow_script(program: str) -> str:
    quoted = program.replace("'", "''")
    name = rule_name(program).replace("'", "''")
    return (
        f"$p = '{quoted}'; $name = '{name}';"
        " Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue"
        " | Remove-NetFirewallRule;"
        " New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow"
        " -Protocol TCP -Program $p -Profile Private, Public -Enabled True | Out-Null;"
        " if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)"
        " { Write-Output 'OK' } else { Write-Output 'FAILED' }"
    )


def allow_command(program: str | None = None) -> list[str]:
    """The elevated script: (re)create the inbound allow rule for this program."""
    return [
        _powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        _encoded(_allow_script(program or program_path())),
    ]


def request_allow(program: str | None = None) -> str | None:
    """Trigger UAC and add the rule. None = handed to Windows (the user may still
    cancel the prompt); otherwise a one-line reason for the GUI to show."""
    if not supported():
        return "只有 Windows 需要放行防火墙"
    cmd = allow_command(program)
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", cmd[0], subprocess.list2cmdline(cmd[1:]), None, 1
        )
    except OSError as exc:  # pragma: no cover - defensive: ctypes call itself
        return str(exc)
    if rc <= 32:
        return "已取消授权" if rc == 5 else f"提权失败（代码 {rc}）"
    forget()
    return None
