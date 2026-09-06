"""API 层: OpenAI 兼容 视觉LLM 调用 (DeepSeek 默认, 可配 base_url/key/model)。

把 Grounding Context(事件卡)拼成: 文字时间轴 + 关键帧图 -> 发给视觉模型 -> 返回结构化视频内容。
key 从环境变量读: DEEPSEEK_API_KEY / VU_API_KEY (OpenAI 兼容)。
"""
import base64
import bisect
import json
import requests
import cv2
from . import config
from .schema import EventCard

SYSTEM_PROMPT = (
    "你是视频内容理解助手。基于给定的【字幕时间轴】和【关键帧】，理解这段视频。"
    "用中文输出结构化内容，格式：\n"
    "1. 整体摘要（1-3 句）\n"
    "2. 时间轴内容（分段列，每段带 [start-end s] 和该段讲了什么）\n"
    "3. 主题 / 观点 / 关键信息（bullet）"
)


def _img_to_base64(rgb) -> str:
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    assert ok, "imencode failed"
    return base64.b64encode(buf.tobytes()).decode()


def build_context_text(card: EventCard) -> str:
    lines = [f"视频时长 {card.duration:.1f}s；抽帧频率 {card.fps_sampled} fps。"]
    lines.append("--- 字幕事件卡(时间轴) ---")
    for s in card.segments:
        spk = f"[{s.speaker}] " if s.speaker else ""
        lines.append(f"[{s.start:.2f}-{s.end:.2f}s] {spk}{s.text}")
    lines.append("--- 关键帧时间点 ---")
    lines.append(", ".join(f"{k.t:.2f}s" for k in card.keyframes) or "无")
    return "\n".join(lines)


def _keyframe_imgs(card: EventCard, frames):
    """按关键帧时间, 从 frames[(t,rgb)] 里取最近帧的 RGB 图."""
    ts = [t for t, _ in frames]
    out = []
    for kf in card.keyframes:
        i = bisect.bisect_left(ts, kf.t)
        cands = [c for c in (i - 1, i) if 0 <= c < len(frames)]
        j = min(cands, key=lambda c: abs(ts[c] - kf.t)) if cands else 0
        out.append(frames[j][1])
    return out


def build_messages(card: EventCard, frames, use_images=None):
    use_images = config.API_USE_IMAGES if use_images is None else use_images
    parts = [{"type": "text", "text": build_context_text(card)}]
    if use_images and card.keyframes and frames:
        for rgb in _keyframe_imgs(card, frames):
            b64 = _img_to_base64(rgb)
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": parts},
    ]


def call_api(card: EventCard, frames, key=None, timeout=180, use_images=None) -> str:
    key = key or config.API_KEY
    if not key:
        raise ValueError("缺少 API key。请设置环境变量 DEEPSEEK_API_KEY 或 VU_API_KEY。")
    url = config.API_BASE_URL.rstrip("/") + "/chat/completions"
    body = {"model": config.API_MODEL,
            "messages": build_messages(card, frames, use_images=use_images)}
    r = requests.post(url, json=body, timeout=timeout, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def output_as_json(text: str) -> dict:
    """尽力把模型输出解析成结构化 JSON; 失败则原样返回文本."""
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        pass
    return {"text": text}
