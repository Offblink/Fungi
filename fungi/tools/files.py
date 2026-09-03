"""File tools: read (with :N / :N-M line selectors), write, edit (unique-match replace)."""

from pathlib import Path

TRUNCATE_READ = 20000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [truncated {len(text) - limit} chars] ...\n{text[-half:]}"


def tool_read(path: str) -> str:
    """Read a file, numbering lines. Supports `path:N` and `path:N-M` selectors."""
    selector: tuple[int, int] | None = None
    base = path
    if ":" in path:
        head, _, tail = path.rpartition(":")
        parts = tail.split("-", maxsplit=1)
        if parts and parts[0].isdigit() and all(p.isdigit() for p in parts):
            start = int(parts[0])
            end = int(parts[1]) if len(parts) == 2 else start
            selector = (start, end)
            base = head

    file = Path(base)
    if not file.is_file():
        return f"ERROR: File not found: {base}"
    try:
        content = file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return f"ERROR: {exc}"

    lines = content.splitlines()
    if selector is not None:
        start, end = selector
        if start < 1:
            return "ERROR: Line numbers start at 1"
        if start > len(lines):
            return f"ERROR: Line {start} past end ({len(lines)} lines)"
        end = min(end, len(lines))
        numbered = [f"{i}:{lines[i - 1]}" for i in range(start, end + 1)]
        return _truncate("\n".join(numbered), TRUNCATE_READ)

    numbered = [f"{i + 1}:{line}" for i, line in enumerate(lines)]
    return _truncate("\n".join(numbered), TRUNCATE_READ)


def tool_write(path: str, content: str) -> str:
    file = Path(path)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        return f"ERROR: {exc}"
    return f"Wrote {file} ({file.stat().st_size} bytes)"


def tool_edit(path: str, old_string: str, new_string: str) -> str:
    file = Path(path)
    if not file.is_file():
        return f"ERROR: File not found: {path}"
    try:
        content = file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return f"ERROR: {exc}"
    count = content.count(old_string)
    if count == 0:
        return f"ERROR: old_string not found in {path}"
    if count > 1:
        return f"ERROR: old_string matches {count} times — must be unique. Include more context."
    file.write_text(content.replace(old_string, new_string, 1), encoding="utf-8", newline="")
    return f"Edited {path} (1 replacement)"
