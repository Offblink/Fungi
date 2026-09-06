"""音频轴: faster-whisper -> 带时间戳的分段(含 words)。"""
from faster_whisper import WhisperModel
from . import config
from .schema import Word, Segment


def transcribe(audio_wav, model_size=None, language=None) -> list[Segment]:
    """音频文件 -> 分段(每段含 start/end/text/words)。speaker 阶段2(说话人分离)。"""
    model_size = model_size or config.ASR_MODEL
    language = config.ASR_LANGUAGE if language is None else language
    # device="auto" 自动 cuda/cpu; compute_type 固定 int8 -> 两机一致
    model = WhisperModel(model_size, device="auto", compute_type=config.ASR_COMPUTE)
    segments, _ = model.transcribe(
        str(audio_wav), language=language, word_timestamps=True, vad_filter=True)
    out: list[Segment] = []
    for i, seg in enumerate(segments):
        text = (seg.text or "").strip()
        if not text:
            continue
        words = [Word(start=w.start, end=w.end, text=w.word) for w in (seg.words or [])]
        if not words:
            words = [Word(start=seg.start, end=seg.end, text=text)]
        out.append(Segment(id=len(out), start=seg.start, end=seg.end, text=text,
                           speaker=None, words=words))
    return out
