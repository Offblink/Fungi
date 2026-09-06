"""Video tool: subprocess wiring against a fake VidSense checkout (no torch)."""

import base64
import json
import textwrap

import pytest

from fungi.agent import _tool_content
from fungi.config import Config
from fungi.tools import dispatch, tool_defs
from fungi.tools import video as video_mod
from fungi.tools.files import ImageRead
from fungi.tools.video import tool_video

FAKE_CARD = {
    "video_id": "clip",
    "duration": 12.0,
    "fps_sampled": 1.0,
    "scenes": [{"start": 0.0, "end": 12.0}],
    "keyframes": [
        {"id": 0, "t": 2.0, "segment_id": 0, "caption": ""},
        {"id": 1, "t": 9.0, "segment_id": 1, "caption": ""},
    ],
    "segments": [
        {"id": 0, "start": 0.0, "end": 5.0, "text": "hello world", "speaker": None,
         "words": [], "frame_id": 2, "scene_index": 0},
        {"id": 1, "start": 5.0, "end": 12.0, "text": "goodbye", "speaker": "host",
         "words": [], "frame_id": 9, "scene_index": 0},
    ],
    "embeddings": [],
}

FAKE_CLI = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    args = sys.argv[1:]
    if "--no-api" not in args:
        print("unexpected args", file=sys.stderr); raise SystemExit(1)
    video = Path([a for a in args if not a.startswith("-")][0])
    jdir = Path("output/json"); jdir.mkdir(parents=True, exist_ok=True)
    (jdir / f"{video.stem}_eventcard.json").write_text(json.dumps(CARD), encoding="utf-8")
    print("ok")
