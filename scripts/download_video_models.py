"""Pre-download the HF models Fungi's video tool needs (never on-demand).

Run: python scripts/download_video_models.py
Fungi's `video` tool refuses to run until these are in the HF hub cache; this
script seeds that cache. huggingface.co is unreachable from CN networks, so
HF_ENDPOINT defaults to hf-mirror.com (override by exporting HF_ENDPOINT).

CLIP's repo also carries TF/Flax weights (~1.8 GB extra) — we pick exactly one
torch weight file via the repo file list, then snapshot_download with
allow_patterns so nothing else lands.
"""

import os
import sys

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

try:
    from huggingface_hub import HfApi, snapshot_download
except ImportError:
    sys.stderr.write(
        "ERROR: huggingface_hub is not installed in this Python.\n"
        "Fix: pip install huggingface_hub\n"
    )
    sys.exit(1)

# label -> (repo_id, weight candidates in preference order, config/tokenizer patterns)
MODELS = [
    (
        "CLIP",
        "openai/clip-vit-base-patch32",
        ["model.safetensors", "pytorch_model.bin"],
        [
            "config.json",
            "preprocessor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.txt",
            "merges.txt",
            "special_tokens_map.json",
        ],
    ),
    (
        "whisper",
        "Systran/faster-whisper-small",
        ["model.bin"],
        [
            "config.json",
            "preprocessor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocabulary.json",
        ],
    ),
]


def main() -> int:
    api = HfApi()
    failed = []
    for label, repo, weights, aux in MODELS:
        print(f"== {label}: {repo}", flush=True)
        try:
            files = set(api.list_repo_files(repo))
        except Exception as exc:  # network mirror down / repo gone
            print(f"   listing {repo} failed: {exc}", flush=True)
            failed.append(label)
            continue
        weight = next((w for w in weights if w in files), None)
        if weight is None:
            print(f"   no known weight file in repo (have {sorted(files)[:8]}...)", flush=True)
            failed.append(label)
            continue
        patterns = [weight] + [p for p in aux if p in files]
        print(f"   downloading {patterns}", flush=True)
        try:
            snapshot_download(repo, allow_patterns=patterns)
        except Exception as exc:
            print(f"   download failed: {exc}", flush=True)
            failed.append(label)
        else:
            print(f"   {label} cached OK", flush=True)
    if failed:
        print(f"FAILED: {', '.join(failed)} — fix the network/endpoint and rerun.", flush=True)
        return 1
    print("All video models cached. Fungi's video tool is ready.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
