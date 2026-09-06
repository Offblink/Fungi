"""Video understanding via an optional VidSense checkout (subprocess, isolated).

VidSense runs its LOCAL pipeline only (--no-api): ffmpeg/ffprobe extract,
faster-whisper transcript, CLIP scenes/MMR keyframes -> event card JSON +
keyframe JPEGs. The final understanding is done by Fungi's own model: the
tool result carries the timestamped transcript plus keyframe pixels (via
ImageRead), so a vision LLM sees both axes. No second API key, no DeepSeek
round-trip.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from ..config import load_config
from .files import ImageRead

_VIDEO_TIMEOUT_S = 1800.0  # CPU laptops transcribe+CLIP a long video slowly
_MAX_TEXT_CHARS = 20000
_MAX_KEYFRAMES = 12  # mirror vidsense API_MAX_KEYFRAMES; tokens are not free

_NOT_CONFIGURED = (
    'ERROR: VidSense is not configured — set "vidsense_dir" in config.json to a '
    "VidSense checkout (needs ffmpeg on PATH + torch, transformers, "
    "faster-whisper, opencv-python installed in Fungi's Python)."
)


def tool_video(path: str) -> str:
    """Understand a local video: transcript + scenes + attached keyframes."""
    cfg = load_config()
    root = Path(cfg.vidsense_dir) if cfg.vidsense_dir else None
    if not root or not root.is_dir():
        return _NOT_CONFIGURED
    video = Path(path)
    if not video.is_file():
        return f"ERROR: File not found: {video}"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "vidsense.cli", str(video), "--no-api", "--save-frames"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VIDEO_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: VidSense timed out after {_VIDEO_TIMEOUT_S:.0f}s"
    except OSError as exc:
        return f"ERROR: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        return f"ERROR: VidSense failed:\n{tail}"

    card_path = root / "output" / "json" / f"{video.stem}_eventcard.json"
    try:
        card = json.loads(card_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"ERROR: VidSense ran but its event card is unreadable ({exc})"
    return _render(card, root / "output" / "frames" / video.stem, video.name)


def _render(card: dict, keyframe_dir: Path, name: str) -> str:
    """Event card -> compact grounding text + keyframe pixels attached."""
    lines = [
        f"VIDEO: {name} — {card.get('duration', 0):.0f}s, "
        f"{len(card.get('scenes', []))} shots, {len(card.get('keyframes', []))} "
        f"keyframes (attached), {len(card.get('segments', []))} transcript segments. "
        "Timestamps let you cite exact moments."
    ]
    for seg in card.get("segments", []):
        text = " ".join(str(seg.get("text", "")).split())
        lines.append(
            f"[{seg.get('start', 0):7.1f}-{seg.get('end', 0):7.1f}] "
            f"{('[' + seg['speaker'] + '] ') if seg.get('speaker') else ''}{text}"
        )
    summary = "\n".join(lines)
    if len(summary) > _MAX_TEXT_CHARS:
        half = _MAX_TEXT_CHARS // 2
        summary = (
            summary[:half] + "\n... [transcript truncated] ...\n" + summary[-half:]
        )

    urls: list[str] = []
    for kf in card.get("keyframes", [])[:_MAX_KEYFRAMES]:
        matches = sorted(keyframe_dir.glob(f"kf{kf.get('id', 0):02d}_*.jpg"))
        if not matches:
            continue
        try:
            raw = matches[0].read_bytes()
        except OSError:
            continue
        urls.append("data:image/jpeg;base64," + base64.b64encode(raw).decode())

    if not urls:
        return summary + "\n(no keyframe images available — judge from the transcript)"
    return ImageRead(summary, urls)