""")

# every component _video_ready() checks, all present
_ALL_READY = {
    "ffmpeg": True,
    "huggingface_hub": True,
    "torch": True,
    "transformers": True,
    "faster_whisper": True,
    "opencv": True,
    "CLIP": True,
    "whisper": True,
}


@pytest.fixture()
def vidsense_env(tmp_path, monkeypatch):
    root = tmp_path / "VidSense"
    (root / "vidsense").mkdir(parents=True)
    (root / "vidsense" / "__init__.py").write_text("", encoding="utf-8")
    (root / "vidsense" / "cli.py").write_text(
        "CARD = " + repr(FAKE_CARD) + "\n" + FAKE_CLI, encoding="utf-8"
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00fake mp4")
    monkeypatch.setattr(
        "fungi.tools.video.load_config",
        lambda: Config(api_key="k", endpoint="e", model="m", vidsense_dir=str(root)),
    )
    # these tests fake a working VidSense checkout; pretend the whole runtime
    # (libs + ffmpeg + HF weights) is in place (dedicated tests below cover gaps)
    monkeypatch.setattr(
        "fungi.tools.video._video_ready",
        lambda: dict(_ALL_READY),
    )

    def fake_extract(video_path, timestamps, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, _t in enumerate(timestamps):
            dest = out_dir / f"kf{i:02d}.jpg"
            dest.write_bytes(b"fakejpeg%d" % i)
            paths.append(dest)
        return paths

    monkeypatch.setattr("fungi.tools.video._extract_keyframes", fake_extract)
    return root, video


def test_video_returns_transcript_plus_keyframe_pixels(vidsense_env):
    _root, video = vidsense_env
    out = tool_video(str(video))
    assert isinstance(out, ImageRead)
    assert "[    0.0-    5.0] hello world" in out
    assert "[host] goodbye" in out
    assert len(out.data_urls) == 2
    assert base64.b64decode(out.data_urls[0].partition("base64,")[2]) == b"fakejpeg0"
    assert out.data_url == out.data_urls[0]


def test_video_multi_image_tool_content_upgrade(vidsense_env):
    _root, video = vidsense_env
    out = tool_video(str(video))
    parts = _tool_content(out)
    assert parts[0] == {"type": "text", "text": str(out)}
    assert [p["type"] for p in parts[1:]] == ["image_url", "image_url"]
    assert _tool_content("plain text") == "plain text"


def test_video_not_configured_reports_setup_hint(tmp_path, monkeypatch):
    # force the auto-discovery to find nothing (it would otherwise locate the
    # real checkout via the well-known sibling layout)
    monkeypatch.setattr("fungi.tools.video._vidsense_root", lambda: None)
    out = tool_video(str(tmp_path / "x.mp4"))
    assert out.startswith("ERROR: VidSense not found")


def test_video_autodiscovers_sibling_checkout(tmp_path, monkeypatch):
    """Dev layout: <root>/Harness/Fungi -> <root>/Skill/VidSense, no config."""
    root = tmp_path / "Skill" / "VidSense"
    (root / "vidsense").mkdir(parents=True)
    (root / "vidsense" / "__init__.py").write_text("", encoding="utf-8")
    (root / "vidsense" / "cli.py").write_text(
        "CARD = " + repr(FAKE_CARD) + "\n" + FAKE_CLI, encoding="utf-8"
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00fake mp4")
    monkeypatch.setattr("fungi.tools.video.PROJECT_ROOT", tmp_path / "Harness" / "Fungi")
    monkeypatch.setattr(
        "fungi.tools.video.load_config",
        lambda: Config(api_key="k", endpoint="e", model="m"),  # no vidsense_dir
    )

    def fake_extract(_video, timestamps, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(len(timestamps)):
            dest = out_dir / f"kf{i:02d}.jpg"
            dest.write_bytes(b"fakejpeg%d" % i)
            paths.append(dest)
        return paths

    monkeypatch.setattr("fungi.tools.video._extract_keyframes", fake_extract)
    out = tool_video(str(video))
    assert isinstance(out, ImageRead) and "hello world" in out


def test_video_missing_file(vidsense_env):
    root, _video = vidsense_env
    out = tool_video(str(root / "nope.mp4"))
    assert out.startswith("ERROR: File not found")


def test_video_surfaces_vidsense_stderr(vidsense_env):
    root, video = vidsense_env
    # a fake CLI that always fails: stderr tail must reach the agent
    (root / "vidsense" / "cli.py").write_text(
        'raise SystemExit("boom: no ffmpeg")', encoding="utf-8"
    )
    out = tool_video(str(video))
    assert out.startswith("ERROR: VidSense failed")
    assert "boom" in out


def test_video_cjk_path_runs_on_ascii_copy(vidsense_env, tmp_path, monkeypatch):
    """ffmpeg fails to decode inputs whose path contains CJK characters on
    this box: the tool must hand the subprocess an ASCII-named copy while the
    result still names the original file."""
    import fungi.tools.video as vt

    _root, video = vidsense_env
    cjk = tmp_path / "中文视频.mp4"
    cjk.write_bytes(video.read_bytes())
    argvs = []
    real_run = vt.subprocess.run

    def spy(argv, **kw):
        argvs.append(argv)
        return real_run(argv, **kw)

    monkeypatch.setattr(vt.subprocess, "run", spy)
    out = tool_video(str(cjk))
    assert isinstance(out, ImageRead)
    assert "[    0.0-    5.0] hello world" in out
    assert "中文视频" in str(out)  # the user-facing name survives the copy
    assert len(argvs) == 1
    video_arg = argvs[0][3]
    assert video_arg.isascii() and video_arg.endswith(".mp4")
    assert str(cjk) not in video_arg


def test_video_registered_and_dispatchable():
    names = {d["function"]["name"] for d in tool_defs()}
    assert "video" in names
    assert dispatch("video", {}).startswith("ERROR: Missing required argument")


def test_video_missing_model_refuses_and_points_to_downloader(
    vidsense_env, monkeypatch
):
    """The tool never downloads on demand: missing weights -> guidance ERROR."""
    _root, video = vidsense_env
    ready = dict(_ALL_READY) | {"whisper": False}
    monkeypatch.setattr("fungi.tools.video._video_ready", lambda: ready)
    out = tool_video(str(video))
    assert out.startswith("ERROR: video not ready")
    assert "download_video_models.py" in out and "whisper" in out


def test_video_missing_non_healable_runtime_reports_manual_install(
    vidsense_env, monkeypatch
):
    """torch/ffmpeg etc. can't be auto-healed: point at VidSense's deps."""
    _root, video = vidsense_env
    ready = dict(_ALL_READY) | {"torch": False, "ffmpeg": False}
    monkeypatch.setattr("fungi.tools.video._video_ready", lambda: ready)
    out = tool_video(str(video))
    assert out.startswith("ERROR: VidSense runtime components missing")
    assert "torch" in out and "ffmpeg" in out and "download_video_models" not in out


def test_model_cached_reads_hf_snapshot_layout(tmp_path, monkeypatch):
    """Cache check: env-driven root, models--*--* snapshot dirs, size floor."""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    monkeypatch.setattr(video_mod, "_MODEL_MIN_BYTES", 8)
    slug = "models--openai--clip-vit-base-patch32"
    snap = tmp_path / slug / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    assert not video_mod._model_cached("openai/clip-vit-base-patch32", ("model.safetensors",))
    (snap / "model.safetensors").write_bytes(b"x" * 16)
    assert video_mod._model_cached("openai/clip-vit-base-patch32", ("model.safetensors",))
    # below the size floor (truncated download) -> treated as missing
    (snap / "model.safetensors").write_bytes(b"x" * 4)
    assert not video_mod._model_cached("openai/clip-vit-base-patch32", ("model.safetensors",))
    # absent cache root -> missing, never raises
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "nope"))
    assert not video_mod._model_cached("openai/clip-vit-base-patch32", ("model.safetensors",))
