"""File tools: read (with :N / :N-M line selectors), write, edit (unique-match replace)."""

import base64
import io
from pathlib import Path

TRUNCATE_READ = 20000

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
IMAGE_MAX_DIM = 1568  # vision sweet spot; bigger just burns tokens
IMAGE_RAW_KEEP = 512 * 1024  # originals up to this ride as-is, no re-encode
IMAGE_MAX_BYTES = 64 * 1024 * 1024  # refuse to buffer absurd files


class ImageRead(str):
    """Read result for an image file: a plain string summary (renders, saves,
    truncates like any tool output) that also carries the pixel data URL, so
    the agent loop can upgrade the tool message to multimodal content."""

    data_url: str

    def __new__(cls, summary: str, data_url: str):
        obj = super().__new__(cls, summary)
        obj.data_url = data_url
        return obj


def _read_image(file: Path) -> str:
    """Images ride along as data URLs. The old text path was worse than
    useless here: errors=\"replace\" mojibake meant the model saw neither the
    bytes nor the picture."""
    try:
        raw = file.read_bytes()
    except OSError as exc:
        return f"ERROR: {exc}"
    if len(raw) > IMAGE_MAX_BYTES:
        return (
            f"ERROR: {file.name} is {len(raw)} bytes — too large to attach "
            f"(cap {IMAGE_MAX_BYTES})"
        )
    url, mime, dims = _image_data_url(file.suffix.lower(), raw)
    if url is None:
        return (
            f"IMAGE: {file.name} ({len(raw)} bytes, extension says {file.suffix}) "
            "— could not be decoded as an image, so no pixels are attached"
        )
    return ImageRead(
        f"IMAGE: {file.name} — attached to this result as {mime}, {dims} "
        "(vision models can see it; describe the content directly)",
        url,
    )


def _image_data_url(ext: str, raw: bytes) -> tuple[str | None, str, str]:
    """Return (data_url, mime, "WxH"). Small originals ride as-is; anything
    bigger is downscaled and re-encoded JPEG (a 2448px phone photo base64s
    into an endpoint-killing payload). The mime comes from the decoded format,
    NOT the extension — files routinely wear the wrong one (.jpg that is a PNG)."""
    try:
        from PIL import Image  # noqa: PLC0415 (optional dependency)
    except ImportError:
        if len(raw) > IMAGE_RAW_KEEP:
            return None, "", ""
        mime = _IMAGE_MIME.get(ext, "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}", mime, "unknown size"
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None, "", ""
    dims = f"{img.width}x{img.height}"
    if len(raw) <= IMAGE_RAW_KEEP and max(img.size) <= IMAGE_MAX_DIM:
        fmt = (img.format or "").lower()
        mime = f"image/{fmt}" if fmt else _IMAGE_MIME.get(ext, "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}", mime, dims
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))  # plain convert("RGB")
        bg.paste(img, mask=img.split()[3])  # composites alpha onto black
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((IMAGE_MAX_DIM, IMAGE_MAX_DIM))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    out = buf.getvalue()
    return (
        "data:image/jpeg;base64," + base64.b64encode(out).decode(),
        "image/jpeg",
        f"{img.width}x{img.height}",
    )


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
    if file.suffix.lower() in IMAGE_EXTS:
        if selector is not None:
            return "ERROR: Images are attached whole — drop the :N line selector"
        return _read_image(file)
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
