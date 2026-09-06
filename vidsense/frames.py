"""视觉轴: CLIP 帧编码 + scene_cut / content_frame / mmr_keyframes (算法已用 numpy 验证)."""
import numpy as np
import torch
from transformers import CLIPModel, CLIPProcessor
from . import config


def pick_device(prefer=None) -> str:
    """有 cuda 走 GPU, 否则 CPU; 结果与设备无关(冻结权重+固定预处理)."""
    if prefer:
        return prefer
    return "cuda" if torch.cuda.is_available() else "cpu"


class FrameEncoder:
    def __init__(self, model_name=None, device=None):
        name = model_name or config.CLIP_MODEL
        self.device = pick_device(device)
        self.model = CLIPModel.from_pretrained(name).to(self.device).eval()
        self.processor = CLIPProcessor.from_pretrained(name)

    @torch.no_grad()
    def encode_images(self, images, batch_size=config.CLIP_BATCH) -> torch.Tensor:
        """images: list[np.ndarray HWC RGB] -> (N,d) 已 L2 归一化."""
        feats = []
        for i in range(0, len(images), batch_size):
            inputs = self.processor(images=images[i:i + batch_size], return_tensors="pt").to(self.device)
            # 手动走 vision_model + visual_projection, 兼容 transformers 5.x
            pooled = self.model.vision_model(inputs["pixel_values"]).pooler_output
            f = self.model.visual_projection(pooled)
            feats.append(torch.nn.functional.normalize(f, dim=-1).cpu())
        return torch.cat(feats) if feats else torch.zeros(0, 512)

    @torch.no_grad()
    def encode_text(self, texts, batch_size=32) -> torch.Tensor:
        feats = []
        for i in range(0, len(texts), batch_size):
            inputs = self.processor(text=texts[i:i + batch_size], return_tensors="pt").to(self.device)
            pooled = self.model.text_model(
                input_ids=inputs["input_ids"],
                attention_mask=inputs["attention_mask"]).pooler_output
            f = self.model.text_projection(pooled)
            feats.append(torch.nn.functional.normalize(f, dim=-1).cpu())
        return torch.cat(feats) if feats else torch.zeros(0, 512)


def scene_cut(emb, threshold=None):
    """连续帧余弦相似度 < threshold -> 切点(帧索引)."""
    threshold = config.SCENE_CUT_THRESH if threshold is None else threshold
    sim = (emb[:-1] * emb[1:]).sum(dim=-1).numpy()
    return (np.where(sim < threshold)[0] + 1).tolist()


def content_frame_per_segment(emb, segment_bounds):
    """每段内挑离段主题(均值)最近的帧 -> 帧索引列表."""
    emb = emb.numpy()
    reps = []
    for s, e in segment_bounds:
        seg = emb[s:e]
        topic = seg.mean(axis=0, keepdims=True)
        seg = seg / np.maximum(np.linalg.norm(seg, axis=1, keepdims=True), 1e-6)
        topic = topic / np.linalg.norm(topic)
        reps.append(s + int((seg @ topic.T).argmax()))
    return reps


def mmr_keyframes(emb, k, lam=0.5):
    """整段挑 k 个语义多样关键帧 (MMR: λ·贴近主题 − (1−λ)·与已选重复). 增量 O(n·k)."""
    emb = emb.numpy().astype(np.float64)
    n = emb.shape[0]
    if n == 0 or k <= 0:
        return []
    emb = emb / np.maximum(np.linalg.norm(emb, axis=1, keepdims=True), 1e-6)
    q = emb.mean(axis=0)
    q = q / np.linalg.norm(q)
    sim_q = emb @ q
    chosen = [int(np.argmax(sim_q))]
    maxsim = emb @ emb[chosen[0]]
    while len(chosen) < min(k, n):
        score = lam * sim_q - (1 - lam) * maxsim
        score[chosen] = -np.inf
        i = int(np.argmax(score))
        chosen.append(i)
        maxsim = np.maximum(maxsim, emb @ emb[i])
    return chosen


def semantic_search(query_emb, frame_emb, k=5):
    sim = (frame_emb * query_emb).sum(dim=-1)
    return sim.topk(min(k, sim.numel())).indices.tolist()
