"""Three-process smoke: alpha (hub) + beta + gamma on 127.0.0.1, one process per host.

Fake (default):
  1. delegate hello : alpha /chat -> delegate(beta) -> beta comm writes
     public/hello.txt -> result -> alpha turn completes.
  2. delegate consent: alpha /chat -> delegate(gamma) -> gamma comm calls
     ask_consent -> card on alpha -> driver answers yes -> gamma writes
     homes/alpha/notes.md -> result.

Real (--real, needs ZAI_API_KEY in config.json): step 1 with natural-language
prompts and real LLMs (deterministic consent flow is fake-only).

Usage:
  python scripts/smoke_fungi.py            # three subprocesses, FakeLLM
  python scripts/smoke_fungi.py --real     # real LLM smoke (step 1 only)

Manual LAN checklist (run on real hosts once this passes):
  1. host A: python -m fungi --server --name A
  2. host B: python -m fungi --join http://A:56287 --token <printed>
  3. tray shows on both; comm clones appear within ~2s (server) / ~10s (client)
  4. ask for a homes/<A>/ write via A's WebUI -> card on A -> allow -> write lands
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fungi.llm import LLMResult  # noqa: E402 (needs sys.path first)

MARKER = "FUNGI_SMOKE"
READY = MARKER + " READY"

from fungi.config import Config, load_config  # noqa: E402 (needs sys.path first)
from fungi.events import NullSink  # noqa: E402
from fungi.room import RoomClient, RoomServer  # noqa: E402

# ── deterministic LLM (fake mode) ──


def tc(name: str, args: dict) -> dict:
    return {
        "id": "t1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class RuleLLM:
    """Deterministic tool-use policy keyed by prompt markers; logs decisions."""

    def __init__(self, label: str):
        self.label = label

    def __call__(self, messages: list[dict], _tool_defs: list[dict]) -> LLMResult:
        users = [m["content"] for m in messages if m["role"] == "user"]
        last = users[-1] if users else ""
        tools = [m["content"] for m in messages if m["role"] == "tool"]
        tail = tools[-1] if tools else ""
        out = self._decide(last, tail)
        action = (
            out.tool_calls[0]["function"]["name"] if out.tool_calls else (out.content or "(empty)")
        )
        print(f"{MARKER} LLM {self.label}: {action}", flush=True)
        return out

    def _decide(self, last: str, tail: str) -> LLMResult:
        # alpha local clone: the two delegation orders
        if "smoke-delegate-hello" in last:
            if not tail:
                return LLMResult(
                    content="",
                    tool_calls=[
                        tc(
                            "delegate",
                            {
                                "host": "beta",
                                "goal": "smoke-write-hello: write file public/hello.txt "
                                "with content 'hello from alpha'",
                            },
                        )
                    ],
                )
            return LLMResult(content=f"HELLO_OK result={tail.strip()[:60]}")
        if "smoke-delegate-consent" in last:
            if not tail:
                return LLMResult(
                    content="",
                    tool_calls=[
                        tc(
                            "delegate",
                            {
                                "host": "gamma",
                                "goal": "smoke-write-consent: write file homes/alpha/notes.md "
                                "with content 'consent notes'",
                            },
                        )
                    ],
                )
            return LLMResult(content=f"CONSENT_OK result={tail.strip()[:60]}")
        # comm clones: task execution
        if "smoke-write-hello" in last:
            if not tail:
                return LLMResult(
                    content="",
                    tool_calls=[
                        tc(
                            "write_file",
                            {"path": "public/hello.txt", "content": "hello from alpha"},
                        )
                    ],
                )
            if "Wrote" in tail:
                return LLMResult(content="hello written")
        if "smoke-write-consent" in last:
            if not tail:
                return LLMResult(
                    content="",
                    tool_calls=[
                        tc(
                            "ask_consent",
                            {
                                "host": "alpha",
                                "action": "write",
                                "path": "homes/alpha/notes.md",
                                "reason": "smoke test",
                            },
                        )
                    ],
                )
            if "USER:" in tail:
                return LLMResult(
                    content="",
                    tool_calls=[
                        tc(
                            "write_file",
                            {"path": "homes/alpha/notes.md", "content": "consent notes"},
                        )
                    ],
                )
            if "Wrote" in tail:
                return LLMResult(content="notes written")
            return LLMResult(content=f"failed: {tail.strip()[:60]}")
        # chat acks and anything unexpected
        return LLMResult(content="ack")


# ── role subprocess ──


def run_role(args: argparse.Namespace) -> int:
    if args.real:
        cfg = load_config()
        if not cfg.configured:
            print(f"{MARKER} FAIL: config.json has no usable api_key for --real", flush=True)
            return 2
        llm = None
    else:
        cfg = Config(api_key="smoke", endpoint="smoke", model="smoke")
        llm = RuleLLM(args.name)

    if args.role == "server":
        room = RoomServer(args.name, cfg, NullSink(), args.token, Path(args.data), llm=llm)
    else:
        room = RoomClient(args.name, cfg, NullSink(), args.server, args.token, llm=llm)
    room.notifier = None  # no Qt in smoke: cards without toasts
    room.start()
    url = room.open_webui(open_browser=False)
    hub_port = room.hub.port if args.role == "server" else 0
    print(f"{READY} name={args.name} webui={url.rsplit(':', 1)[1]} hub={hub_port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        room.stop()
    return 0


# ── driver (parent) ──


class Child:
    def __init__(self, argv: list[str]):
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        self.lines: list[str] = []
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line.rstrip())
            if MARKER in line or "error" in line.lower():
                print(f"  [child] {line.rstrip()}", flush=True)

    def wait_for(self, prefix: str, timeout_s: float = 30.0) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for line in self.lines:
                if line.startswith(prefix):
                    return line
            if self.proc.poll() is not None:
                raise RuntimeError(f"child exited early: {self.lines[-5:]}")
            time.sleep(0.1)
        raise RuntimeError(f"timeout waiting for {prefix!r}: {self.lines[-5:]}")


def post_json(url: str, payload: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def chat(url: str, message: str, timeout: float = 120.0) -> list[dict]:
    req = urllib.request.Request(
        url + "/chat",
        data=json.dumps({"message": message}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in resp:
            events.append(json.loads(line))
            if events[-1].get("type") == "done":
                break
    return events


def stream_text(events: list[dict]) -> str:
    return "".join(e["content"] for e in events if e.get("type") == "text")


def final_text(events: list[dict], base: str) -> str:
    """Streamed text; fake-LLM turns emit none, so fall back to the session."""
    streamed = stream_text(events)
    if streamed.strip():
        return streamed
    sid = next((e["content"] for e in events if e.get("type") == "sessionId"), None)
    if not sid:
        return ""
    with urllib.request.urlopen(f"{base}/session?id={sid}", timeout=10) as resp:
        data = json.loads(resp.read())
    assistant = [
        m["content"]
        for m in data.get("messages", [])
        if m.get("role") == "assistant" and m.get("content")
    ]
    return assistant[-1] if assistant else ""


def assert_clean(events: list[dict], what: str) -> str:
    text = stream_text(events)
    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, f"{what}: stream errors {errors}"
    assert "FAIL" not in text and "ERROR" not in text, f"{what}: {text!r}"
    return text


def drive(args: argparse.Namespace) -> int:
    data = Path(args.data) if args.data else Path(tempfile.mkdtemp(prefix="fungi-smoke-"))
    token = "smoke-token"
    argv = [sys.executable, str(Path(__file__).resolve())]
    print(f"{MARKER} data={data}", flush=True)
    server = Child(
        [
            *argv,
            "--role",
            "server",
            "--name",
            "alpha",
            "--token",
            token,
            "--data",
            str(data),
            *(["--real"] if args.real else []),
        ]
    )
    children = [server]
    try:
        line = server.wait_for(READY)
        hub_port = int(line.split("hub=")[1])
        webui_alpha = line.split("webui=")[1].split()[0]
        common = [
            "--token",
            token,
            "--server",
            f"http://127.0.0.1:{hub_port}",
            *(["--real"] if args.real else ["--fake"]),
        ]
        beta = Child([*argv, "--role", "client", "--name", "beta", *common])
        gamma = Child([*argv, "--role", "client", "--name", "gamma", *common])
        children += [beta, gamma]
        beta.wait_for(READY)
        gamma.wait_for(READY)
        print(f"{MARKER} room formed; waiting for roster propagation", flush=True)
        time.sleep(6)
        base = f"http://localhost:{webui_alpha}"

        # step 1: delegate -> beta comm -> public/hello.txt
        print(f"{MARKER} step1: delegate hello", flush=True)
        if args.real:
            # LLM-facing Chinese prompt; fullwidth punctuation is intentional
            message = "请调用 delegate 工具把任务交给 host beta：在 public/ 目录写文件 hello.txt，内容为 hello from alpha。完成后把结果简短转述给我。"  # noqa: RUF001
        else:
            message = "smoke-delegate-hello"
        events = chat(base, message, timeout=180.0)
        assert_clean(events, "step1")
        text = final_text(events, base)
        if not args.real:
            assert "HELLO_OK" in text, f"step1: {text!r}"
        hello = data / "public" / "hello.txt"
        assert hello.is_file(), "public/hello.txt was never written"
        assert "hello" in hello.read_text(encoding="utf-8").lower()
        print(f"{MARKER} step1 OK", flush=True)

        if not args.real:
            # step 2: delegate -> gamma comm -> ask_consent -> card -> yes -> homes write
            print(f"{MARKER} step2: delegate consent", flush=True)
            result: list = []

            def _run_step2() -> None:
                result.append(chat(base, "smoke-delegate-consent"))

            reader = threading.Thread(target=_run_step2, daemon=True)
            reader.start()
            answered = False
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and not answered:
                time.sleep(0.5)
                asks = json.loads(urllib.request.urlopen(base + "/asks", timeout=5).read())["asks"]
                for card in asks:
                    question = card["questions"][0]["question"]
                    if card.get("kind") == "consent" and "homes/alpha/notes.md" in question:
                        out = post_json(base + "/answer", {"id": card["id"], "value": "yes"})
                        assert out.get("ok"), f"answer rejected: {out}"
                        answered = True
            reader.join(timeout=120)
            assert answered, "consent card never appeared on alpha"
            events = result[0]
            assert_clean(events, "step2")
            text = final_text(events, base)
            assert "CONSENT_OK" in text, f"step2: {text!r}"
            notes = data / "homes" / "alpha" / "notes.md"
            assert notes.is_file(), "homes/alpha/notes.md was never written"
            assert "consent notes" in notes.read_text(encoding="utf-8")
            print(f"{MARKER} step2 OK", flush=True)

        for child in children:
            for line in child.lines:
                assert "turn failed" not in line.lower(), f"child error: {line}"
        print(f"{MARKER} OK", flush=True)
        return 0
    except Exception as exc:
        print(f"{MARKER} FAIL: {exc}", flush=True)
        return 1
    finally:
        for child in children:
            child.proc.terminate()
        if args.keep:
            print(f"{MARKER} data kept at {data}", flush=True)
        else:
            shutil.rmtree(data, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fungi three-process smoke")
    parser.add_argument("--role", choices=("server", "client"), help="run one host process")
    parser.add_argument("--name", default="alpha")
    parser.add_argument("--token", default="smoke-token")
    parser.add_argument("--server", help="hub URL (client role)")
    parser.add_argument("--data", help="server data directory (server role / driver)")
    parser.add_argument("--real", action="store_true", help="real LLM via config.json")
    parser.add_argument("--fake", dest="real", action="store_false")
    parser.add_argument("--keep", action="store_true", help="keep data dir for debugging")
    args = parser.parse_args()
    if args.role:
        return run_role(args)
    return drive(args)


if __name__ == "__main__":
    raise SystemExit(main())
