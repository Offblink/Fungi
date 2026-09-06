"""输入解析: 本地文件 -> 音频wav + 帧列表 + 时长元数据。URL 支持为阶段2。"""
import subprocess
from pathlib import Path
import cv2
from . import config


def probe_duration(video) -> float:
    """ffprobe 读取时长(秒)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def extract_audio(video, out_wav) -> Path:
    """ffmpeg 抽单声道 16k wav."""
    out_wav = Path(out_wav)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video),
         "-ac", "1", "-ar", str(config.AUDIO_SR), "-vn", str(out_wav)],
        check=True, capture_output=True)
    return out_wav


def extract_frames(video, fps: float = config.FPS_SAMPLED):
    """用 grab/retrieve 高效抽帧(跳过未选中帧的解码) -> [(t秒, rgb np.ndarray)]."""
    cap = cv2.VideoCapture(str(video))
    vfps = cap.get(cv2.CAP_PROP_FPS) or 1.0
    step = max(1, int(round(vfps / fps)))
    out = []
    i = 0
    while True:
        if not cap.grab():
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                out.append((i / vfps, cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        i += 1
    cap.release()
    return out


def ingest(video) -> dict:
    """本地视频 -> {'video', 'audio', 'frames', 'duration'}."""
    video = Path(video)
    if not video.exists():
        raise FileNotFoundError(f"video not found: {video}")
    config.ensure_dirs()
    wav = config.AUDIO_DIR / (video.stem + ".wav")
    extract_audio(video, wav)
    frames = extract_frames(video)
    return {"video": video, "audio": wav, "frames": frames, "duration": probe_duration(video)}
