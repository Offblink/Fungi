"""Comm clone assembly: role prompt + CommTools + Clone."""

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
- Incoming chats appear as [peer] messages and are kept in your conversation history. Call send_peer \
only when a reply is actually warranted — never reply just to acknowledge.
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
    ask_timeout_s: float = 600.0,
    poll_timeout: float = 5.0,
    system_prompt: str | None = None,
) -> Clone:
    addr = f"{host}:comm-{peer}"
    pending = PendingAsks()
    comm_tools = CommTools(addr, transport, pending, ask_timeout_s)
    prompt = system_prompt or COMM_SYSTEM_PROMPT.format(host=host, peer=peer)
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
        # No native base tools: file work only via the guarded fs tools; the
        # spawned workers inherit exactly that surface (spec 6.1).
        tool_names=frozenset(),
        child_tool_names=frozenset(),
        child_extra_tools=comm_tools.fs_bound(),
    )
