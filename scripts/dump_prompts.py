"""Print the exact system prompt a role receives on its next turn.

Run: python scripts/dump_prompts.py <role> [--host NAME]

    l1            the user-facing orchestrator (TriLayer L1)
    server-local  the local clone of a room host
    comm:<peer>   the courier clone dedicated to <peer>

The prompts are assembled from five modules (clone/local, clone/comm, trilayer,
todos, diary) and two of the comm pieces are re-read per turn, so "what does
this role actually get" had no answer short of starting a room and printing it
from the inside. This builds the same objects the room builds and prints theirs,
with a per-section size table so a prompt that grew a section is visible.

The section table is checked, not rebuilt: each known piece is looked up in the
assembled prompt, and a piece reported MISSING means an assembly site moved and
this script needs the matching change.
"""

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # runnable from anywhere

# E402 is not raised after a sys.path edit, so these carry no noqa.
from fungi import skills, todos
from fungi.agent import SYSTEM_PROMPT
from fungi.clone.comm import COMM_SYSTEM_PROMPT, build_comm_clone
from fungi.clone.local import build_local_clone
from fungi.config import load_config
from fungi.diary import section as diary_section
from fungi.events import NullSink
from fungi.trilayer import L1_ADDENDUM, TriLayer


class _NeverCalled:
    """Stand-in transport: a prompt is built, no turn ever runs."""

    def send(self, _env):
        raise NotImplementedError("dump_prompts never sends")

    def poll(self, _after, _timeout):
        raise NotImplementedError("dump_prompts never polls")


def _local(cfg, host: str) -> tuple[str, list[tuple[str, str]]]:
    # room.py's build_agent: the clone's own prompt, plus the diary section
    # when the settings switch is on.
    clone = build_local_clone(host, _NeverCalled(), cfg, NullSink(), local_store=True)
    pieces = [("local clone prompt", clone.system_prompt)]
    if load_config().diary:
        pieces.append(("diary section", diary_section()))
    return "".join(text for _label, text in pieces), pieces


def _comm(cfg, host: str, peer: str) -> tuple[str, list[tuple[str, str]]]:
    clone = build_comm_clone(host, peer, _NeverCalled(), cfg, NullSink())
    # comm clones carry a callable: courier memory and the calendar are
    # re-read per turn, so asking it now is the only faithful answer.
    prompt = clone.system_prompt() if callable(clone.system_prompt) else clone.system_prompt
    memory = (load_config().courier_memory or "").strip()
    entries = todos.upcoming()
    pieces = [
        ("comm base prompt", COMM_SYSTEM_PROMPT.format(host=host, peer=peer)),
        ("todos.RULES", todos.RULES),
    ]
    pieces.append(("courier memory", memory))
    if entries:
        pieces.append(
            ("calendar", "\n".join(f"- {d}: {'; '.join(items)}" for d, items in entries) + "\n")
        )
    return prompt, pieces


def _l1(cfg) -> str:
    return TriLayer(cfg, NullSink()).build_orchestrator(NullSink()).system_prompt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Dump the evaluated system prompt of one role.")
    parser.add_argument("role", help="l1 | server-local | comm:<peer>")
    parser.add_argument(
        "--host", default=socket.gethostname(), help="this host's wire name (default: %(default)s)"
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.role == "l1":
        prompt = _l1(cfg)
        pieces = [
            ("base SYSTEM_PROMPT", SYSTEM_PROMPT),
            ("L1 addendum", L1_ADDENDUM),
            ("skills section", skills.section()),
            ("todos.RULES", todos.RULES),
        ]
        if cfg.diary:
            pieces.append(("diary section", diary_section()))
    elif args.role == "server-local":
        prompt, pieces = _local(cfg, args.host)
    elif args.role.startswith("comm:"):
        peer = args.role.split(":", 1)[1]
        if not peer:
            parser.error("comm:<peer> needs a peer name")
        prompt, pieces = _comm(cfg, args.host, peer)
    else:
        parser.error(f"unknown role: {args.role}")

    print(
        f"{args.role} on host {args.host}: {len(prompt)} chars, {prompt.count(chr(10)) + 1} lines"
    )
    for label, text in pieces:
        if not text:
            continue
        at = prompt.find(text)
        where = f"offset {at}" if at >= 0 else "MISSING (assembly changed?)"
        print(f"  {len(text):>6} chars  {label:<22} {where}")
    print("-" * 72)
    print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
