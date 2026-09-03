"""Session storage: one JSON file per session, schema compatible with the PowerShell original.

File shape: {id, title, created, updated, messages: [{role, content, ...}]}
"""

import contextlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = PROJECT_ROOT / "sessions"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def get_session_title(messages: list[dict[str, Any]]) -> str:
    """Title = first user message, collapsed whitespace, capped at 50 chars."""
    for msg in messages:
        if msg.get("role") == "user":
            text = " ".join(str(msg.get("content", "")).split())
            return text[:47] + "..." if len(text) > 50 else text
    return "(empty)"


def ensure_dir() -> None:
    SESSIONS_DIR.mkdir(exist_ok=True)


def list_sessions() -> list[dict[str, Any]]:
    """Metadata for all sessions, newest first. Broken files are skipped."""
    ensure_dir()
    result = []
    for path in sorted(
        SESSIONS_DIR.glob("*.json"),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        result.append(
            {
                "id": data.get("id"),
                "title": data.get("title"),
                "created": data.get("created"),
                "updated": data.get("updated"),
                "msgCount": len(data.get("messages", [])),
            }
        )
    return result


def save_session(
    session_id: str,
    title: str,
    messages: list[dict[str, Any]],
    subagents: list[dict[str, Any]] | None = None,
    asks: list[dict[str, Any]] | None = None,
) -> None:
    ensure_dir()
    path = SESSIONS_DIR / f"{session_id}.json"
    created = _now()
    if path.is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            created = json.loads(path.read_text(encoding="utf-8-sig")).get("created", created)
    payload = {
        "id": session_id,
        "title": title,
        "created": created,
        "updated": _now(),
        "messages": messages,
        "subagents": subagents or [],
        "asks": asks or [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(session_id: str) -> dict[str, Any] | None:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def delete_session(session_id: str) -> None:
    path = SESSIONS_DIR / f"{session_id}.json"
    path.unlink(missing_ok=True)
