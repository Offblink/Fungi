"""Skill system: reusable markdown procedure files, per host, in data/skills/.

Every agent build appends `section()` to its system prompt — a fresh list of
skill names + descriptions read from disk (so a skill saved mid-session is
visible on the next turn). The `skills` tool reads full bodies and saves new
files; the writing-skills meta-skill teaches the format and seeds the
directory on first use.

Security: comm clones (and their spawns) get a readonly tool — autonomous
inter-host agents must not be able to persist prompt changes on this host.
"""

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fungi.agent import BoundTool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PROJECT_ROOT / "data" / "skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY = 32768
MAX_DESC = 200
_GUARD = threading.Lock()

WRITING_SKILLS = """\
---
name: writing-skills
description: How to author a new skill file for this host (format, naming, quality bar, save procedure).
---
# Writing skills

A skill is a reusable step-by-step procedure stored on this host in
`data/skills/<name>.md`. When a task matches a skill, read it first and
follow it instead of improvising.

## When to write one
- The same non-trivial procedure was done (or will be needed) more than once:
  a build/test recipe, a deploy sequence, a debugging loop, a house style.
- Do NOT write skills for one-off tasks or facts already obvious from the
  codebase or docs.

## Format
The `skills` tool writes the frontmatter for you; pass:
- `name`: kebab-case ([a-z0-9-], max 64 chars) — the filename stem.
- `description`: one line stating WHEN to use the skill. This is the only
  thing other agents see in the list, so make it a trigger condition, not
  a summary.
- `body`: markdown instructions. Quality bar:
  - Numbered steps with exact commands, paths, and file names.
  - Pitfalls and known failure modes from real experience.
  - Verification: how to tell the procedure worked.
  - Keep it tight — a skill the agent can follow in one read.

## Updating
Prefer updating an existing skill over creating a near-duplicate: save with
the same `name` to overwrite it.
"""


@dataclass
class Skill:
    name: str
    description: str
    body: str


def _seed() -> None:
    """Create the skills dir and the writing-skills meta-skill on first use."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    meta = SKILLS_DIR / "writing-skills.md"
    if not meta.is_file():
        with _GUARD:
            if not meta.is_file():
                meta.write_text(WRITING_SKILLS, encoding="utf-8")


def _parse(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8-sig")
    name = path.stem
    description = ""
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1 and text[end + 4 : end + 5] in ("", "\n", "\r"):
            for line in text[4:end].splitlines():
                key, _, value = line.partition(":")
                value = value.strip()
                if key.strip() == "name" and value:
                    name = value
                elif key.strip() == "description" and value:
                    description = value
            body = text[end + 4 :]
    if not description:
        for raw in body.splitlines():
            candidate = raw.strip().lstrip("#").strip()
            if candidate:
                description = candidate
                break
    return Skill(name=name, description=description[:MAX_DESC], body=body.strip())


def load_all() -> list[Skill]:
    """All skills on this host, sorted by name; seeds the meta-skill."""
    _seed()
    out: list[Skill] = []
    for path in sorted(SKILLS_DIR.glob("*.md")):
        try:
            out.append(_parse(path))
        except OSError:
            continue
    return out


def get(name: str) -> Skill | None:
    if not NAME_RE.fullmatch(name):
        return None
    path = SKILLS_DIR / f"{name}.md"
    if not path.is_file():
        return None
    try:
        return _parse(path)
    except OSError:
        return None


def save(name: str, description: str, body: str) -> str:
    """Write one skill file; returns an OK/ERROR status line."""
    if not NAME_RE.fullmatch(name):
        return f"ERROR: invalid skill name {name!r} (kebab-case [a-z0-9-], max 64 chars)"
    if not body.strip():
        return "ERROR: body required"
    if len(body) > MAX_BODY:
        return f"ERROR: body too large ({len(body)} > {MAX_BODY} chars)"
    description = " ".join(description.split())[:MAX_DESC]
    _seed()
    path = SKILLS_DIR / f"{name}.md"
    text = f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n"
    with _GUARD:
        path.write_text(text, encoding="utf-8")
    return f"OK: saved {path.name} ({len(body.strip())} chars)"


SECTION_HEAD = "\n\n## Skills\n"


def section() -> str:
    """System-prompt block listing available skills; fresh on every call."""
    all_skills = load_all()
    if not all_skills:
        return ""
    rows = "\n".join(f"- {s.name}: {s.description}" for s in all_skills)
    return (
        SECTION_HEAD + 'Reusable procedures ("skills") live on this host. When the current task '
        "matches one, read it with the `skills` tool and follow it. To capture a "
        "repeated procedure as a new skill, read `writing-skills` first.\n\n" + rows
    )


def skill_tool(args: dict, readonly: bool = False) -> str:
    action = str(args.get("action") or "")
    if action == "list":
        all_skills = load_all()
        if not all_skills:
            return "(no skills yet)"
        return "\n".join(f"- {s.name}: {s.description}" for s in all_skills)
    name = str(args.get("name") or "").strip()
    if action == "read":
        if not name:
            return "ERROR: name required"
        skill = get(name)
        if skill is None:
            return f"ERROR: no skill named {name!r}"
        return f"# {skill.name}: {skill.description}\n\n{skill.body}"
    if action == "save":
        if readonly:
            return "ERROR: this agent may read skills but not save them"
        return save(name, str(args.get("description") or ""), str(args.get("body") or ""))
    return "ERROR: action must be list|read|save"


SKILLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills",
        "description": (
            "Reusable skill files stored on this host (data/skills/). "
            "list: enumerate skills; read: load one skill's full body; "
            "save: create or update a skill (see the writing-skills skill for the format)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "read", "save"]},
                "name": {
                    "type": "string",
                    "description": "Skill name (kebab-case, the filename stem). Required for read/save.",
                },
                "description": {
                    "type": "string",
                    "description": "For save: one-line trigger condition shown in the skill list.",
                },
                "body": {
                    "type": "string",
                    "description": "For save: full markdown body (frontmatter is added automatically).",
                },
            },
            "required": ["action"],
        },
    },
}


def bound(readonly: bool = False) -> "dict[str, BoundTool]":
    """The `skills` tool as an agent-bound tool; readonly for autonomous clones."""
    from fungi.agent import BoundTool  # noqa: PLC0415 (deferred: agent imports skills)

    return {
        "skills": BoundTool(
            schema=SKILLS_SCHEMA, fn=lambda args, _ro=readonly: skill_tool(args, _ro)
        )
    }
