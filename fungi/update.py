"""Self-update: compare the pyproject version against GitHub Releases.

Design (user-ratified): the GUI *checks* automatically in the background but
never updates on its own — when a newer release exists, an update button
appears on the ConfigPage and the update only happens on click.

Three deployment shapes, three actions:
- frozen exe (blank host, no git): download the ``fungi-v*-windows-x64.zip``
  release asset over plain HTTPS, swap exe + _internal via a rename dance
  (a running exe cannot be overwritten, but it can be renamed), relaunch.
- source checkout with git: ``git pull --ff-only``.
- anything else (source zip without git): fall back to opening the Releases
  page in the browser.

Version lives solely in pyproject.toml; the exe bundle ships a copy of it
(release.yml --add-data), resolved from RESOURCE_ROOT when frozen.
"""

import contextlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

from .config import PROJECT_ROOT, RESOURCE_ROOT

GITHUB_REPO = "Offblink/Fungi"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_ZIP_SUFFIX = "-windows-x64.zip"

# relaunch must survive the dying parent process
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

_OLD_EXE = "Fungi.exe.old"
_OLD_INTERNAL = "_internal.old"


def _pyproject() -> Path:
    """Source layout: repo root. Frozen: the copy bundled next to web/."""
    for cand in (PROJECT_ROOT / "pyproject.toml", RESOURCE_ROOT / "pyproject.toml"):
        if cand.is_file():
            return cand
    raise FileNotFoundError("pyproject.toml not found (bundled copy missing?)")


