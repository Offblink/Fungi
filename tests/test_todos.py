"""todos: shared calendar store + agent todo tool + courier prompt injection."""

import datetime as dt

from fungi import todos


def _write(path, data: dict) -> None:
    import json

    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_normalizes(tmp_path):
    p = tmp_path / "todos.json"
    _write(p, {"2026-09-10": ["去上课", "", "  "], "bad": "nope", "2026-09-11": []})
    assert todos.load(p) == {"2026-09-10": ["去上课"]}
    assert todos.load(tmp_path / "missing.json") == {}


def test_set_day_roundtrip(tmp_path):
    p = tmp_path / "todos.json"
    todos.set_day("2026-09-10", ["开会"], path=p)
    todos.set_day("2026-09-10", ["开会", "买菜"], path=p)  # replace, not append
    assert todos.load(p)["2026-09-10"] == ["开会", "买菜"]
    todos.set_day("2026-09-10", [], path=p)  # empty clears the day
    assert todos.load(p) == {}


def test_upcoming_orders_overdue_first(tmp_path, monkeypatch):
    p = tmp_path / "todos.json"
    today = dt.date(2026, 9, 10)
    orig = todos.load
    monkeypatch.setattr(todos, "load", lambda path=None: orig(p))
    _write(p, {
        "2026-09-09": ["yesterday"],
        "2026-09-10": ["today"],
        "2026-09-24": ["later"],
        "2026-08-20": ["recently overdue"],
    })
    got = todos.upcoming(today=today)
    assert [d for d, _ in got] == ["2026-08-20", "2026-09-09", "2026-09-10", "2026-09-24"]


def test_todo_tool_add_list_remove(tmp_path, monkeypatch):
    p = tmp_path / "todos.json"
    monkeypatch.setattr(todos, "TODOS_PATH", p)
    orig = todos.load
    monkeypatch.setattr(todos, "load", lambda path=None: orig(p))
    assert "added" in todos.todo_tool("add", "2026-09-12", "去看牙")
    assert "error" in todos.todo_tool("add", "not-a-date", "x")
    assert "2026-09-12" in todos.todo_tool("list")
    assert "removed" in todos.todo_tool("remove", "2026-09-12", "去看牙")
    assert "no items" in todos.todo_tool("remove", "2026-09-12")
    assert todos.load(p) == {}
