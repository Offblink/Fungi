"""事件卡 / Grounding Context 数据结构。"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Scene:
    start: float
    end: float


@dataclass
class Keyframe:
    id: int
    t: float
    segment_id: Optional[int] = None
    caption: str = ""


@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    words: list[Word] = field(default_factory=list)
    frame_id: Optional[int] = None
    scene_index: Optional[int] = None


@dataclass
class EventCard:
    video_id: str
    duration: float
    fps_sampled: float
    scenes: list[Scene] = field(default_factory=list)
    keyframes: list[Keyframe] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    # keyframe 的 CLIP 向量(排序与 keyframes 对齐), 供检索用
    embeddings: list[list[float]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "EventCard":
        return cls(
            video_id=d["video_id"], duration=d["duration"], fps_sampled=d["fps_sampled"],
            scenes=[Scene(start=s["start"], end=s["end"]) for s in d.get("scenes", [])],
            keyframes=[Keyframe(id=k["id"], t=k["t"], segment_id=k.get("segment_id"),
                                caption=k.get("caption", "")) for k in d.get("keyframes", [])],
            segments=[Segment(id=s["id"], start=s["start"], end=s["end"], text=s["text"],
                              speaker=s.get("speaker"),
                              words=[Word(**w) for w in s.get("words", [])],
                              frame_id=s.get("frame_id"), scene_index=s.get("scene_index"))
                      for s in d.get("segments", [])],
            embeddings=d.get("embeddings", []),
        )

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "duration": self.duration,
            "fps_sampled": self.fps_sampled,
            "scenes": [{"start": s.start, "end": s.end} for s in self.scenes],
            "keyframes": [
                {"id": k.id, "t": k.t, "segment_id": k.segment_id, "caption": k.caption}
                for k in self.keyframes
            ],
            "segments": [
                {
                    "id": s.id,
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                    "speaker": s.speaker,
                    "words": [{"start": w.start, "end": w.end, "text": w.text} for w in s.words],
                    "frame_id": s.frame_id,
                    "scene_index": s.scene_index,
                }
                for s in self.segments
            ],
            "embeddings": self.embeddings,
        }
