"""fungi/config.py: where the version lives, and the two roots it resolves."""

import tomllib
from pathlib import Path

from fungi import config


def test_local_version_reads_pyproject():
    # reads the real pyproject.toml; compare against the file, never a pinned
    # version (that broke the suite on every release bump)
    with Path("pyproject.toml").open("rb") as fh:
        expected = tomllib.load(fh)["project"]["version"]
    assert config.local_version() == expected


def test_local_version_frozen_uses_resource_root(monkeypatch, tmp_path):
    bundled = tmp_path / "pyproject.toml"
    bundled.write_text('[project]\nname = "fungi"\nversion = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(config, "RESOURCE_ROOT", bundled.parent)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "nowhere")
    assert config.local_version() == "9.9.9"
