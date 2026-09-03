"""Clone base: transports + the inbox/turn thread model.

Thread model (design.md): the loop thread polls the transport and dispatches
control envelopes (answer/result/err) immediately — that is what wakes blocked
asks while a turn is running. Turn envelopes (chat/task) are queued to a single
worker thread, keeping turns serial per clone.
"""

import queue
import threading

from .. import tools
from ..agent import Agent, BoundTool
from ..config import Config
from ..events import Sink
from ..hub.app import fs_via_hub
from ..hub.client import HubClient
from ..hub.relay import Relay
from ..pending import PendingAsks
from ..protocol import Envelope, parse_addr
from ..trilayer import TriLayer

TURN_TYPES = ("chat", "task")
MAX_CHAT_HISTORY = 40  # chat messages kept per clone; older entries are dropped


class LocalTransport:
    """For clones hosted on the server process itself: direct relay + store."""

    def __init__(self, relay: Relay, self_addr: str, hub=None):
        self.relay = relay
        self.hub = hub
        self.inbox = relay.register_local(self_addr)
        self.host, _role, _peer = parse_addr(self_addr)

    def send(self, env: Envelope) -> dict:
        # via hub.send so ask/answer registry maintenance applies locally too
        if self.hub is not None:
            return self.hub.send(env)
        return self.relay.deliver(env)

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:
        return self.inbox.after(after, timeout)

    def fs(self, op: str, path: str, **kw) -> dict:
        if self.hub is None:
            return {"error": "no hub attached"}
        return fs_via_hub(self.hub.store, self.host, op, path, **kw)


class RemoteTransport:
    """For clones on client hosts: HTTP to the hub.

    Poll source is injectable: a multi-clone client host runs one HostPoller
    over the shared host buffer and feeds each clone its own Inbox — polling
    the shared buffer directly would let clones steal each other's messages.
    """

    def __init__(self, client: HubClient, inbox=None):
        self.client = client
        self.inbox = inbox

    def send(self, env: Envelope) -> dict:
        return self.client.send(env)

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:
        if self.inbox is not None:
            return self.inbox.after(after, timeout)
        return self.client.poll(after, timeout)

    def fs(self, op: str, path: str, **kw) -> dict:
        return self.client.fs(op, path, **kw)


class Clone:
    """One Orchestrator clone: role prompt + toolset + inbox loop."""

    def __init__(
        self,
        addr: str,
        transport: LocalTransport | RemoteTransport,
        cfg: Config,
        sink: Sink,
        tools: dict[str, BoundTool] | None = None,
        system_prompt: str = "",
        llm=None,
        model: str | None = None,
        poll_timeout: float = 5.0,
        on_ask=None,
        pending: PendingAsks | None = None,
        tool_names: frozenset[str] | set[str] = frozenset(tools.BASE_TOOL_NAMES),
        child_tool_names: frozenset[str] | None = None,
        child_extra_tools: dict[str, BoundTool] | None = None,
    ):
        self.addr = addr
        self.host, self.role, self.peer = parse_addr(addr)
        self.transport = transport
        self.cfg = cfg
        self.sink = sink
        self.tools = tools or {}
        # Native base-tool whitelist; comm clones pass frozenset() so only the
        # guarded extra tools exist (spec 6.1; the path guard is server-side).
        self.tool_names = frozenset(tool_names)
        # Spawned worker surface (None -> TriLayer native defaults)
        self.child_tool_names = child_tool_names
        self.child_extra_tools = child_extra_tools
        self.system_prompt = system_prompt
        self.llm = llm
        self.model = model
        self.poll_timeout = poll_timeout
        self.on_ask = on_ask
        # `is not None` (not `or`): an empty PendingAsks is falsy via __len__
        self.pending = pending if pending is not None else PendingAsks()
        self.history: list[dict] = []  # chat exchanges kept locally (no-reply stays)
        self._cursor = 0
        self._stop = threading.Event()
        self._work: queue.Queue[Envelope] = queue.Queue()
        self._loop_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None

    # ── lifecycle ──

    def start(self) -> None:
        self._loop_thread = threading.Thread(
            target=self._loop, name=f"clone-loop-{self.addr}", daemon=True
        )
        self._worker_thread = threading.Thread(
            target=self._work_loop, name=f"clone-turn-{self.addr}", daemon=True
        )
        self._loop_thread.start()
        self._worker_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._work.put(None)  # unblock the worker
        for t in (self._loop_thread, self._worker_thread):
            if t is not None:
                t.join(timeout=5)

    # ── threads ──

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                messages, cursor = self.transport.poll(self._cursor, self.poll_timeout)
            except Exception:
                self._stop.wait(1.0)  # transient network hiccup: back off, keep looping
                continue
            self._cursor = cursor
            for env in messages:
                self.dispatch(env)

    def _work_loop(self) -> None:
        while True:
            env = self._work.get()
            if env is None or self._stop.is_set():
                return
            try:
                self.run_turn(env)
            except Exception as exc:  # a turn must never kill the worker
                self.sink.emit("error", f"{self.addr}: turn failed: {exc}")

    def dispatch(self, env: Envelope) -> None:
        """Control envelopes wake blocked tools inline; turns are queued."""
        if env.type == "answer" and env.reply_to:
            self.pending.resolve(env.reply_to, env.body.get("value"))
        elif env.type == "result" and env.reply_to:
            self.pending.resolve(env.reply_to, env.body)
        elif env.type == "err":
            self.sink.emit("error", f"{self.addr}: {env.body.get('error')}")
        elif env.type == "ask":
            if self.on_ask is not None:
                self.on_ask(env)
        elif env.type in TURN_TYPES:
            self._work.put(env)

    # ── turns ──

    def render_input(self, env: Envelope) -> str:
        if env.type == "task":
            body = env.body
            parts = [f"[TASK from {env.src}]", f"Goal: {body.get('goal', '')}"]
            if body.get("reply_format"):
                parts.append(f"Reply format: {body['reply_format']}")
            if body.get("context"):
                parts.append(f"Context: {body['context']}")
            return "\n".join(parts)
        return f"[{env.src}] {env.body.get('text', '')}"

    def build_agent(self) -> Agent:
        """Per-turn agent: clone's whitelist + guarded extra tools + spawn
        (TriLayer L2/L3; children inherit this clone's file surface)."""
        trilayer = TriLayer(
            self.cfg,
            self.sink,
            llm=self.llm,
            child_tool_names=self.child_tool_names,
            child_extra_tools=self.child_extra_tools,
        )
        return trilayer.build_clone_agent(
            self.sink,
            system_prompt=self.system_prompt,
            extra_tools=self.tools,
            tool_names=self.tool_names,
            model=self.model,
        )

    def run_turn(self, env: Envelope) -> None:
        agent = self.build_agent()
        if env.type == "chat":
            # Chat turns ride the accumulated local history.
            messages = [*self.history, {"role": "user", "content": self.render_input(env)}]
        else:
            messages = [{"role": "user", "content": self.render_input(env)}]
        result = agent.run(messages)
        reply = (result.content or "").strip()
        if env.type == "chat":
            # spec 6.x: the Orchestrator decides whether to reply — via an
            # explicit send_peer call. No auto-reply; the exchange is kept.
            self.history.append({"role": "user", "content": self.render_input(env)})
            if reply:
                self.history.append({"role": "assistant", "content": reply})
            del self.history[:-MAX_CHAT_HISTORY]
            return
        self.transport.send(
            Envelope(
                src=self.addr,
                dst=env.src,
                type="result",
                body={"ok": True, "payload": reply},
                reply_to=env.id,
            )
        )