def local_version() -> str:
    """Version string from pyproject.toml, e.g. "0.1.1"."""
    with _pyproject().open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _vtuple(version: str) -> tuple[int, ...] | None:
    """("v0.1.2") -> (0, 1, 2); None when not a plain numeric release tag."""
    m = re.fullmatch(r"[vV]?(\d+(?:\.\d+)+)", version.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def update_mode() -> str:
    """Which action the update button takes: "exe" | "git" | "none"."""
    if getattr(sys, "frozen", False):
        return "exe"
    if (PROJECT_ROOT / ".git").exists() and shutil.which("git"):
        return "git"
    return "none"


def fetch_latest(timeout: float = 10.0) -> dict:
    """Latest GitHub release: {"tag", "asset_url"}. asset_url is None when the
    release carries no windows zip (source-only release)."""
    req = urllib.request.Request(
        _API_LATEST,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Fungi"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    tag = data.get("tag_name", "")
    asset_url = None
    for asset in data.get("assets") or ():
        name = asset.get("name", "")
        if name.endswith(_ZIP_SUFFIX) and (name == f"fungi-{tag}{_ZIP_SUFFIX}" or asset_url is None):
            asset_url = asset.get("browser_download_url")
    return {"tag": tag, "asset_url": asset_url}


def check() -> dict:
    """Full status for the GUI: current/latest versions, behind flag, action.

    Network or parse failures land in {"error"}; the GUI shows them as text
    and keeps working (checking must never take the app down).
    """
    status: dict = {"mode": update_mode(), "current": None, "latest": None,
                    "behind": False, "asset_url": None, "error": None}
    try:
        status["current"] = local_version()
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        status["error"] = f"读取本地版本失败：{exc}"
        return status
    try:
        latest = fetch_latest()
    except OSError as exc:  # urllib raises URLError/OSError family on GFW flake
        status["error"] = f"检查更新失败（网络）：{exc}"
        return status
    tag = latest["tag"]
    cur, new = _vtuple(status["current"]), _vtuple(tag)
    if new is None:
        status["error"] = f"远端版本号无法解析：{tag!r}"
        return status
    if cur is None:
        status["error"] = f"本地版本号无法解析：{status['current']!r}"
        return status
    status["latest"] = tag
    status["asset_url"] = latest["asset_url"]
    status["behind"] = new > cur
    return status


def update_source() -> tuple[bool, str]:
    """git pull --ff-only in the repo. Returns (ok, combined output)."""
    git = shutil.which("git")
    if not git:
        return False, "git 不在 PATH 上"
    try:
        proc = subprocess.run(
            [git, "pull", "--ff-only"], cwd=PROJECT_ROOT, capture_output=True,
            text=True, timeout=300, encoding="utf-8", errors="replace", check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "git pull 超时（300s）"
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out


def _pick_zip_asset_dir(extract_root: Path) -> Path:
    """The extracted folder that directly holds Fungi.exe (release zip layout:
    Fungi/Fungi.exe + Fungi/_internal/)."""
    candidates = [p for p in extract_root.rglob("Fungi.exe") if p.is_file()]
    if not candidates:
        raise FileNotFoundError("zip 内未找到 Fungi.exe")
    return candidates[0].parent


def _download(url: str, dest: Path, progress=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Fungi"})
    with urllib.request.urlopen(req, timeout=30) as resp, dest.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)


def update_exe(asset_url: str, progress=None) -> Path:
    """Download the windows zip, swap the running install in place, and return
    the new exe path (the GUI relaunches it, then quits).

    The rename dance: a running exe cannot be overwritten on Windows but can
    be *renamed* — so Fungi.exe/_internal step aside as .old, the new files
    move in, and .old leftovers are swept by cleanup_old_install() on the
    next start. When the running image is no longer on disk (the folder was
    moved under the live process, or an earlier swap died half-way leaving
    only Fungi.exe.old) there is nothing to step aside: the new files move
    straight in, which is also the recovery from that state — 2026-09-11, the
    pc box failed the whole update on `WinError 2` renaming a missing
    Fungi.exe. Any OSError behind the dance puts the old install back whole.
    """
    exe = Path(sys.executable).resolve()
    root = exe.parent
    with tempfile.TemporaryDirectory(prefix="fungi-update-") as tmp:
        zip_path = Path(tmp) / "update.zip"
        _download(asset_url, zip_path, progress)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(Path(tmp) / "x")
        new_dir = _pick_zip_asset_dir(Path(tmp) / "x")
        new_exe = new_dir / "Fungi.exe"
        new_internal = new_dir / "_internal"

        old_exe = root / _OLD_EXE
        old_internal = root / _OLD_INTERNAL
        internal = root / "_internal"
        _sweep(old_exe)
        _sweep(old_internal)

        backed_up_exe = exe.is_file()  # False: nothing at sys.executable to keep
        backed_up_internal = internal.is_dir()
        try:
            if backed_up_exe:
                exe.rename(old_exe)
            if backed_up_internal:
                internal.rename(old_internal)
            shutil.move(str(new_exe), str(exe))
            if new_internal.exists():  # the bundle brings the runtime, not the layout
                shutil.move(str(new_internal), str(internal))
        except OSError:
            # Put the old install back whole (partial copies from this attempt
            # go away first), so the app keeps working on the version it runs.
            if backed_up_internal and old_internal.exists():
                _sweep(internal)
                old_internal.rename(internal)
            if backed_up_exe and old_exe.exists():
                _sweep(exe)
                old_exe.rename(exe)
            raise
    return exe


def _sweep(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        with contextlib.suppress(OSError):
            path.unlink()  # loaded DLLs can't be deleted yet; swept on next start


def cleanup_old_install() -> None:
    """Remove .old leftovers from a previous in-place update (call at GUI
    start: the fresh process holds no locks on them)."""
    exe_dir = Path(sys.executable).resolve().parent
    _sweep(exe_dir / _OLD_EXE)
    _sweep(exe_dir / _OLD_INTERNAL)


def relaunch(exe: Path) -> None:
    """Start the updated exe detached from the current (dying) process."""
    flags = 0
    if sys.platform == "win32":
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(exe)], cwd=str(exe.parent), close_fds=True,
        creationflags=flags, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
