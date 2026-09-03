"""Search tools: glob (files by pattern, newest first) and grep (regex over text files)."""

import re
from pathlib import Path

GLOB_LIMIT = 50
GREP_FILE_LIMIT = 200
GREP_MATCH_LIMIT = 40
TEXT_EXTENSIONS = {
    ".ps1",
    ".cmd",
    ".bat",
    ".txt",
    ".md",
    ".json",
    ".xml",
    ".yml",
    ".yaml",
    ".ini",
    ".cfg",
    ".log",
    ".css",
    ".html",
    ".js",
    ".ts",
    ".py",
    ".rb",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".sql",
    ".sh",
    ".toml",
}


def _human_size(size: int) -> str:
    if size > 1_000_000:
        return f"{size / 1_000_000:.1f}MB"
    if size > 1_000:
        return f"{size / 1_000:.1f}KB"
    return f"{size}B"


def tool_glob(pattern: str, path: str | None = None) -> str:
    base = Path(path or ".")
    if not base.is_dir():
        return f"ERROR: Directory not found: {path}"
    try:
        matches = sorted(
            (p for p in base.rglob(pattern) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:GLOB_LIMIT]
    except OSError as exc:
        return f"ERROR: {exc}"
    if not matches:
        return f"(no files matched {pattern})"
    rows = []
    for p in matches:
        try:
            rel = p.relative_to(Path.cwd())
        except ValueError:
            rel = p
        rows.append(f"{rel}  ({_human_size(p.stat().st_size)})")
    return "\n".join(rows)


def tool_grep(pattern: str, path: str | None = None) -> str:
    target = Path(path or ".")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"ERROR: Invalid regex: {exc}"

    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = [
            p for p in target.rglob("*") if p.suffix.lower() in TEXT_EXTENSIONS and p.is_file()
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        files = files[:GREP_FILE_LIMIT]
    else:
        return f"ERROR: Path not found: {path}"

    rows = []
    for file in files:
        try:
            with file.open(encoding="utf-8-sig", errors="replace") as handle:
                for lineno, line in enumerate(handle, start=1):
                    if regex.search(line):
                        rows.append(f"{file.name}:{lineno}:{line.strip()}")
                        if len(rows) >= GREP_MATCH_LIMIT:
                            return "\n".join(rows)
        except OSError:
            continue
    if not rows:
        return f"(no matches for '{pattern}')"
    return "\n".join(rows)
