"""WebUI LAN gate: loopback passes, non-loopback needs the QR token; /lan
hands out the token only to loopback callers."""

import json
import threading
import urllib.request

from fungi.server import WEBUI_TOKEN, YesSirHandler, lan_payload, make_webui_server


def _fake_handler(client_ip: str, path: str) -> YesSirHandler:
    """Bare handler instance (no socket): enough for the pure _authorized logic."""
    h = YesSirHandler.__new__(YesSirHandler)
    h.client_address = (client_ip, 12345)
    h.path = path
    return h


def test_loopback_passes_without_token():
    assert YesSirHandler._authorized(_fake_handler("127.0.0.1", "/sessions")) is True


def test_lan_client_without_token_is_rejected():
    assert YesSirHandler._authorized(_fake_handler("192.168.1.7", "/sessions")) is False


def test_lan_client_with_wrong_token_is_rejected():
    assert (
        YesSirHandler._authorized(_fake_handler("192.168.1.7", "/sessions?t=nope"))
        is False
    )


def test_lan_client_with_qr_token_passes():
    assert (
        YesSirHandler._authorized(
            _fake_handler("192.168.1.7", f"/sessions?t={WEBUI_TOKEN}")
        )
        is True
    )


def test_lan_payload_hides_token_from_lan_callers():
    loopback = lan_payload(8900, loopback=True)
    assert loopback["port"] == 8900
    assert loopback["token"] == WEBUI_TOKEN
    assert loopback["url"] == f"http://{loopback['ip']}:8900/m?t={WEBUI_TOKEN}"

    remote = lan_payload(8900, loopback=False)
    assert "token" not in remote
    assert "url" not in remote
    assert remote["ip"] == loopback["ip"]


class _TouchRuntime:
    """/lan only needs the handler's touch() hook."""

    def touch(self) -> None:
        pass


def test_webui_binds_all_interfaces_and_lan_endpoint_works():
    server = make_webui_server(0, _TouchRuntime())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert server.server_address[0] == "0.0.0.0"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/lan", timeout=5
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["port"] == server.server_address[1]
        assert payload["ip"] not in ("", None)
        assert payload["token"] == WEBUI_TOKEN
        assert payload["url"].endswith(f"/m?t={WEBUI_TOKEN}")
    finally:
        server.shutdown()
        server.server_close()
