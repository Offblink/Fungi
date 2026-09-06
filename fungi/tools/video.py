"""Video understanding via an optional VidSense checkout (subprocess, isolated).

VidSense runs its LOCAL pipeline only (--no-api, stock behavior): ffmpeg/ffprobe
extract, faster-whisper transcript, CLIP scenes/MMR keyframes -> event card
JSON. The final understanding is done by Fungi's own model: the tool result
carries the timestamped transcript plus keyframe pixels (via ImageRead), so a
vision LLM sees both axes. No second API key, no DeepSeek round-trip, and no
VidSense-side modifications — keyframes are re-extracted here with ffmpeg,
which VidSense already requires.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..config import PROJECT_ROOT, load_config
from .files import ImageRead

_VIDEO_TIMEOUT_S = 1800.0  # CPU laptops transcribe+CLIP a long video slowly
_MAX_TEXT_CHARS = 20000
_MAX_KEYFRAMES = 12  # mirror vidsense API_MAX_KEYFRAMES; tokens are not free

_NOT_CONFIGURED = (
    "ERROR: VidSense not found — drop the Offblink/VidSense checkout beside Fungi "
    "(<install root>/Skill/VidSense) or set \"vidsense_dir\" in config.json. "
    "Needs ffmpeg/ffprobe on PATH + torch, transformers, faster-whisper, "
    "opencv-python in Fungi's Python."
)


def _vidsense_root() -> Path | None:
    """Explicit config wins; otherwise well-known layouts. The dev checkout
    sits beside Fungi: <root>/Harness/Fungi -> <root>/Skill/VidSense."""
    cfg = load_config()
    candidates = []
    if cfg.vidsense_dir:
        candidates.append(Path(cfg.vidsense_dir))
    candidates += [
        PROJECT_ROOT.parent.parent / "Skill" / "VidSense",
        Path.home() / "Desktop" / "Vibe Coding" / "useful" / "基于LLM" / "Skill" / "VidSense",
    ]
    for c in candidates:
        if (c / "vidsense" / "cli.py").is_file():
            return c
    return None


def tool_video(path: str) -> str:
    """Understand a local video: transcript + scenes + attached keyframes."""
    root = _vidsense_root()
    if not root:
        return _NOT_CONFIGURED
    video = Path(path).resolve()  # resolve against Fungi's cwd: the VidSense
    if not video.is_file():       # subprocess runs with cwd=vidsense_dir
        return f"ERROR: File not found: {video}"

    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # GFW: model HEAD checks must not hit huggingface.co
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "vidsense.cli", str(video), "--no-api"],
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

    keyframes = card.get("keyframes", [])[:_MAX_KEYFRAMES]
    with tempfile.TemporaryDirectory(prefix="fungi-video-") as tmp:
        frames = _extract_keyframes(video, [k.get("t", 0.0) for k in keyframes], Path(tmp))
        return _render(card, frames, video.name)


def _extract_keyframes(video: Path, timestamps: list[float], out_dir: Path) -> list[Path]:
    """Grab one JPEG per keyframe timestamp with ffmpeg (fast seek). VidSense
    keeps frames in memory only, so we re-extract them here — keeps the
    VidSense checkout untouched."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, t in enumerate(timestamps):
        dest = out_dir / f"kf{i:02d}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
                 "-i", str(video), "-frames:v", "1", "-q:v", "3", str(dest)],
                capture_output=True, timeout=120, check=True,
            )
        except (subprocess.SubprocessError, OSError):
            paths.append(None)  # type: ignore[list-item]
        else:
            paths.append(dest)
    return paths


def _render(card: dict, frame_paths: list[Path | None], name: str) -> str:
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
    for p in frame_paths:
        if p is None:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        urls.append("data:image/jpeg;base64," + base64.b64encode(raw).decode())

    if not urls:
        return summary + "\n(no keyframe images available — judge from the transcript)"
    return ImageRead(summary, urls)
