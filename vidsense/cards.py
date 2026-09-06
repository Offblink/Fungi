"""Grounding Context: 合并 转写(segments) 与 视觉(frames+CLIP) -> EventCard。"""
import bisect
from .schema import EventCard, Scene, Keyframe
from .frames import scene_cut, mmr_keyframes
from . import config


def _seg_frame_slot(frame_emb, ts, seg):
    """segment 的时间区间 -> 帧索引区间内的内容帧索引(离该区间主题最近)."""
    s = bisect.bisect_left(ts, seg.start)
    e = bisect.bisect_right(ts, seg.end)
    if e <= s:
        e = min(s + 1, len(ts))
    sub = frame_emb[s:e]
    if sub.shape[0] == 0:
        return s if s < len(ts) else None
    topic = sub.mean(0, keepdim=True)
    topic = topic / topic.norm()
    return s + int((sub * topic).sum(1).argmax())


def _scene_index(bounds, t):
    for i, (s, e) in enumerate(bounds):
        if s <= t < e:
            return i
    return max(0, len(bounds) - 1)


def build_event_card(video_id, duration, audio_segments, frames, frame_emb,
                     fps=None) -> EventCard:
    """audio_segments: list[Segment]; frames: [(t, rgb)]; frame_emb: torch (N,d)."""
    ts = [t for t, _ in frames]
    n = len(ts)
    fps = fps or config.FPS_SAMPLED

    # ---- 场景切分 (来自 CLIP 帧相似度断崖) ----
    cuts = scene_cut(frame_emb) if n >= 2 else []
    bounds = []
    prev = 0.0
    for i in cuts:
        b = min(ts[i], duration)
        bounds.append((prev, b))
        prev = b
    bounds.append((prev, duration))
    scenes = [Scene(start=s, end=e) for s, e in bounds]

    # ---- 每段配关键帧 + 场景索引 ----
    segments = []
    for seg in audio_segments:
        seg.scene_index = _scene_index(bounds, (seg.start + seg.end) / 2)
        if n > 0:
            seg.frame_id = _seg_frame_slot(frame_emb, ts, seg)
        segments.append(seg)

    # ---- 全局 MMR 关键帧 (整段语义多样) ----
    mmr_idx = mmr_keyframes(frame_emb, config.API_MAX_KEYFRAMES) if n else []
    keyframes = []
    for i, j in enumerate(mmr_idx):
        kf = Keyframe(id=i, t=ts[j])
        for seg in segments:
            if seg.start <= kf.t <= seg.end:
                kf.segment_id = seg.id
                break
        keyframes.append(kf)

    return EventCard(
        video_id=video_id, duration=duration, fps_sampled=fps,
        scenes=scenes, keyframes=keyframes, segments=segments)
