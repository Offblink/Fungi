"""Cross-process integration: hub runs in a child process; the test process
joins as two hosts and exchanges chat over real HTTP."""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HUB_SCRIPT = """
import sys
from pathlib import Path
from fungi.hub.app import Hub

hub = Hub("srv", "room-token", Path(sys.argv[1]))
hub.start()
print(hub.port, flush=True)
sys.stdin.readline()  # block until the parent closes stdin
hub.stop()
"""


def _post(base: str, path: str, obj: dict) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def test_two_processes_exchange_chat(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", HUB_SCRIPT, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    try:
        port = int(proc.stdout.readline().strip())
        base = f"http://127.0.0.1:{port}"

        for name in ("alpha", "beta"):
            out = _post(base, "/api/join", {"name": name, "token": "room-token"})
            assert out["ok"] is True

        _post(
            base,
            "/api/send",
            {
                "token": "room-token",
                "envelope": {
                    "v": 1,
                    "src": "alpha:comm-beta",
                    "dst": "beta:comm-alpha",
                    "type": "chat",
                    "body": {"text": "hello from alpha"},
                },
            },
        )

        with urllib.request.urlopen(
            f"{base}/api/poll?host=beta&token=room-token&after=0&timeout=0", timeout=10
        ) as resp:
            polled = json.loads(resp.read())
        assert len(polled["messages"]) == 1
        assert polled["messages"][0]["body"] == {"text": "hello from alpha"}
        assert polled["messages"][0]["src"] == "alpha:comm-beta"
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=10)
    assert proc.returncode == 0
