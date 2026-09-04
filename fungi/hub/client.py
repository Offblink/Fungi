"""HTTP client for the hub API: room ops + fs + sessions + transfers."""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..protocol import Envelope, ProtocolError, deserialize

POLL_CAP = 25.0


class HubError(Exception):
    pass


class HubClient:
    def __init__(self, base_url: str, token: str, host: str, display: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token
        self.host = host
        self.display = display

    # ── plumbing ──

    def _request(self, method: str, path: str, obj: dict | None = None) -> dict:
        url = self.base + path
        data = None
        if obj is not None:
            obj = {"token": self.token, **obj}
            data = json.dumps(obj).encode("utf-8")
        elif method == "GET":
            sep = "&" if "?" in path else "?"
            url = f"{url}{sep}token={self.token}"
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", body)
            except json.JSONDecodeError:
                detail = body
            raise HubError(f"{path}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HubError(f"{path}: {exc}") from exc

    # ── room ops ──

    def join(self) -> dict:
        return self._request("POST", "/api/join", {"name": self.host, "display": self.display})

    def leave(self) -> dict:
        return self._request("POST", "/api/leave", {"name": self.host})

    def heartbeat(self) -> dict:
        return self._request("POST", "/api/heartbeat", {"name": self.host})

    def send(self, env: Envelope) -> dict:
        return self._request("POST", "/api/send", {"envelope": env.serialize()})

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:
        timeout = min(timeout, POLL_CAP)
        out = self._request("GET", f"/api/poll?host={self.host}&after={after}&timeout={timeout}")
        messages = []
        for raw in out.get("messages", []):
            try:
                messages.append(deserialize(raw))
            except ProtocolError:
                continue
        return messages, int(out.get("cursor", after))

    # ── fs ops (path guard lives on the server) ──

    def fs(
        self,
        op: str,
        path: str,
        *,
        content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        pattern: str | None = None,
        consent_id: str | None = None,
    ) -> dict:
        body: dict = {"host": self.host, "path": path, "consent_id": consent_id}
        if content is not None:
            body["content"] = content
        if old_string is not None:
            body["old_string"] = old_string
        if new_string is not None:
            body["new_string"] = new_string
        if pattern is not None:
            body["pattern"] = pattern
        return self._request("POST", f"/api/fs/{op}", body)

    # ── sessions ──

    def list_sessions(self) -> list[dict]:
        return self._request("GET", "/api/sessions").get("sessions", [])

    def load_session(self, session_id: str) -> dict:
        return self._request("GET", f"/api/session?id={session_id}")

    def save_session(self, payload: dict) -> dict:
        return self._request("POST", "/api/save", payload)

    def delete_session(self, session_id: str) -> dict:
        return self._request("POST", "/api/session/delete", {"id": session_id})

    # ── peers / comm log ──

    def peers(self) -> list[dict]:
        """Peers as display records ``[{"name", "display"}]``."""
        return self._request("GET", f"/api/peers?host={self.host}").get("peers", [])

    def comm_log(self, other: str) -> list[dict]:
        out = self._request("GET", f"/api/comm-log?host={self.host}&with={other}")
        return out.get("messages", [])

    # ── file transfers (bytes live on the hub; only metadata is exchanged) ──

    def create_transfer(self, path: str, name: str, to_host: str) -> dict:
        return self._request(
            "POST", "/api/transfer", {"host": self.host, "path": path, "name": name, "to": to_host}
        )

    def download_transfer(self, transfer_id: str, dest) -> None:
        """Stream a staged transfer to a local file path."""
        url = f"{self.base}/api/transfer?id={transfer_id}&host={self.host}&token={self.token}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as resp, Path(dest).open("wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)

    def upload_transfer(self, path: str, name: str, to_host: str) -> dict:
        """Stream a local file's raw bytes to the hub staging area."""
        src = Path(path)
        u = urllib.parse.urlparse(self.base)
        q = urllib.parse.urlencode(
            {"token": self.token, "host": self.host, "to": to_host, "name": name}
        )
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=600)
        try:
            with src.open("rb") as fh:
                conn.putrequest("POST", f"/api/transfer/upload?{q}")
                conn.putheader("Content-Type", "application/octet-stream")
                conn.putheader("Content-Length", str(src.stat().st_size))
                conn.endheaders()
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        try:
            out = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return {"error": f"upload failed: HTTP {resp.status}"}
        if resp.status != 200 and "error" not in out:
            out = {"error": f"upload failed: HTTP {resp.status}"}
        return out
