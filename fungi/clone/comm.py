"""Comm clone assembly: role prompt + CommTools + Clone."""

from pathlib import Path

from .. import todos
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
homes/<host>/ needs the owning host user's consent (your own host's home too) — call confirm \
first, then use the fs tools (the granted consent is applied automatically). Authoring \
documentation or skill-like files under public/docs/ also needs consent — propose first, never \
self-publish knowledge bases unannounced.
- send_file moves a server-stored file onto the peer host's local disk; their user must accept it.
- Incoming chats appear as [peer] messages and are kept in your conversation history. Call send_peer \
when a reply is warranted — never reply just to acknowledge. If you end your turn without calling \
send_peer, your final message is delivered automatically. To say nothing, end your turn with the \
exact single line <<SILENT>> — prose "silence declarations" would themselves be delivered; only \
the bare marker stays silent.
- Use inquire only when your own host's user must decide something.
- When given a [TASK], do exactly what the goal says and answer strictly in the reply format; report \
failure as specified instead of improvising.
"""

SILENT_REPLY = "<<SILENT>>"  # a comm turn ending with exactly this delivers nothing


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
    on_direct=None,
) -> Clone:
    addr = f"{host}:comm-{peer}"
    pending = PendingAsks()
    comm_tools = CommTools(addr, transport, pending, ask_timeout_s, inbox_dir=inbox_dir)
    base_prompt = system_prompt or COMM_SYSTEM_PROMPT.format(host=host, peer=peer)

    def courier_prompt() -> str:
        """Message-courier prompt, re-read per turn: the GUI memory entry
        (config.courier_memory) and the calendar (todos.upcoming) apply to
        the next incoming chat, live."""
        from ..config import load_config  # noqa: PLC0415 — re-read per turn, like the courier switch
        memory = (load_config().courier_memory or "").strip()
        entries = todos.upcoming()
        calendar = ""
        if entries:
            calendar = (
                "\n本机主人的日历待办（GUI 日历/todo 工具写入，回答涉及日程时据此代为说明）:\n"
                + "\n".join(f"- {d}: {'; '.join(items)}" for d, items in entries)
                + "\n"
            )
        if not memory and not calendar:
            return base_prompt
        return (
            base_prompt
            + "\n本机主人的长期备忘（用户在 GUI 里写给你的背景记忆，回答时可用它代为说明或转达）:\n"
            + memory + "\n"
            + calendar
        )

    def _chat_end(_env, reply: str) -> None:
        """Fallback: a chat turn that produced text but never called send_peer
        delivers that text — an LLM forgetting the tool call must not
        silently drop its reply (2026-09-03 real-machine finding). A turn
        ending with exactly SILENT_REPLY says nothing: the bare marker is the
        only reliable "empty reply" an LLM actually produces (prose silence
        declarations would themselves be delivered — the five-round
        mutual-silence loop of 2026-09-04)."""
        try:
            if reply and reply.strip() != SILENT_REPLY and comm_tools.peer_sends == 0:
                comm_tools.send_peer({"text": reply})
        finally:
            comm_tools.peer_sends = 0

    return Clone(
        addr,
        transport,
        cfg,
        sink,
        tools={**comm_tools.bound(), **todos.bound()},
        system_prompt=courier_prompt,
        llm=llm,
        poll_timeout=poll_timeout,
        pending=pending,
        on_transfer=comm_tools.receive_transfer,
        on_chat_end=_chat_end,
        on_turn_end=on_turn_end,
        on_direct=on_direct,
        # No native base tools: file work only via the guarded fs tools; the
        # spawned workers inherit exactly that surface (spec 6.1).
        tool_names=frozenset(),
        child_tool_names=frozenset(),
        child_extra_tools=comm_tools.fs_bound(),
    )
