"""Local clone assembly: the user-facing Orchestrator with delegate/peers."""

from .. import tools as _tools
from ..config import Config
from ..events import Sink
from ..pending import PendingAsks
from ..tools.ask import make_ask_tool
from .base import Clone
from .delegate import DelegateTools

BASE_TOOL_NAMES = _tools.BASE_TOOL_NAMES

LOCAL_SYSTEM_PROMPT = """You are the local Orchestrator on host {host}: the only clone the user talks to.

Rules:
- Serve the user directly with your native tools for anything on this host.
- The user never talks to other hosts directly: delegate cross-host work with the delegate tool \
(check peers first), then relay the result back in your own words.
- ask_user questions surface as system notifications with a WebUI card.
"""


def build_local_clone(
    host: str,
    transport,
    cfg: Config,
    sink: Sink,
    llm=None,
    peers_fn=None,
    ask_timeout_s: float = 1800.0,
    poll_timeout: float = 5.0,
    on_ask=None,
    system_prompt: str | None = None,
) -> Clone:
    addr = f"{host}:local"
    pending = PendingAsks()
    tools = {"ask_user": make_ask_tool(sink)}
    if peers_fn is not None:
        tools.update(DelegateTools(addr, transport, pending, peers_fn, ask_timeout_s).bound())
    prompt = system_prompt or LOCAL_SYSTEM_PROMPT.format(host=host)
    return Clone(
        addr,
        transport,
        cfg,
        sink,
        tools=tools,
        system_prompt=prompt,
        llm=llm,
        poll_timeout=poll_timeout,
        on_ask=on_ask,
        pending=pending,
        # YESIR native full toolset (spec 6.2); spawned workers inherit it.
        tool_names=BASE_TOOL_NAMES,
        child_tool_names=BASE_TOOL_NAMES,
    )
