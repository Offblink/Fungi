"""TriLayer agent system.

L1 Orchestrator (user-facing) → spawn → L2 Task Agent → spawn → L3 Worker.

Dispatch contract (TaskSpec): the upper agent states the goal and the reply
format; the lower agent executes strictly within that scope and MUST answer in
the requested format. Final answers flow back as the spawn tool result.
"""

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fungi import skills, tools
from fungi.agent import SYSTEM_PROMPT, Agent, BoundTool
from fungi.config import Config
from fungi.events import FnSink, Sink
from fungi.llm import LLMResult
from fungi.tools.ask import make_ask_tool
from fungi.tools.mcp import mcp_extra_tools

MAX_SPAWNS_PER_TURN = 8
JSON_RETRIES = 2

L1_ADDENDUM = """

## TriLayer dispatch
You are the L1 Orchestrator — the only layer that talks to the user. For
substantial subtasks (research, multi-file work, independent checks) use the
`spawn` tool to dispatch an L2 Task Agent instead of doing everything inline.
When spawning you MUST write:
- goal: what the subagent should accomplish (self-contained, no references to
  this conversation),
- reply_format: exactly how to report back (e.g. "yes/no plus a reason",
  "JSON with fields X and Y", "list of up to 3 file paths").
context. Trivial one-step actions (a single read or a single command) are
better done directly with your own tools.

## Asking the user
You are the only layer that can ask the user a question. Use `inquire` when
a decision genuinely needs the user's input (choosing between approaches,
confirming something hard to undo). Offer clear `options` when possible; the
user can also type a free-form answer. Asking blocks the turn until they
answer, so ask only when it truly matters — do not ask for permission to do
obvious work.
"""

L2_SYSTEM = """\
You are a Task Agent (layer 2 of a three-layer system). An orchestrator has
dispatched a task to you with an explicit goal and a required reply format.

Execute the task with your tools. You may use the `spawn` tool to dispatch an
L3 Worker for basic sub-steps (single file operations, single commands,
single lookups) — never for whole-task delegation.

Discipline (mandatory):
- Do exactly what the goal says. Do NOT widen the scope, touch unrelated
  files, or pursue improvements nobody asked for.
- Tool output is ground truth. NEVER fabricate file contents or command output.
- Your FINAL message must follow the required reply_format exactly — it is
  the only thing the orchestrator sees. No preamble, no meta commentary.
- If the task cannot be completed, say so inside the required reply format
  rather than improvising something else.
"""

L3_SYSTEM = """\
You are a basic Worker (layer 3, the lowest layer of a three-layer system).
A Task Agent has dispatched a small, concrete job to you.

Your toolset is intentionally limited to: read, write, edit, glob, grep, bash.

Discipline (mandatory):
- Do exactly what the goal says, in as few steps as possible. Do NOT widen
  the scope or fix anything beyond the goal.
- Tool output is ground truth. NEVER fabricate results.
- Your FINAL message must follow the required reply_format exactly — it is
  the only thing the dispatcher sees.
- If the job cannot be done, report that inside the required reply format.
"""

SPAWN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn",
        "description": (
            "Dispatch a subagent one layer below you. Write a self-contained goal and the"
            " exact format the subagent must use for its final reply. Returns the subagent's"
            " reply, or FAIL: <reason>."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Self-contained description of the task"},
                "reply_format": {
                    "type": "string",
                    "description": (
                        'Required format of the final reply, e.g. "yes/no + reason", '
                        '"JSON {found: bool, paths: []}"'
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "Background material the subagent needs (optional)",
                },
                "constraints": {
                    "type": "string",
                    "description": "Hard boundaries, e.g. files it may touch (optional)",
                },
            },
            "required": ["goal", "reply_format"],
        },
    },
}


@dataclass
class TaskSpec:
    id: str
    layer: int  # 2 or 3
    goal: str
    reply_format: str
    context: str = ""
    constraints: str = ""
    parent_id: str | None = None


