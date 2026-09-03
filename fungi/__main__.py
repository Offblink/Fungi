"""Entry point: `python -m fungi [query]` for single-shot console mode."""

import argparse

from fungi import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fungi",
        description="Fungi — LAN multi-host Orchestrator network on the TriLayer agent harness",
    )
    parser.add_argument("query", nargs="*", help="single-shot query (console mode)")
    parser.add_argument("--version", action="version", version=f"fungi {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.query:
        from fungi.cli import run_single_shot  # noqa: PLC0415 (lazy: keep --help dependency-free)

        return run_single_shot(" ".join(args.query))
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
