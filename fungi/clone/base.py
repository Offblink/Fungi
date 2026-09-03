"""Clone base: transports + the inbox/turn thread model.

Thread model (design.md): the loop thread polls the transport and dispatches
control envelopes (answer/result/err) immediately — that is what wakes blocked
asks while a turn is running. Turn envelopes (chat/task) are queued to a single
worker thread, keeping turns serial per clone.
"""

import queue
import threading

from ..agent import Agent, BoundTool
from ..config import Config
from ..events import Sink
from ..hub.app import fs_via_hub
from ..hub.client import HubClient
from ..hub.relay import Relay
from ..pending import PendingAsks
from ..protocol import Envelope, parse_addr

TURN_TYPES = ("chat", "task")


class LocalTransport:
    """For clones hosted on the server process itself: direct relay + store."""

    def __init__(self, relay: Relay, self_addr: str, hub=None):
        self.relay = relay
        self.hub = hub
        self.inbox = relay.register_local(self_addr)
        self.host = parse_addr(self_addr).host

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
    """For clones on client hosts: HTTP to the hub."""

    def __init__(self, client: HubClient):
        self.client = client

    def send(self, env: Envelope) -> dict:
        return self.client.send(env)

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:
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
    ):
        self.addr = addr
        self.host, self.role, self.peer = parse_addr(addr)
        self.transport = transport
        self.cfg = cfg
        self.sink = sink
        self.tools = tools or {}
        self.system_prompt = system_prompt
        self.llm = llm
        self.model = model
        self.poll_timeout = poll_timeout
        self.on_ask = on_ask
        # `is not None` (not `or`): an empty PendingAsks is falsy via __len__
        self.pending = pending if pending is not None else PendingAsks()
        self._cursor = 0
        self._stop = threading.Event()
        self._work: queue.Queue[Envelope] = queue.Queue()
        self._loop_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None

    # ── lifecycle ──

    def start(self) -> None:
        self._loop_thread = threading.Thread(target=self._loop, name=f"clone-loop-{self.addr}")
        self._worker_thread = threading.Thread(
            target=self._work_loop, name=f"clone-turn-{self.addr}"
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
        return Agent(
            self.cfg,
            self.sink,
            system_prompt=self.system_prompt,
            extra_tools=self.tools,
            llm=self.llm,
            model=self.model,
        )

    def run_turn(self, env: Envelope) -> None:
        agent = self.build_agent()
        messages: list[dict] = [{"role": "user", "content": self.render_input(env)}]
        result = agent.run(messages)
        reply = (result.content or "").strip()
        if env.type == "task":
            body = {"ok": True, "payload": reply}
        else:
            body = {"text": reply}
        out_type = "result" if env.type == "task" else "chat"
        self.transport.send(
            Envelope(src=self.addr, dst=env.src, type=out_type, body=body, reply_to=env.id)
        )
