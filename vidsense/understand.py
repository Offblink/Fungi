"""端到端管线: video/url -> 摄取 -> 转写 -> CLIP -> 事件卡 -> 调用API -> 返回内容."""
import json
from . import config
from .ingest import ingest
from .transcribe import transcribe
from .frames import FrameEncoder
from .cards import build_event_card
from .api_client import call_api
from .source import resolve


def build(input_):
    """本地管线(不调API): 返回 (video_path, ingest_result, EventCard). 可在无key时单独测."""
    video, meta = resolve(input_)
    met = ingest(video)
    segments = transcribe(met["audio"])
    enc = FrameEncoder()
    emb = enc.encode_images([f for _, f in met["frames"]])
    card = build_event_card(meta.get("bvid", video.stem), met["duration"],
                            segments, met["frames"], emb)
    return video, met, card


def save_card(card, name):
    config.ensure_dirs()
    out = config.JSON_DIR / f"{name}_eventcard.json"
    out.write_text(json.dumps(card.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def understand(input_, use_images=None, key=None):
    """端到端: 返回 (模型内容, 事件卡dict)."""
    video, met, card = build(input_)
    save_card(card, video.stem)
    content = call_api(card, met["frames"], key=key)
    return content, card.to_dict()
