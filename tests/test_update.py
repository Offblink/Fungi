"""fungi/update.py: version source, release check, and the exe swap dance."""

import json
import sys
import tomllib
import zipfile
from pathlib import Path
from urllib.error import URLError

import pytest

from fungi import update

# ---------- local_version ----------

def test_local_version_reads_pyproject():
    # reads the real pyproject.toml; compare against the file, never a pinned
    # version (that broke the suite on every release bump)
    with Path("pyproject.toml").open("rb") as fh:
        expected = tomllib.load(fh)["project"]["version"]
    assert update.local_version() == expected


def test_local_version_frozen_uses_resource_root(monkeypatch, tmp_path):
    bundled = tmp_path / "pyproject.toml"
    bundled.write_text('[project]\nname = "fungi"\nversion = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(update, "RESOURCE_ROOT", bundled.parent)
    monkeypatch.setattr(update, "PROJECT_ROOT", tmp_path / "nowhere")
    assert update.local_version() == "9.9.9"


# ---------- version comparison ----------

def test_vtuple_parses_release_tags():
    assert update._vtuple("v0.1.2") == (0, 1, 2)
    assert update._vtuple("0.2.0") == (0, 2, 0)
    assert update._vtuple("V1.0") == (1, 0)


def test_vtuple_rejects_non_numeric():
    assert update._vtuple("nightly-2026") is None
    assert update._vtuple("") is None


# ---------- check ----------

def _fake_fetch(monkeypatch, tag, asset=True):
    def fake(*_a, **_kw):
        return {
            "tag": tag,
            "asset_url": f"https://example/fungi-{tag}-windows-x64.zip" if asset else None,
        }
    monkeypatch.setattr(update, "fetch_latest", fake)


def test_check_behind_when_remote_higher(monkeypatch):
    _fake_fetch(monkeypatch, "v9.0.0")
    status = update.check()
    assert status["behind"] is True
    assert status["mode"] == update.update_mode()  # whatever this box is
    assert status["asset_url"].endswith(".zip")


def test_check_current_when_remote_equal_or_lower(monkeypatch):
    _fake_fetch(monkeypatch, "v0.1.1")
    assert update.check()["behind"] is False
    _fake_fetch(monkeypatch, "v0.1.0")
    assert update.check()["behind"] is False


def test_check_survives_network_failure(monkeypatch):
    def boom(*_a, **_kw):
        raise URLError("getaddrinfo failed")
    monkeypatch.setattr(update, "fetch_latest", boom)
    status = update.check()
    assert status["behind"] is False
    assert "网络" in status["error"]


def test_check_survives_unparseable_remote_tag(monkeypatch):
    _fake_fetch(monkeypatch, "nightly-2026")
    status = update.check()
    assert status["behind"] is False
    assert "无法解析" in status["error"]


# ---------- mode selection ----------

def test_mode_none_without_git_and_not_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(update.shutil, "which", lambda _name: None)
    assert update.update_mode() == "none"


def test_mode_git_with_repo_and_binary(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(update, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    assert update.update_mode() == "git"


# ---------- exe swap ----------

class _Resp:
    """Minimal urlopen stand-in handing out the zip bytes in one chunk."""

    def __init__(self, payload):
        self._payload = payload
        self.headers = {}
    def read(self, _n=-1):
        data, self._payload = self._payload, b""
        return data
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def _make_release_zip(root: Path, marker: str = "new") -> Path:
    """A release-layout zip: Fungi/Fungi.exe + Fungi/_internal/web.js."""
    bundle = root / "dist" / "Fungi"
    bundle.mkdir(parents=True)
    (bundle / "Fungi.exe").write_text(f"exe-{marker}", encoding="utf-8")
    internal = bundle / "_internal"
    internal.mkdir()
    (internal / "web.js").write_text(f"js-{marker}", encoding="utf-8")
    zipped = root / "fungi-v9.9.9-windows-x64.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.write(bundle / "Fungi.exe", "Fungi/Fungi.exe")
        zf.write(internal / "web.js", "Fungi/_internal/web.js")
    return zipped


def test_update_exe_swaps_running_install(monkeypatch, tmp_path):
    # fake running install: exe + _internal (as loaded, non-deletable content)
    root = tmp_path / "app"
    root.mkdir()
    (root / "Fungi.exe").write_text("exe-old", encoding="utf-8")
    (root / "_internal").mkdir()
    (root / "_internal" / "web.js").write_text("js-old", encoding="utf-8")
    zipped = _make_release_zip(tmp_path)

    monkeypatch.setattr(sys, "executable", str(root / "Fungi.exe"))
    seen = {}
    def fake_download(url, dest, progress=None):
        seen["url"] = url
        dest.write_bytes(zipped.read_bytes())
        progress and progress(zipped.stat().st_size, zipped.stat().st_size)

    monkeypatch.setattr(update, "_download", fake_download)
    reported = []
    exe = update.update_exe("https://example/fungi-v9.9.9-windows-x64.zip",
                            progress=lambda d, t: reported.append((d, t)))

    assert seen["url"].endswith("fungi-v9.9.9-windows-x64.zip")
    assert exe == root / "Fungi.exe"
    assert exe.read_text(encoding="utf-8") == "exe-new"          # new exe in place
    assert (root / "_internal" / "web.js").read_text(encoding="utf-8") == "js-new"
    old_exe = root / "Fungi.exe.old"
    assert old_exe.read_text(encoding="utf-8") == "exe-old"       # old kept as .old
    assert (root / "_internal.old" / "web.js").exists()
    assert reported == [(zipped.stat().st_size, zipped.stat().st_size)]


def test_update_exe_restores_old_install_on_failure(monkeypatch, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "Fungi.exe").write_text("exe-old", encoding="utf-8")
    (root / "_internal").mkdir()
    (root / "_internal" / "web.js").write_text("js-old", encoding="utf-8")
    zipped = _make_release_zip(tmp_path)

    monkeypatch.setattr(sys, "executable", str(root / "Fungi.exe"))
    monkeypatch.setattr(update, "_download",
                        lambda _url, dest, _progress=None: dest.write_bytes(zipped.read_bytes()))

    def bad_move(_src, _dst):
        raise OSError("disk full")
    monkeypatch.setattr(update.shutil, "move", bad_move)

    with pytest.raises(OSError):
        update.update_exe("https://example/fungi-v9.9.9-windows-x64.zip")

    # rollback: running install intact, .old step-asides reverted
    assert (root / "Fungi.exe").read_text(encoding="utf-8") == "exe-old"
    assert (root / "_internal" / "web.js").read_text(encoding="utf-8") == "js-old"
    assert not (root / "Fungi.exe.old").exists()
    assert not (root / "_internal.old").exists()


def test_cleanup_old_install_sweeps_leftovers(monkeypatch, tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "Fungi.exe").write_text("exe", encoding="utf-8")
    old_exe = root / "Fungi.exe.old"
    old_internal = root / "_internal.old"
    old_exe.write_text("stale", encoding="utf-8")
    old_internal.mkdir()
    (old_internal / "junk.dll").write_text("stale", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(root / "Fungi.exe"))
    update.cleanup_old_install()
    assert not old_exe.exists()
    assert not old_internal.exists()
    assert (root / "Fungi.exe").read_text(encoding="utf-8") == "exe"  # live install untouched


# ---------- source update ----------

def test_update_source_reports_git_failure(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda _name: None)
    ok, out = update.update_source()
    assert ok is False
    assert "git" in out


def test_update_source_pulls_ff_only(monkeypatch):
    calls = {}
    class Proc:
        returncode = 0
        stdout = "Already up to date."
        stderr = ""
    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["cwd"] = kw["cwd"]
        return Proc()
    monkeypatch.setattr(update.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(update.subprocess, "run", fake_run)
    ok, out = update.update_source()
    assert ok is True
    assert calls["cmd"] == ["git", "pull", "--ff-only"]
    assert calls["cwd"] == update.PROJECT_ROOT
    assert "Already up to date." in out


# ---------- fetch_latest ----------

def test_fetch_latest_picks_windows_zip_asset(monkeypatch):
    payload = {
        "tag_name": "v0.2.0",
        "assets": [
            {"name": "fungi-v0.2.0-source.zip", "browser_download_url": "https://x/src.zip"},
            {"name": "fungi-v0.2.0-windows-x64.zip", "browser_download_url": "https://x/win.zip"},
        ],
    }
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *_a, **_kw: _Resp(json.dumps(payload).encode()))
    got = update.fetch_latest()
    assert got == {"tag": "v0.2.0", "asset_url": "https://x/win.zip"}


def test_fetch_latest_no_exe_asset_gives_none(monkeypatch):
    payload = {"tag_name": "v0.2.0", "assets": [
        {"name": "fungi-v0.2.0-source.zip", "browser_download_url": "https://x/src.zip"}]}
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *_a, **_kw: _Resp(json.dumps(payload).encode()))
    assert update.fetch_latest()["asset_url"] is None
