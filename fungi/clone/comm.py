"""Comm clone assembly: role prompt + CommTools + Clone."""

from pathlib import Path

from ..agent import Agent  # noqa: F401 (re-exported type)
from ..config import Config
from ..events import Sink
from ..pending import PendingAsks
from .base import Clone
from .tools_comm import CommTools

COMM_SYSTEM_PROMPT = """You are the comm Orchestrator for host {host}, dedicated to the counterpart \
comm Orchestrator on host {peer}.

Rules:
- You and your counterpart may collaborate autonomously; no user attention is needed for that.
- File work is restricted by the server-side guard: public/ is free for both sides; anything under \
homes/<host>/ needs the owning host user's consent (your own host's home too) — call ask_consent \
first, then use the fs tools (the granted consent is applied automatically).
- send_file moves a server-stored file onto the peer host's local disk; their user must accept it.
- Incoming chats appear as [peer] messages and are kept in your conversation history. Call send_peer \
when a reply is warranted — never reply just to acknowledge. If you end your turn without calling \
send_peer, your final message is delivered automatically; to say nothing, end with an empty reply.
- Use ask_user only when your own host's user must decide something.
- When given a [TASK], do exactly what the goal says and answer strictly in the reply format; report \
failure as specified instead of improvising.
"""


def build_comm_clone(
    host: str,
    peer: str,
    transport,
    cfg: Config,
    sink: Sink,
    llm=None,
    ask_timeout_s: float = 1800.0,
    poll_timeout: float = 5.0,
    system_prompt: str | None = None,
    inbox_dir: Path | None = None,
    on_turn_end=None,
) -> Clone:
    addr = f"{host}:comm-{peer}"
    pending = PendingAsks()
    comm_tools = CommTools(addr, transport, pending, ask_timeout_s, inbox_dir=inbox_dir)
    prompt = system_prompt or COMM_SYSTEM_PROMPT.format(host=host, peer=peer)

    def _chat_end(_env, reply: str) -> None:
        """Fallback: a chat turn that produced text but never called send_peer
        delivers that text — an LLM forgetting the tool call must not
        silently drop its reply (2026-09-03 real-machine finding)."""
        try:
            if reply and comm_tools.peer_sends == 0:
                comm_tools.send_peer({"text": reply})
        finally:
            comm_tools.peer_sends = 0

    return Clone(
        addr,
        transport,
        cfg,
        sink,
        tools=comm_tools.bound(),
        system_prompt=prompt,
        llm=llm,
        poll_timeout=poll_timeout,
        pending=pending,
        on_transfer=comm_tools.receive_transfer,
        on_chat_end=_chat_end,
        on_turn_end=on_turn_end,
        # No native base tools: file work only via the guarded fs tools; the
        # spawned workers inherit exactly that surface (spec 6.1).
        tool_names=frozenset(),
        child_tool_names=frozenset(),
        child_extra_tools=comm_tools.fs_bound(),
    )
