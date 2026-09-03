"""Shared fixtures: a real hub room with a HubClient-shaped urllib client."""

import json
import urllib.error
import urllib.request

import pytest

from fungi.hub.app import Hub
from fungi.protocol import Envelope, deserialize


class Client:
    """HubClient-compatible surface over raw urllib (no dependency on fungi.hub.client).

    Raw methods (post/get/poll_raw) return (status_code, body_dict) for API-level
    assertions; the HubClient-shaped methods (send/poll/fs/join) return parsed payloads.
    """

    def __init__(self, base: str, token: str, host: str):
        self.base = base.rstrip("/")
        self.token = token
        self.host = host

    # ── raw HTTP ──

    def post(self, path: str, obj: dict) -> tuple[int, dict]:
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=40) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def poll_raw(self, host: str, after: int = 0, timeout: float = 0.0) -> tuple[int, dict]:
        return self.get(f"/api/poll?host={host}&token={self.token}&after={after}&timeout={timeout}")

    # ── HubClient-compatible surface ──

    def join(self) -> dict:
        return self.post("/api/join", {"name": self.host, "token": self.token})[1]

    def send(self, env) -> dict:
        return self.post("/api/send", {"token": self.token, "envelope": env.serialize()})[1]

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:
        _code, out = self.poll_raw(self.host, after, timeout)
        return [deserialize(m) for m in out["messages"]], out["cursor"]

    def fs(self, op: str, path: str, **kw) -> dict:
        body = {"token": self.token, "host": self.host, "path": path, **kw}
        return self.post(f"/api/fs/{op}", body)[1]

    def poll_env(
        self, host: str, after: int = 0, timeout: float = 0.0
    ) -> tuple[list[Envelope], int]:
        """Poll another host's buffer (test-side observation)."""
        _code, out = self.poll_raw(host, after, timeout)
        return [deserialize(m) for m in out["messages"]], out["cursor"]


@pytest.fixture()
def room(tmp_path):
    hub = Hub("srv", "room-token", tmp_path)
    hub.start()
    names = ("alpha", "beta", "srv")
    clients = {n: Client(f"http://127.0.0.1:{hub.port}", "room-token", n) for n in names}
    yield hub, clients
    hub.stop()
