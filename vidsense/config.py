"""集中配置: 模型名 / fps / 阈值 / 路径 / API。所有可调参数集中于此，保证两机一致。"""
from pathlib import Path
import os

# --- 优先从项目 .env 读 key/配置 (免贴聊天、不入 git) ---
def _load_dotenv(path=".env"):
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

# ---------------- 视觉轴 ----------------
CLIP_MODEL = os.environ.get("VU_CLIP_MODEL", "openai/clip-vit-base-patch32")
FPS_SAMPLED = float(os.environ.get("VU_FPS", "1.0"))   # 抽帧频率 (帧/秒)
CLIP_BATCH = int(os.environ.get("VU_CLIP_BATCH", "64"))
SCENE_CUT_THRESH = float(os.environ.get("VU_CUT", "0.85"))  # 连续帧余弦相似度 < 此值 -> 镜头切点

# ---------------- 音频轴 ----------------
ASR_MODEL = os.environ.get("VU_ASR_MODEL", "small")    # faster-whisper 模型尺寸
ASR_LANGUAGE = os.environ.get("VU_ASR_LANG")            # None=自动; 中文可设 "zh"
ASR_COMPUTE = os.environ.get("VU_ASR_COMPUTE", "int8")  # 固定计算精度 -> 两机一致
AUDIO_SR = 16000

# ---------------- 输入/输出 ----------------
WORK_DIR = Path(os.environ.get("VU_WORK_DIR", "output"))
VIDEO_DIR = WORK_DIR / "video"
AUDIO_DIR = WORK_DIR / "audio"
FRAME_DIR = WORK_DIR / "frames"
JSON_DIR = WORK_DIR / "json"

# ---------------- API (OpenAI 兼容, 可配) ----------------
API_BASE_URL = os.environ.get("VU_API_BASE_URL", "https://api.deepseek.com")
API_KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("VU_API_KEY") or ""
API_MODEL = os.environ.get("VU_API_MODEL", "deepseek-v4-flash-vision-exp")  # 可按你的 DeepSeek 端点覆盖
API_USE_IMAGES = os.environ.get("VU_API_USE_IMAGES", "1") == "1"
API_MAX_KEYFRAMES = int(os.environ.get("VU_API_MAX_KEYFRAMES", "12"))   # 最多送多少张关键帧图


def ensure_dirs():
    for d in (WORK_DIR, VIDEO_DIR, AUDIO_DIR, FRAME_DIR, JSON_DIR):
        d.mkdir(parents=True, exist_ok=True)
