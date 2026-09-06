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
import importlib.util
import json
import os
import shutil
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

# HF models the local pipeline needs. The tool never downloads on demand;
# scripts/download_video_models.py pre-seeds the cache (via hf-mirror.com).
_VIDEO_MODELS: dict[str, tuple[str, tuple[str, ...]]] = {
    # label -> (repo_id, weight-file candidates; first fully-present wins)
    "CLIP": ("openai/clip-vit-base-patch32", ("model.safetensors", "pytorch_model.bin")),
    "whisper": ("Systran/faster-whisper-small", ("model.bin",)),
}
# Snapshot files are pointers into blobs/; anything materially below the real
# weight size means a truncated/interrupted download.
_MODEL_MIN_BYTES = 50 * 1024 * 1024


def _hub_cache_root() -> Path:
    """huggingface_hub's cache resolution, without importing it."""
    hub = os.environ.get("HF_HUB_CACHE")
    if hub:
        return Path(hub)
    hf_home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
    return Path(hf_home) / "hub"


def _model_cached(repo_id: str, filenames: tuple[str, ...]) -> bool:
    """True when one candidate weight file is fully present in the HF hub
    cache (models--<org>--<name>/snapshots/<rev>/<file>, >= 50 MB)."""
    snapshots = _hub_cache_root() / ("models--" + repo_id.replace("/", "--")) / "snapshots"
    try:
        entries = list(snapshots.iterdir())
    except OSError:
        return False
    for snap in entries:
        for name in filenames:
            try:
                if (snap / name).stat().st_size >= _MODEL_MIN_BYTES:
                    return True
            except OSError:
                continue
    return False


def _models_ready() -> dict[str, bool]:
    return {
        label: _model_cached(repo, files) for label, (repo, files) in _VIDEO_MODELS.items()
    }


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # broken/partial installs
        return False


# Missing items the GUI download button can heal on its own (pip + model script).
_HEALABLE = frozenset({"huggingface_hub", "CLIP", "whisper"})


def _video_ready() -> dict[str, bool]:
    """Single source of truth for everything the local VidSense pipeline needs:
    Python libs, ffmpeg/ffprobe, and the HF model caches. Drives the tool gate
    AND the GUI status line, so the two can never disagree."""
    ready = {
        "ffmpeg": shutil.which("ffmpeg") is not None
        and shutil.which("ffprobe") is not None,
        "huggingface_hub": _module_available("huggingface_hub"),
        "torch": _module_available("torch"),
        "transformers": _module_available("transformers"),
        "faster_whisper": _module_available("faster_whisper"),
        "opencv": _module_available("cv2"),
    }
    ready.update(_models_ready())  # CLIP, whisper
    return ready


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


def _demo_video() -> Path:
    """Built-in 5s test clip (lavfi-generated countdown-style pattern + tone,
    cached under data/): gives `video` something concrete to run so verifying
    the whole pipeline is just one call with path "demo". ffmpeg is already a
    VidSense prerequisite, so generation adds no new dependency."""
    dest = PROJECT_ROOT / "data" / "demo_video.mp4"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg not found on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(dest),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return dest


def tool_video(path: str) -> str:
    """Understand a local video: transcript + scenes + attached keyframes."""
    root = _vidsense_root()
    if not root:
        return _NOT_CONFIGURED
    missing = [name for name, ok in _video_ready().items() if not ok]
    if missing:
        if set(missing) <= _HEALABLE:
            steps = []
            if "huggingface_hub" in missing:
                steps.append("`pip install huggingface_hub`")
            steps.append(
                "`python scripts/download_video_models.py` in Fungi's root "
                "(downloads via hf-mirror.com)"
            )
            return (
                "ERROR: video not ready, missing: "
                + ", ".join(missing)
                + ". Run "
                + " and ".join(steps)
                + " - the GUI download button does both automatically; the tool "
                "never downloads on demand."
            )
        return (
            "ERROR: VidSense runtime components missing: "
            + ", ".join(missing)
            + ". Install VidSense's Python deps (torch, transformers, "
            "faster-whisper, opencv-python) and put ffmpeg/ffprobe on PATH."
        )
    if path == "demo":  # built-in self-test clip: one successful call proves
        try:            # the whole pipeline (ffmpeg, VidSense, models, LLM)
            video = _demo_video()
        except (OSError, subprocess.SubprocessError) as exc:
            return f"ERROR: could not generate the built-in demo clip: {exc}"
    else:
        video = Path(path).resolve()  # resolve against Fungi's cwd: the VidSense
        if not video.is_file():       # subprocess runs with cwd=vidsense_dir
            return f"ERROR: File not found: {video}"
    with tempfile.TemporaryDirectory(prefix="fungi-video-") as tmp:
        # ffmpeg on this box fails to decode inputs whose path contains CJK
        # characters (observed on real runs). VidSense's checkout must stay
        # native, so hand BOTH its subprocess and our keyframe pass an
        # ASCII-named copy instead.
        work = video
        if not str(video).isascii():
            work = Path(tmp) / ("video" + (video.suffix.lower() or ".mp4"))
            shutil.copyfile(video, work)
        env = dict(os.environ)
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # GFW: model HEAD checks must not hit huggingface.co
        env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "vidsense.cli", str(work), "--no-api"],
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

        card_path = root / "output" / "json" / f"{work.stem}_eventcard.json"
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"ERROR: VidSense ran but its event card is unreadable ({exc})"

        keyframes = card.get("keyframes", [])[:_MAX_KEYFRAMES]
        frames = _extract_keyframes(work, [k.get("t", 0.0) for k in keyframes], Path(tmp) / "kf")
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
