"""Tests for base tools (offline: files, shell, search)."""

import json

import fungi.tools.shell as shell_mod
from fungi.tools import BASE_TOOL_NAMES, dispatch, tool_defs
from fungi.tools.files import tool_edit, tool_read, tool_write
from fungi.tools.search import tool_glob, tool_grep
from fungi.tools.shell import tool_bash
from fungi.tools.webtools import tool_web  # noqa: F401 (exercises import wiring)


def test_read_numbered_lines(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("one\ntwo\nthree", encoding="utf-8")
    assert tool_read(str(file)) == "1:one\n2:two\n3:three"


def test_read_line_selector(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("one\ntwo\nthree", encoding="utf-8")
    assert tool_read(f"{file}:2") == "2:two"
    assert tool_read(f"{file}:2-3") == "2:two\n3:three"


def test_read_selector_past_end(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("one", encoding="utf-8")
    assert tool_read(f"{file}:5").startswith("ERROR: Line 5 past end")


def test_read_bom_tolerant(tmp_path):
    file = tmp_path / "bom.txt"
    file.write_bytes("中文".encode("utf-8-sig"))
    assert tool_read(str(file)) == "1:中文"


def test_read_missing():
    assert tool_read("Z:/definitely/not/here.txt").startswith("ERROR: File not found")


def test_write_creates_parents(tmp_path):
    target = tmp_path / "deep" / "dir" / "f.txt"
    result = tool_write(str(target), "hello")
    assert result.startswith("Wrote")
    assert target.read_text(encoding="utf-8") == "hello"


def test_edit_unique(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("alpha beta alpha gamma", encoding="utf-8")
    result = tool_edit(str(file), "alpha gamma", "delta")
    assert result.startswith("Edited")
    assert file.read_text(encoding="utf-8") == "alpha beta delta"


def test_edit_not_found(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("alpha", encoding="utf-8")
    assert tool_edit(str(file), "missing", "x").startswith("ERROR: old_string not found")


def test_edit_not_unique(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("alpha alpha", encoding="utf-8")
    assert "matches 2 times" in tool_edit(str(file), "alpha", "x")


def test_bash_utf8():
    result = tool_bash("echo 中文")
    assert "中文" in result


def test_bash_exit_code():
    result = tool_bash("exit 3")
    assert result.endswith("[exit: 3]")


def test_bash_timeout(monkeypatch):
    monkeypatch.setattr(shell_mod, "BASH_TIMEOUT", 2)
    result = shell_mod.tool_bash("ping -n 10 127.0.0.1 > nul")
    assert result.startswith("ERROR: Timed out")


def test_glob_and_grep(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.py").write_text("target_token = 1\n", encoding="utf-8")
    (tmp_path / "y.md").write_text("has target_token too\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    hits = tool_glob("*.py")
    assert "sub/x.py" in hits.replace("\\", "/")

    matches = tool_grep("target_token")
    lines = matches.splitlines()
    assert len(lines) == 2
    assert any("x.py:1" in line for line in lines)


def test_glob_no_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert tool_glob("*.zzz") == "(no files matched *.zzz)"


def test_grep_invalid_regex():
    assert tool_grep("([bad").startswith("ERROR: Invalid regex")


def test_dispatch_missing_required():
    assert dispatch("read", {}) == "ERROR: Missing required argument: path"


def test_dispatch_unknown_tool():
    assert dispatch("nope", {}) == "ERROR: Unknown tool: nope"


def test_dispatch_filters_extra_kwargs(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("hi", encoding="utf-8")
    assert dispatch("read", {"path": str(file), "bogus": 1}) == "1:hi"


def test_tool_defs_shape():

    defs = tool_defs()
    assert {d["function"]["name"] for d in defs} == set(BASE_TOOL_NAMES)
    for d in defs:
        assert d["type"] == "function"
        json.dumps(d)  # must be JSON-serializable for the API
