"""Immediate to-dos the message courier answers from, in data/todos.json.

One JSON object mapping YYYY-MM-DD to a list of item strings — the GUI
calendar is the writer, the courier prompt is the reader. Unlike the long-term
courier_memory (open-ended context), these are dated commitments: the prompt
injects overdue items plus a rolling window of upcoming days so the courier
can answer "he's busy then" without anyone updating it by hand. data/ is
gitignored; the calendar never leaves the host.
"""

import datetime as _dt
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from fungi.config import PROJECT_ROOT

if TYPE_CHECKING:  # the import is deferred below (agent imports todos)
    from fungi.agent import BoundTool

TODOS_PATH = PROJECT_ROOT / "data" / "todos.json"
_GUARD = threading.Lock()

# Injected wherever the `todo` tool is mounted (L1, local clone, comm courier):
# the store is the *user's* calendar, and that is not obvious from the schema
# alone (2026-09-10 real-machine finding: the courier removed the host's 09-11
# entry and re-added a reworded copy of it -- a silent cancellation to the user,
# whose actual wish was one more item on the same day).
RULES = """## The host user's calendar (the `todo` tool)

The same store the GUI calendar page and the message courier read. It is the
user's own list, not your scratchpad.

- Entries stay until the user asks for them to go: never tidy the list up, never
  clear a day, never drop an entry because it looks stale or already handled.
- New detail on an existing plan is a NEW item (`add`); deleting the entry and
  re-adding it with different wording is a cancellation as far as the user and
  the message courier are concerned.
- `remove` takes the exact item text, and only once there is a reason to believe
  the user wants that entry gone -- someone merely mentioning a date is not one.
- Record dated commitments as they settle, the user's or a peer's (a meeting, a
  rendezvous point, an errand), and put the clock time in the item when the plan
  has one.
"""


def load(path: Path | None = None) -> dict[str, list[str]]:
    """The whole map, normalized: date -> non-empty item list."""
    source = path if path is not None else TODOS_PATH
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, items in data.items():
        if isinstance(items, list):
            clean = [str(i) for i in items if str(i).strip()]
            if clean:
                out[str(key)] = clean
    return out


def set_day(date: str, items: list[str], path: Path | None = None) -> None:
    """Replace one day's items (empty/blank list removes the day) and persist."""
    with _GUARD:
        source = path if path is not None else TODOS_PATH
        data: dict = {}
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = {}
        clean = [str(i) for i in items if str(i).strip()]
        if clean:
            data[date] = clean
        else:
            data.pop(date, None)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def upcoming(days: int = 21, today: _dt.date | None = None) -> list[tuple[str, list[str]]]:
    """Overdue first, then today, then the next `days` days — only days that
    have items. The courier prompt consumes this directly."""
    ref = today or _dt.date.today()
    all_items = load()
    out: list[tuple[str, list[str]]] = []
    start = ref - _dt.timedelta(days=30)  # overdue: a month's tail is plenty
    for i in range(days + 30 + 1):
        day = start + _dt.timedelta(days=i)
        key = day.isoformat()
        if key in all_items:
            out.append((key, all_items[key]))
    return out


TODO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": (
            "The host user's shared calendar to-dos (data/todos.json) — the same "
            "list the GUI calendar shows and the message courier answers from. "
            "action 'add' appends one item to a date and keeps everything already "
            "there; 'list' shows upcoming days that have items; 'remove' deletes "
            "one item by its exact text. The entries belong to the user and stay "
            "until they ask for them to be gone: record new detail as a new item "
            "instead of rewriting an old one, and never tidy the list up. Use "
            "'add' when a dated commitment settles (meetings, errands, reminders, "
            "rendezvous points) so the courier can act on it later."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "remove"]},
                "date": {
                    "type": "string",
                    "description": "For add/remove: YYYY-MM-DD.",
                },
                "text": {
                    "type": "string",
                    "description": "For add: the item, with its clock time when the plan has one. For remove: the exact item text (required).",
                },
            },
            "required": ["action"],
        },
    },
}


def todo_tool(args: dict) -> str:
    """Agent entry to the shared calendar: same store the GUI and courier use.
    BoundTool convention: the whole argument dict arrives as one positional."""
    action = str(args.get("action") or "")
    date = str(args.get("date") or "")
    text = str(args.get("text") or "")
    if action == "add":
        if not _valid_date(date) or not text.strip():
            return "error: 'add' needs date (YYYY-MM-DD) and text"
        item = text.strip()
        items = load().get(date, [])
        if item in items:  # same item twice: the user's list, not a log
            return f"already on {date}: {item}"
        set_day(date, [*items, item])
        return f"added on {date}: {item}"
    if action == "remove":
        if not _valid_date(date):
            return "error: 'remove' needs date (YYYY-MM-DD)"
        if not text.strip():
            # A bare date used to wipe the whole day. One entry at a time keeps
            # an accidental clear impossible and leaves the GUI as the only
            # whole-day editor (2026-09-10 real-machine finding).
            return (
                "error: 'remove' needs text (the exact item). Entries belong to the"
                " user: ask before deleting one, and never clear a day on your own."
            )
        items = load().get(date, [])
        if not items:
            return f"no items on {date}"
        remaining = [i for i in items if i != text.strip()]
        if len(remaining) == len(items):
            return f"no such item on {date}"
        set_day(date, remaining)
        return f"removed from {date}"
    if action == "list":
        entries = upcoming()
        if not entries:
            return "(no upcoming to-dos)"
        return "\n".join(f"{d}: {'; '.join(items)}" for d, items in entries)
    return "error: unknown action (add/list/remove)"


def _valid_date(s: str) -> bool:
    try:
        _dt.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def bound() -> "dict[str, BoundTool]":
    """The `todo` tool as an agent-bound tool (L1 + comm clones)."""
    from fungi.agent import BoundTool  # noqa: PLC0415 (deferred: agent imports todos)

    return {"todo": BoundTool(schema=TODO_SCHEMA, fn=todo_tool)}
