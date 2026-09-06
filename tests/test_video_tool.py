"""Video tool: subprocess wiring against a fake VidSense checkout (no torch)."""

import base64
import json
import textwrap

import pytest

from fungi.agent import _tool_content
from fungi.config import Config
from fungi.tools import dispatch, tool_defs
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
    if "--save-frames" in args:
        fdir = Path("output/frames") / video.stem; fdir.mkdir(parents=True, exist_ok=True)
        for kid, t in ((0, 2.0), (1, 9.0)):
            (fdir / f"kf{kid:02d}_{t:08.2f}.jpg").write_bytes(b"fakejpeg%d" % kid)
    print("ok")
""")


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
    monkeypatch.setattr(
        "fungi.tools.video.load_config",
        lambda: Config(api_key="k", endpoint="e", model="m"),
    )
    assert tool_video(str(tmp_path / "x.mp4")).startswith("ERROR: VidSense is not configured")


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


def test_video_registered_and_dispatchable():
    names = {d["function"]["name"] for d in tool_defs()}
    assert "video" in names
    assert dispatch("video", {}).startswith("ERROR: Missing required argument")
