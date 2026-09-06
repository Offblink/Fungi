"""CLI: python -m vidsense.cli <video_or_url> [--no-images] [--json] [--no-api]

--no-api 只跑本地管线(不调 LLM), 用于无 key 时验证。
"""
import argparse
import json
import sys
from .understand import build, save_card, understand
from .api_client import output_as_json


def main(argv=None):
    p = argparse.ArgumentParser(description="视频理解应用: 输入视频(文件/URL) -> 返回结构化内容")
    p.add_argument("input", help="视频文件路径 或 B站 URL")
    p.add_argument("--no-images", action="store_true", help="不发送关键帧图给模型")
    p.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p.add_argument("--no-api", action="store_true", help="只构建事件卡, 不调用 LLM")
    args = p.parse_args(argv)

    try:
        if args.no_api:
            video, _, card = build(args.input)
            out = save_card(card, video.stem)
            print(f"事件卡已保存: {out}")
            print(json.dumps(card.to_dict(), ensure_ascii=False, indent=2)[:2000])
            return 0
        content, card = understand(args.input, use_images=not args.no_images)
    except Exception as e:  # noqa: BLE001
        print(f"错误: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(output_as_json(content), ensure_ascii=False, indent=2))
    else:
        print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
