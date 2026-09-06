"""输入源解析: 本地文件 或 URL(B站)。URL -> 下载 mp4 -> 走统一 ingest。"""
import re
import subprocess
import json
import urllib.request
from pathlib import Path
from . import config

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
BILI_HEADERS = {"Referer": "https://www.bilibili.com", "User-Agent": UA}


def is_url(s) -> bool:
    return str(s).startswith(("http://", "https://"))


def _get_json(url: str, headers=BILI_HEADERS) -> dict:
    req = urllib.request.Request(url, headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _follow_short(url: str) -> str:
    req = urllib.request.Request(url, headers=BILI_HEADERS, method="HEAD")
    return urllib.request.urlopen(req).geturl()


def _bvid_from(url: str) -> str:
    url = _follow_short(url) if "b23.tv" in url else url
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        raise ValueError(f"无法从 URL 解析 bvid: {url}")
    return m.group(1)


def _dl(url: str, out, headers=BILI_HEADERS):
    out = Path(out)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r, open(out, "wb") as f:
        f.write(r.read())
    return out


def _merge(video, audio, out) -> Path:
    out = Path(out)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-i", str(audio), "-c", "copy",
         "-movflags", "+faststart", str(out)],
        check=True, capture_output=True)
    return out


def _download_bilibili(url: str):
    """B站 URL -> 本地 mp4 -> (path, meta)。

    优先 fnval=16(渐进式 durl); 无则 fnval=4048(DASH) 分开拉视频/音频流再 merge。
    """
    bvid = _bvid_from(url)
    view = _get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    data = view["data"]
    cid, title, duration = data["cid"], data["title"], data["duration"]
    config.ensure_dirs()
    out = config.VIDEO_DIR / (bvid + ".mp4")

    # 1) 渐进式 mp4
    pv = _get_json(f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=80&fnval=16")
    durl = pv["data"].get("durl")
    if durl:
        _dl(durl[0]["url"], out)
        return out, {"source": "bilibili", "bvid": bvid, "title": title, "duration": duration}

    # 2) DASH: 分开视频+音频, 再合并
    pv = _get_json(f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=80&fnval=4048")
    dash = pv["data"].get("dash")
    if not dash or not dash.get("video") or not dash.get("audio"):
        raise ValueError("B站该视频既无渐进式 mp4 也无可用 DASH 流，无法下载")
    vsrc = max(dash["video"], key=lambda v: v.get("bandwidth", 0))
    asrc = max(dash["audio"], key=lambda a: a.get("bandwidth", 0))
    vpath = _dl(vsrc["baseUrl"], config.VIDEO_DIR / f"{bvid}_v.m4s")
    apath = _dl(asrc["baseUrl"], config.AUDIO_DIR / f"{bvid}_a.m4s")
    _merge(vpath, apath, out)
    return out, {"source": "bilibili", "bvid": bvid, "title": title, "duration": duration}


def resolve(input_):
    """统一入口: 本地文件 or URL -> (video_path, meta)."""
    if not is_url(input_):
        return Path(input_), {"source": "file"}
    if "bilibili.com" in input_ or "b23.tv" in input_:
        return _download_bilibili(input_)
    raise ValueError(f"暂不支持该 URL 源(当前支持 B站): {input_}")
