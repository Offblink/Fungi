"""confirm tool (L1 only): the Inquire mechanism.

Emits an `ask` event on the turn sink, then blocks the calling agent thread
until the UI answers via POST /answer (resolve_ask) or the timeout expires.
Supports one question (question/options/allow_custom) or a question list
(questions: [{question, options?, allow_custom?}]); every completed ask is
reported via the on_answer callback for session persistence.
"""

import time
import uuid
from collections.abc import Callable

from fungi.agent import BoundTool
from fungi.events import Sink
from fungi.pending import PendingAsks

ASK_TIMEOUT_S = 900
HEARTBEAT_S = 15

ASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "confirm",
        "description": (
            "Ask the user one or several questions and wait for their answers. Only the"
            " orchestrator can ask; use it when a decision genuinely needs the user (approach"
            " choice, confirmation before something hard to undo). Use `question` for a single"
            " question or `questions` for a list shown on one form. Returns 'USER: <answer>'"
            " (numbered lines for several questions)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask (single-question form)",
                },
                "options": {
                    "type": "array",
                    "description": "Optional choices the user can pick from (single-question form)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["label"],
                    },
                },
                "allow_custom": {
                    "type": "boolean",
                    "description": "Whether the user may type a free-form answer (default true)",
                },
                "questions": {
                    "type": "array",
                    "description": "Several questions on one form (overrides `question`)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label"],
                                },
                            },
                            "allow_custom": {"type": "boolean"},
                        },
                        "required": ["question"],
                    },
                },
            },
            "required": ["question"],
        },
    },
}

# Ask registry shared by every confirm tool instance; /answer resolves in-process.
_pending = PendingAsks()


def resolve_ask(ask_id: str, value: str | list[str]) -> bool:
    """Wake a pending ask (called by POST /answer). False for unknown/expired ids."""
    return _pending.resolve(ask_id, value)


def _normalize_options(options) -> list[dict]:
    if not isinstance(options, list):
        return []
    out: list[dict] = []
    for opt in options:
        if isinstance(opt, str):
            out.append({"label": opt})
        elif isinstance(opt, dict) and str(opt.get("label") or "").strip():
            item = {"label": str(opt["label"])}
            if opt.get("description"):
                item["description"] = str(opt["description"])
            out.append(item)
    return out


def _normalize_questions(args: dict) -> list[dict]:
    """[{question, options: [{label, description?}], allow_custom}] from tool args."""
    top_allow = bool(args.get("allow_custom", True))
    items: list[dict] = []
    raw = args.get("questions")
    if isinstance(raw, list) and raw:
        for q in raw:
            if isinstance(q, str):
                items.append({"question": q.strip(), "options": [], "allow_custom": top_allow})
            elif isinstance(q, dict) and str(q.get("question") or "").strip():
                items.append(
                    {
                        "question": str(q["question"]).strip(),
                        "options": _normalize_options(q.get("options")),
                        "allow_custom": bool(q.get("allow_custom", top_allow)),
                    }
                )
        return items
    question = str(args.get("question") or "").strip()
    if question:
        items.append(
            {
                "question": question,
                "options": _normalize_options(args.get("options")),
                "allow_custom": top_allow,
            }
        )
    return items


def _format_answer(value: str | list[str]) -> str:
    if isinstance(value, list):
        return "USER:\n" + "\n".join(f"{i}. {a}" for i, a in enumerate(value, start=1))
    return f"USER: {value}"


def make_ask_tool(
    sink: Sink,
    on_answer: Callable[[dict], None] | None = None,
    should_abort: Callable[[], bool] | None = None,
    notify: Callable[[str], None] | None = None,
) -> BoundTool:
    """Build the confirm BoundTool bound to one turn's sink.

    `on_answer(record)` is called once per completed ask with
    {id, questions, answers, status: answered|timeout|aborted} for
    persistence. `should_abort` lets a stopped turn wake the blocked tool
    instead of holding the session for the full ASK_TIMEOUT_S (15 min).
    `notify(summary)` fires once when the card is raised — callers use it
    for a system notification when nobody has the WebUI open (the card
    alone is invisible there, and the turn blocks up to 15 minutes).
    """

    def ask(args: dict) -> str:
        questions = _normalize_questions(args)
        if not questions:
            return "ERROR: Missing required argument: question"
        ask_id = uuid.uuid4().hex[:6]
        _pending.register(ask_id)
        sink.emit("ask", {"id": ask_id, "questions": questions})
        if notify is not None:
            notify(str(questions[0].get("question") or "(no question text)"))
        answered = False
        value = None
        aborted = False
        deadline = time.monotonic() + ASK_TIMEOUT_S
        try:
            # Slice the wait so a stop request wakes the tool within ~1s;
            # heartbeat pings keep the NDJSON stream alive while blocked.
            last_ping = 0.0
            while True:
                if should_abort is not None and should_abort():
                    aborted = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                answered, value = _pending.wait(ask_id, timeout_s=min(1.0, remaining))
                if answered:
                    break
                now = time.monotonic()
                if now - last_ping >= HEARTBEAT_S:
                    sink.emit("ping", None)
                    last_ping = now
        finally:
            _pending.discard(ask_id)
        status = "answered" if answered else ("aborted" if aborted else "timeout")
        record = {"id": ask_id, "questions": questions, "answers": value if answered else None, "status": status}
        if on_answer is not None:
            on_answer(record)
        if not answered:
            return "ERROR: 回合已被停止，用户未回答" if aborted else "ERROR: 用户未回答"
        return _format_answer(value)

    return BoundTool(schema=ASK_SCHEMA, fn=ask)
