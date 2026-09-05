"""Skill system: reusable procedure packages, per host, in data/skills/.

A skill is a directory: SKILL.md (required, the procedure document) plus any
companion files such as scripts (optional, shipped and referenced by the
doc). Legacy flat `data/skills/<name>.md` files from earlier versions are
still read; a directory with the same name shadows the flat file.

Every agent build appends `section()` to its system prompt — a fresh list of
skill names + descriptions read from disk (so a skill saved mid-session is
visible on the next turn). The `skills` tool reads docs and companion files
and saves new skills; the writing-skills meta-skill teaches the format and
seeds the directory on first use.

Security: comm clones (and their spawns) get a readonly tool — autonomous
inter-host agents must not be able to persist prompt changes on this host.
"""

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fungi.config import PROJECT_ROOT

if TYPE_CHECKING:
    from fungi.agent import BoundTool

SKILLS_DIR = PROJECT_ROOT / "data" / "skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY = 32768
MAX_DESC = 200
_GUARD = threading.Lock()
WRITING_SKILLS = """\
---
name: writing-skills
description: How to author a new skill for this host (layout, naming, quality bar, save procedure).
---
# Writing skills

A skill is a reusable step-by-step procedure stored on this host in
`data/skills/<name>/SKILL.md`. When a task matches a skill, read it first and
follow it instead of improvising.

## When to write one
- The same non-trivial procedure was done (or will be needed) more than once:
  a build/test recipe, a deploy sequence, a debugging loop, a house style.
- Do NOT write skills for one-off tasks or facts already obvious from the
  codebase or docs.

## Layout
A skill is a directory:
- `SKILL.md` (required): frontmatter + markdown body — the procedure itself.
- Companion files (optional): scripts or data shipped next to the doc, e.g.
  `scripts/check.py`. Reference them from SKILL.md by relative path. Read or
  run them directly by absolute path, or via the `skills` tool's `path`
  argument when direct file access is not available.

Legacy flat `data/skills/<name>.md` files still read fine; a directory with
the same name wins.

## Format
The `skills` tool writes the frontmatter for you; pass:
- `name`: kebab-case ([a-z0-9-], max 64 chars) — becomes the directory name.
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
    meta_dir = SKILLS_DIR / "writing-skills"
    doc = meta_dir / "SKILL.md"
    legacy = SKILLS_DIR / "writing-skills.md"  # pre-directory auto-generated seed
    if not doc.is_file():
        with _GUARD:
            if not doc.is_file():
                meta_dir.mkdir(parents=True, exist_ok=True)
                doc.write_text(WRITING_SKILLS, encoding="utf-8")
                if legacy.is_file():
                    legacy.unlink()  # superseded by the directory version


def _parse(path: Path, name: str | None = None) -> Skill:
    text = path.read_text(encoding="utf-8-sig")
    name = name or path.stem
    description = ""
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1 and text[end + 4 : end + 5] in ("", "\n", "\r"):
            for line in text[4:end].splitlines():
                key, _, value = line.partition(":")
                value = value.strip()
                if key.strip() == "description" and value:
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
    """All skills on this host, sorted by name; seeds the meta-skill.

    Directory skills (<name>/SKILL.md) come first; a legacy flat <name>.md
    whose name is taken by a directory is shadowed, not duplicated."""
    _seed()
    out: list[Skill] = []
    seen: set[str] = set()
    for entry in sorted(SKILLS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        doc = entry / "SKILL.md"
        if not doc.is_file():
            continue
        try:
            out.append(_parse(doc, entry.name))
            seen.add(entry.name)
        except OSError:
            continue
    for path in sorted(SKILLS_DIR.glob("*.md")):
        if path.stem in seen:
            continue
        try:
            out.append(_parse(path))
        except OSError:
            continue
    return out


def get(name: str) -> Skill | None:
    if not NAME_RE.fullmatch(name):
        return None
    doc = SKILLS_DIR / name / "SKILL.md"
    legacy = SKILLS_DIR / f"{name}.md"
    path = doc if doc.is_file() else (legacy if legacy.is_file() else None)
    if path is None:
        return None
    try:
        return _parse(path, name if path == doc else None)
    except OSError:
        return None


def save(name: str, description: str, body: str) -> str:
    """Write one skill as <name>/SKILL.md; returns an OK/ERROR status line."""
    if not NAME_RE.fullmatch(name):
        return f"ERROR: invalid skill name {name!r} (kebab-case [a-z0-9-], max 64 chars)"
    if not body.strip():
        return "ERROR: body required"
    if len(body) > MAX_BODY:
        return f"ERROR: body too large ({len(body)} > {MAX_BODY} chars)"
    description = " ".join(description.split())[:MAX_DESC]
    _seed()
    skill_dir = SKILLS_DIR / name
    doc = skill_dir / "SKILL.md"
    text = f"---\ndescription: {description}\n---\n\n{body.strip()}\n"
    with _GUARD:
        skill_dir.mkdir(parents=True, exist_ok=True)
        doc.write_text(text, encoding="utf-8")
    legacy = SKILLS_DIR / f"{name}.md"
    if legacy.is_file():
        with _GUARD:
            legacy.unlink()  # superseded by the directory version
    return f"OK: saved {name}/SKILL.md ({len(body.strip())} chars)"


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


def _companion_files(skill_dir: Path) -> list[Path]:
    """Every file in a directory skill except SKILL.md itself."""
    root = skill_dir.resolve()
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p != root / "SKILL.md":
            out.append(p)
    return out


def _read_companion(name: str, rel: str) -> str:
    """Read one companion file inside a directory skill (path-confined)."""
    if not NAME_RE.fullmatch(name):
        return f"ERROR: invalid skill name {name!r}"
    root = (SKILLS_DIR / name).resolve()
    if not (root / "SKILL.md").is_file():
        return f"ERROR: skill {name!r} has no directory (legacy flat file)"
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        return "ERROR: path escapes the skill directory"
    if not target.is_file():
        return f"ERROR: no file {rel!r} in skill {name!r}"
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return f"ERROR: unreadable: {exc}"
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY] + "\n…(truncated)"
    return f"# {name}/{rel}\n\n{text}"


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
        rel = str(args.get("path") or "").strip()
        if rel:
            return _read_companion(name, rel)
        skill = get(name)
        if skill is None:
            return f"ERROR: no skill named {name!r}"
        out = f"# {skill.name}: {skill.description}\n\n{skill.body}"
        files = _companion_files(SKILLS_DIR / name)
        if files:
            listed = "\n".join(
                f"- {q.relative_to((SKILLS_DIR / name).resolve()).as_posix()}"
                f" ({q.stat().st_size} bytes)"
                for q in files
            )
            out += f"\n\nCompanion files — read with action=read plus path:\n{listed}"
        return out
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
            "Reusable skill packages stored on this host (data/skills/<name>/SKILL.md"
            " plus optional companion scripts). list: enumerate skills; read: load one"
            " skill's doc (or a companion file via path); save: create or update a"
            " skill (see the writing-skills skill for the format)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "read", "save"]},
                "name": {
                    "type": "string",
                    "description": "Skill name (kebab-case, the directory name). Required for read/save.",
                },
                "path": {
                    "type": "string",
                    "description": "For read: relative companion-file path inside the skill directory (e.g. scripts/check.py).",
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