def task_brief(spec: TaskSpec) -> str:
    parts = [f"## Goal\n{spec.goal}", f"## Reply format (mandatory)\n{spec.reply_format}"]
    if spec.context:
        parts.append(f"## Context\n{spec.context}")
    if spec.constraints:
        parts.append(f"## Constraints\n{spec.constraints}")
    return "\n\n".join(parts)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


class TriLayer:
    """Spawns and supervises subagents for one agent turn tree."""

    def __init__(
        self,
        cfg: Config,
        sink: Sink,
        llm: Callable[[list[dict], list[dict]], LLMResult] | None = None,
        should_abort: Callable[[], bool] | None = None,
        child_tool_names: frozenset[str] | None = None,
        child_extra_tools: dict[str, BoundTool] | None = None,
        skill_save: bool = False,
    ) -> None:
        """child_tool_names/child_extra_tools: when set, spawned subagents use
        this surface instead of the native defaults — a clone's spawn inherits
        the clone's file restrictions (spec 6.1)."""
        self.cfg = cfg
        self.sink = sink
        self._llm = llm
        self._should_abort = should_abort
        self._child_tool_names = child_tool_names
        self._child_extra_tools = child_extra_tools or {}
        # Whether agents built from this layer may author skills (user-facing
        # surfaces only; autonomous comm clones get a readonly tool).
        self._skill_save = skill_save
        self._active = 0
        self._lock = threading.Lock()
        # spec_id -> {id, call_id, layer, goal, reply_format, status, events: [...]}
        self.subagents: dict[str, dict] = {}
        self.asks: list[dict] = []  # completed inquire records (for persistence)

    def bound_spawn(self, parent_layer: int) -> BoundTool:
        """The spawn tool bound to a parent layer: L1 spawns L2, L2 spawns L3."""

        return BoundTool(
            schema=SPAWN_SCHEMA,
            fn=lambda args, call_id=None: self._spawn(args, parent_layer, call_id),
            with_call_id=True,
        )

    def build_orchestrator(self, sink: Sink) -> Agent:
        """The L1 agent, ready to run user turns."""
        agent = Agent(
            self.cfg,
            sink,
            system_prompt=SYSTEM_PROMPT + L1_ADDENDUM + skills.section(),
            extra_tools={
                "spawn": self.bound_spawn(1),
                "inquire": make_ask_tool(
                    sink, on_answer=self.asks.append, should_abort=self._should_abort
                ),
                **mcp_extra_tools(self.cfg.mcp_servers),
                **skills.bound(),
            },
            parallel_tools={"spawn"},
            llm=self._llm,
            model=self.cfg.model_for(1),
            should_abort=self._should_abort,
        )
        # Persisted by the WebUI turn runner (server.py / room.py): spawn
        # records + completed inquire records for session replay.
        agent.subagents = self.subagents
        agent.asks = self.asks
        return agent

    def build_clone_agent(
        self,
        sink: Sink,
        *,
        system_prompt: str,
        extra_tools: dict[str, BoundTool],
        tool_names: frozenset[str] | set[str] = frozenset(),
        model: str | None = None,
    ) -> Agent:
        """A clone turn agent: clone's prompt/tools + spawn; children inherit
        the clone's file surface (see __init__ child_* params)."""
        agent = Agent(
            self.cfg,
            sink,
            system_prompt=system_prompt + skills.section(),
            tool_names=tool_names,
            extra_tools={
                "spawn": self.bound_spawn(1),
                **skills.bound(readonly=not self._skill_save),
                **extra_tools,
            },
            parallel_tools={"spawn"},
            llm=self._llm,
            model=model or self.cfg.model_for(1),
            should_abort=self._should_abort,
        )
        agent.subagents = self.subagents
        agent.asks = self.asks
        return agent

    def _spawn(self, args: dict, parent_layer: int, call_id: str | None = None) -> str:
        goal = str(args.get("goal") or "").strip()
        reply_format = str(args.get("reply_format") or "").strip()
        if not goal:
            return "ERROR: Missing required argument: goal"
        if not reply_format:
            return "ERROR: Missing required argument: reply_format"
        child_layer = parent_layer + 1
        if child_layer > 3:
            return "ERROR: You are at the deepest layer (L3); finish the job yourself."

        with self._lock:
            if self._active >= MAX_SPAWNS_PER_TURN:
                return f"ERROR: spawn limit reached ({MAX_SPAWNS_PER_TURN} per turn)."
            self._active += 1

        spec = TaskSpec(
            id=uuid.uuid4().hex[:6],
            layer=child_layer,
            goal=goal,
            reply_format=reply_format,
            context=str(args.get("context") or ""),
            constraints=str(args.get("constraints") or ""),
        )
        record = {
            "id": spec.id,
            "call_id": call_id,
            "layer": spec.layer,
            "goal": goal,
            "reply_format": reply_format,
            "status": "running",
            "events": [],
        }
        self.subagents[spec.id] = record
        self.sink.emit(
            "agent_spawn",
            {
                "id": spec.id,
                "call_id": call_id,
                "layer": spec.layer,
                "goal": goal,
                "reply_format": reply_format,
            },
        )
        self.sink.emit("agent_status", {"id": spec.id, "status": "running"})
        try:
            answer = self._run_task(spec)
            failed = answer.startswith("FAIL")
            status = "failed" if failed else "done"
            self.sink.emit("agent_status", {"id": spec.id, "status": status})
            record["status"] = status
            return answer
        except Exception as exc:
            self.sink.emit("agent_status", {"id": spec.id, "status": "failed"})
            record["status"] = "failed"
            return f"FAIL: subagent crashed: {exc}"
        finally:
            with self._lock:
                self._active -= 1

    def _run_task(self, spec: TaskSpec) -> str:
        child_sink = FnSink(lambda t, c, _sid=spec.id: self._record_event(_sid, t, c))
        agent = Agent(
            self.cfg,
            child_sink,
            system_prompt=L3_SYSTEM if spec.layer == 3 else L2_SYSTEM,
            tool_names=(
                self._child_tool_names
                if self._child_tool_names is not None
                else (tools.L3_TOOL_NAMES if spec.layer == 3 else tools.BASE_TOOL_NAMES)
            ),
            extra_tools=(
                {
                    "spawn": self.bound_spawn(spec.layer),
                    **skills.bound(readonly=not self._skill_save),
                    **self._child_extra_tools,
                }
                if spec.layer == 2
                else {
                    **skills.bound(readonly=not self._skill_save),
                    **self._child_extra_tools,
                }
            ),
            parallel_tools={"spawn"},
            llm=self._llm,
            model=self.cfg.model_for(spec.layer),
            should_abort=self._should_abort,
        )
        messages: list[dict] = [{"role": "user", "content": task_brief(spec)}]
        result = agent.run(messages)
        answer = result.content

        if "JSON" in spec.reply_format.upper():
            attempts = 0
            while not _is_json(answer) and attempts < JSON_RETRIES:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your reply does not satisfy the required reply format "
                            f'("{spec.reply_format}"), which demands valid JSON. '
                            "Reply again with valid JSON only."
                        ),
                    }
                )
                result = agent.run(messages)
                answer = result.content
                attempts += 1
            if not _is_json(answer):
                return f"FAIL: reply is not valid JSON per reply_format. Last reply: {answer[:500]}"

        if not answer.strip():
            return "FAIL: empty reply"
        return answer

    def _record_event(self, spec_id: str, event_type: str, content) -> None:
        """Store the child event for replay, and forward it to the UI stream."""
        record = self.subagents.get(spec_id)
        if record is not None:
            record["events"].append({"type": event_type, "content": content})
        self.sink.emit(
            "agent_event", {"id": spec_id, "event": {"type": event_type, "content": content}}
        )
