"""GUI launcher smoke: three pages construct offscreen; validation logic holds."""

import os
import pytest
import time

pytest.importorskip("qfluentwidgets", reason="PyQt6-Fluent-Widgets (qfluentwidgets) not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QSharedMemory
from PyQt5.QtNetwork import QLocalSocket
from PyQt5.QtWidgets import QApplication

from fungi import gui
from fungi.gui import FungiGui, valid_host_name


@pytest.fixture(scope="module")
def window(qapp):  # noqa: ARG001 (Qt app fixture)
    win = FungiGui()
    yield win
    win.close()


class FakeRoom:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _fresh_pages(window):
    """Module-scoped window: reset mutable page state between tests."""
    yield
    window.host_page.room = None
    window.host_page.ip_edit.clear()
    window.host_page.token_edit.clear()
    window.host_page._set_started(False)
    window.join_page.room = None
    window.join_page.join_btn.setEnabled(True)
    window.join_page.leave_btn.setVisible(False)


def test_three_pages_present(window):
    assert window.host_page.objectName() == "hostPage"
    assert window.join_page.objectName() == "joinPage"
    assert window.mobile_page.objectName() == "mobilePage"
    assert window.cfg_page.objectName() == "configPage"
    # status card hidden until the room is launched
    assert not window.host_page.ip_row.isVisibleTo(window.host_page)
    window.host_page._set_started(True)
    assert window.host_page.ip_row.isVisibleTo(window.host_page)
    assert not window.host_page.start_btn.isEnabled()
    window.host_page._set_started(False)


def test_mobile_page_renders_qr_for_running_room(window):
    from fungi.server import WEBUI_TOKEN

    page = window.mobile_page
    # no room yet: placeholder text, empty URL field, no QR pixmap
    assert page.url_edit.text() == ""
    assert page.qr_label.pixmap() is None or page.qr_label.pixmap().isNull()

    class FakeWebRoom:
        def open_webui(self, open_browser=True):
            return "http://localhost:12345"

    window.host_page.room = FakeWebRoom()
    try:
        page.refresh()
        assert page.url_edit.text() == (
            f"http://{gui.lan_ip()}:12345/m?t={WEBUI_TOKEN}"
        )
        pm = page.qr_label.pixmap()
        assert pm is not None and not pm.isNull()
    finally:
        window.host_page.room = None
        page.refresh()
    assert page.url_edit.text() == ""


def test_host_page_starts_server_in_process(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(host, display, token, port):
        started.append((host, display, token, port))
        return object()

    monkeypatch.setattr(gui, "start_server_room", fake_start)
    page.name_edit.setText("pc-alpha")
    page.nick_edit.setText("🌸花酱")
    page._start()
    host, display, token, port = started[0]
    assert host == "pc-alpha" and display == "🌸花酱"
    assert token and len(token) >= 16
    assert gui.GUI_PORT <= port < gui.GUI_PORT + gui.PORT_SCAN_LIMIT  # upward scan
    assert page.room is not None and not page.start_btn.isEnabled()
    page.room = None  # FungiGui.closeEvent must not stop a fake


def test_host_page_self_heals_pure_cjk_name(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(host, display, token, port):  # noqa: ARG001 (fakes ignore token/port)
        started.append((host, display))
        return FakeRoom()

    monkeypatch.setattr(gui, "start_server_room", fake_start)
    page.name_edit.setText("小新")  # pure CJK: nothing sanitizable
    page.nick_edit.setText("")
    page._start()
    wire = page.name_edit.text()
    assert gui.valid_host_name(wire) and wire != "小新"
    assert page.nick_edit.text() == "小新"  # the pretty input became the nickname
    assert started == [(wire, "小新")]
    page.room = None


def test_host_page_sanitizes_mixed_name(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(host, display, token, port):  # noqa: ARG001 (fakes ignore token/port)
        started.append((host, display))
        return FakeRoom()

    monkeypatch.setattr(gui, "start_server_room", fake_start)
    page.name_edit.setText("pc 阿新!")
    page.nick_edit.setText("🌸花酱")  # already set: must not be overwritten
    page._start()
    assert started == [("pc", "🌸花酱")]  # sanitized wire, nickname untouched
    assert page.name_edit.text() == "pc"
    page.room = None


def test_host_page_refreshes_ip(window, monkeypatch):
    page = window.host_page
    page.ip_edit.setText("192.168.1.10")
    monkeypatch.setattr(gui, "lan_ip", lambda: "10.0.0.9")
    page._refresh_ip()
    assert page.ip_edit.text() == "10.0.0.9"
    page._refresh_ip()  # unchanged: must not raise
    assert page.ip_edit.text() == "10.0.0.9"


def test_host_page_leave_stops_room(window):
    page = window.host_page
    room = FakeRoom()
    page.room = room
    page._set_started(True)
    page._leave()
    assert room.stopped and page.room is None
    assert page.start_btn.isEnabled() and not page.leave_btn.isVisibleTo(page)
    assert page.ip_edit.text() == ""  # status card reset


def test_host_page_leave_without_room_is_noop(window):
    window.host_page._leave()  # must not raise


def test_host_page_start_uses_custom_token(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(_host, _display, token, _port):
        started.append(token)
        return object()

    monkeypatch.setattr(gui, "start_server_room", fake_start)
    page.name_edit.setText("pc-alpha")
    page.token_edit.setText("my-token_1")
    page._start()
    assert started == ["my-token_1"]
    assert page.token_edit.text() == "my-token_1" and page._token == "my-token_1"
    page.room = None


def test_host_page_start_rejects_invalid_token(window):
    page = window.host_page
    page.name_edit.setText("pc-alpha")
    page.token_edit.setText("bad token 中文")  # token rides URLs: spaces/CJK invalid
    page._start()
    assert page.room is None and page.start_btn.isEnabled()


def test_host_page_token_hot_update(window):
    page = window.host_page

    class FakeHub:
        token = "old-token-12"

    page.room = FakeRoom()
    page.room.hub = FakeHub()
    page._token = "old-token-12"
    page.token_edit.setText("new-token-99")
    page._apply_token()
    assert page.room.hub.token == "new-token-99" and page._token == "new-token-99"
    # invalid edit reverts the field and keeps the live token
    page.token_edit.setText("不合法 token")
    page._apply_token()
    assert page.token_edit.text() == "new-token-99"
    assert page.room.hub.token == "new-token-99"
    page.room = None


def test_join_page_leave_stops_room(window):
    page = window.join_page
    room = FakeRoom()
    page.room = room
    page.leave_btn.setVisible(True)
    page._leave()  # stop() sends leave to the hub
    assert room.stopped and page.room is None
    assert page.join_btn.isEnabled() and not page.leave_btn.isVisibleTo(page)


def test_join_page_requires_token(window):
    page = window.join_page
    page.token_edit.setText("")
    before = page.settings.value("last_token", "")
    page._join()  # empty token: warns and returns without spawning
    assert page.settings.value("last_token", "") == before


def test_join_page_discovers_and_joins_in_process(window, monkeypatch):
    joined = []

    def fake_discover(token):
        assert token == "tok"
        return ("192.168.1.20", gui.GUI_PORT + 3)

    def fake_client(host, display, url, token):
        joined.append((host, display, url, token))
        return FakeRoom()

    monkeypatch.setattr(gui, "discover_room", fake_discover)
    monkeypatch.setattr(gui, "start_client_room", fake_client)
    page = window.join_page
    page.ip_edit.clear()  # a real join on this box may have restored last_ip from QSettings
    page.token_edit.setText("tok")  # IP left empty: auto-discovery fills it
    page.nick_edit.setText("🌸花酱")
    page.name_edit.setText("pc-alpha")
    page._join()  # discovery runs on a thread; results arrive via signals
    for _ in range(200):
        QApplication.processEvents()
        if joined:
            break
    assert page.ip_edit.text() == "192.168.1.20"  # discovered IP visible in the field
    assert joined == [("pc-alpha", "🌸花酱", f"http://192.168.1.20:{gui.GUI_PORT + 3}", "tok")]
    assert page.settings.value("last_token") == "tok"
    for key in ("last_ip", "last_token", "last_nick"):  # don't leak into user QSettings
        page.settings.remove(key)


def test_join_page_uses_typed_ip(window, monkeypatch):
    joined = []

    def fake_probe(ip, token, start=gui.GUI_PORT, limit=gui.PORT_SCAN_LIMIT):  # noqa: ARG001
        assert ip == "192.168.1.20" and token == "tok"
        return gui.GUI_PORT + 3

    def fake_client(host, display, url, token):
        joined.append((host, display, url, token))
        return FakeRoom()

    monkeypatch.setattr(gui, "probe_room_port", fake_probe)
    monkeypatch.setattr(gui, "start_client_room", fake_client)
    page = window.join_page
    page.ip_edit.setText("192.168.1.20")  # typed/refilled IP: no subnet sweep
    page.token_edit.setText("tok")
    page.nick_edit.setText("🌸花酱")
    page.name_edit.setText("pc-alpha")
    page._join()
    for _ in range(200):
        QApplication.processEvents()
        if joined:
            break
    assert joined == [("pc-alpha", "🌸花酱", "http://192.168.1.20:8902", "tok")]
    for key in ("last_ip", "last_token", "last_nick"):
        page.settings.remove(key)


def test_local_subnet_hosts_covers_self(monkeypatch):
    monkeypatch.setattr(gui, "lan_ip", lambda: "192.168.0.104")
    hosts = gui.local_subnet_hosts()
    assert len(hosts) == 254 and hosts[0] == "192.168.0.1" and "192.168.0.104" in hosts


def test_discover_room_finds_matching_host(monkeypatch):
    monkeypatch.setattr(gui, "local_subnet_hosts", lambda: ["10.0.0.1", "10.0.0.2"])
    monkeypatch.setattr(
        gui,
        "_port_open",
        lambda ip, port, timeout=gui.SWEEP_TIMEOUT: (  # noqa: ARG005
            (ip, port) == ("10.0.0.2", gui.GUI_PORT)
        ),
    )
    monkeypatch.setattr(
        gui,
        "_room_accepts",
        lambda ip, port, token: (ip, port) == ("10.0.0.2", gui.GUI_PORT),  # noqa: ARG005
    )
    assert gui.discover_room("tok") == ("10.0.0.2", gui.GUI_PORT)


def test_discover_room_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(gui, "local_subnet_hosts", lambda: ["10.0.0.1"])
    monkeypatch.setattr(
        gui,
        "_port_open",
        lambda ip, port, timeout=gui.SWEEP_TIMEOUT: False,  # noqa: ARG005
    )
    assert gui.discover_room("tok") is None


def test_join_page_webui_button_lifecycle(window, monkeypatch):
    page = window.join_page
    rooms = []

    def fake_client(host, display, url, token):  # noqa: ARG001 (fakes ignore args)
        rooms.append(FakeRoom())
        return rooms[-1]

    monkeypatch.setattr(gui, "probe_room_port", lambda *a, **k: gui.GUI_PORT + 3)  # noqa: ARG005
    monkeypatch.setattr(gui, "start_client_room", fake_client)
    page.ip_edit.setText("192.168.1.20")
    page.token_edit.setText("tok")
    page._join()
    for _ in range(200):
        QApplication.processEvents()
        if rooms:
            break
    assert page.webui_row.isVisibleTo(page)  # joined: own WebUI is openable
    page._leave()
    assert rooms[0].stopped
    assert not page.webui_row.isVisibleTo(page)
    for key in ("last_ip", "last_token", "last_nick"):  # don't leak into user QSettings
        page.settings.remove(key)


def test_join_page_reports_scan_miss(window, monkeypatch):
    monkeypatch.setattr(gui, "discover_room", lambda *_a, **_k: None)
    monkeypatch.setattr(gui, "start_client_room", lambda *_: pytest.fail("must not join"))
    page = window.join_page
    page.ip_edit.clear()  # module-scoped window: drop any leftover from other tests
    page.token_edit.setText("tok")  # empty IP: full subnet discovery
    page.name_edit.setText("pc-alpha")
    page._join()
    for _ in range(200):
        QApplication.processEvents()
        if page.join_btn.isEnabled():
            break
    assert page.room is None
    assert page.ip_edit.text() == ""  # nothing discovered, nothing filled
    assert "没有找到" in page.status.text()


def test_join_page_refresh_fills_ip(window, monkeypatch):
    page = window.join_page
    monkeypatch.setattr(gui, "discover_room", lambda *_a, **_k: ("10.0.0.9", gui.GUI_PORT))
    page.token_edit.setText("tok")
    page._refresh_ip()
    for _ in range(200):
        QApplication.processEvents()
        if page.ip_edit.text() == "10.0.0.9":
            break
    assert page.ip_edit.text() == "10.0.0.9"
    assert page.ip_refresh_btn.isEnabled()


def test_close_parks_room_to_tray(window):
    page = window.host_page
    room = FakeRoom()
    page.room = room
    window._tray = None
    window.close()
    assert not room.stopped  # close hides to tray; the room keeps running
    assert not window.isVisible()
    window._tray.hide()
    page.room = None


def test_quit_from_tray_stops_rooms(window):
    page = window.host_page
    room = FakeRoom()
    page.room = room
    window.quit_from_tray()
    assert room.stopped and page.room is None


def test_valid_host_name_contract():
    assert valid_host_name("pc-alpha")
    assert not valid_host_name("不合法/名字")


def test_gui_singleton_guard():
    key = "FungiGuiSingletonTest"

    shared = QSharedMemory(key)
    assert shared.create(1)
    assert gui._singleton_taken(key)
    shared.detach()
    assert not gui._singleton_taken(key)


def test_second_launch_activates_existing_window(window, monkeypatch):

    calls = []
    monkeypatch.setattr(window, "show_and_raise", lambda: calls.append(1))
    sock = QLocalSocket()
    sock.connectToServer(gui._GUI_IPC)
    assert sock.waitForConnected(1000)
    sock.write(b"show")
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    for _ in range(200):
        QApplication.processEvents()
        if calls:
            break
    assert calls


def _ready(**overrides):
    return {
        "ffmpeg": True, "huggingface_hub": True, "torch": True,
        "transformers": True, "faster_whisper": True, "opencv": True,
        "CLIP": True, "whisper": True, **overrides,
    }


def test_config_page_video_models_all_present_disables_download(
    window, monkeypatch
):
    """运行时全就绪 -> 按钮禁用, 状态行打勾 (不缺失禁用下载)."""
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui._video_ready", lambda: _ready())
    page._check_video_models()
    assert "✓" in page.video_status.text()
    assert "✗" not in page.video_status.text()
    assert not page.download_btn.isEnabled()


def test_config_page_video_models_missing_enables_download(window, monkeypatch):
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui._video_ready", lambda: _ready(CLIP=False))
    page._check_video_models()
    assert "✗" in page.video_status.text() and "CLIP" in page.video_status.text()
    assert page.download_btn.isEnabled()


def test_config_page_missing_dep_enables_download(window, monkeypatch):
    """模型都在但 huggingface_hub 没了 -> 状态行标 ✗, 按钮启用(可自愈)."""
    page = window.cfg_page
    monkeypatch.setattr(
        "fungi.gui._video_ready", lambda: _ready(huggingface_hub=False)
    )
    page._check_video_models()
    assert "huggingface_hub" in page.video_status.text()
    assert "✗" in page.video_status.text()
    assert page.download_btn.isEnabled()


def test_config_page_non_healable_missing_disables_download(window, monkeypatch):
    """torch/ffmpeg 缺失不可自愈: 状态行提示手动装, 按钮不给下载."""
    page = window.cfg_page
    monkeypatch.setattr(
        "fungi.gui._video_ready", lambda: _ready(torch=False, ffmpeg=False)
    )
    page._check_video_models()
    assert "torch" in page.video_status.text() and "手动安装" in page.video_status.text()
    assert not page.download_btn.isEnabled()


def test_config_page_download_runs_script_and_rechecks(window, monkeypatch):
    """点下载 -> Popen 脚本 + 轮询结束后自动复检并恢复按钮可用性。"""
    page = window.cfg_page
    monkeypatch.setattr(
        "fungi.gui._video_ready", lambda: _ready(CLIP=False, whisper=False)
    )
    monkeypatch.setattr("fungi.gui._hf_hub_missing", lambda: False)
    page._check_video_models()

    spawned = []

    class FakeProc:
        returncode = 0

        def poll(self):
            return 0  # "finished" on first tick

    monkeypatch.setattr(
        gui.subprocess,
        "Popen",
        lambda argv, **_kw: spawned.append(argv) or FakeProc(),
    )
    page._download_models()
    assert page._dl_proc is not None and not page.download_btn.isEnabled()
    assert len(spawned) == 1 and spawned[0][-1].endswith("download_video_models.py")

    # 下载结束后的复检, 两个模型都已就绪
    monkeypatch.setattr(
        "fungi.gui._video_ready", lambda: _ready()
    )
    page._poll_download()
    assert page._dl_proc is None
    assert not page._dl_timer.isActive()
    assert "✓" in page.video_status.text()
    assert not page.download_btn.isEnabled()  # 全部就绪 -> 禁用


def test_config_page_download_installs_missing_dep_first(window, monkeypatch):
    """缺 huggingface_hub: 先 pip 装依赖, 成功后自动接下载脚本, 全程一次点击。"""
    page = window.cfg_page
    monkeypatch.setattr(
        "fungi.gui._video_ready", lambda: _ready(CLIP=False, whisper=False)
    )
    monkeypatch.setattr("fungi.gui._hf_hub_missing", lambda: True)

    spawned = []

    class FakeProc:
        returncode = 0

        def poll(self):
            return 0  # 每步首个轮询 tick 即"完成"

    monkeypatch.setattr(
        gui.subprocess,
        "Popen",
        lambda argv, **_kw: spawned.append(argv) or FakeProc(),
    )
    page._download_models()
    assert spawned[0][1:4] == ["-m", "pip", "install"] and "huggingface_hub" in spawned[0]
    assert "依赖" in page.video_status.text()
    page._poll_download()  # 依赖装完 -> 链到模型下载
    assert len(spawned) == 2 and spawned[1][-1].endswith("download_video_models.py")
    assert "视频模型" in page.video_status.text()
    monkeypatch.setattr(
        "fungi.gui._video_ready", lambda: _ready()
    )
    page._poll_download()  # 模型下完 -> 复检就绪并禁用按钮
    assert not page._dl_timer.isActive()
    assert not page.download_btn.isEnabled()


def test_config_page_frozen_exe_falls_back_to_path_python(monkeypatch):
    """exe 冻结态没有内嵌解释器, 落到系统 PATH 上的 python。"""
    monkeypatch.setattr(gui.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gui.shutil, "which", lambda _name: "C:/Python/python.exe")
    page_gui = gui.ConfigPage.__new__(gui.ConfigPage)  # 不触 Qt: 只测纯函数
    assert page_gui._python_cmd() == "C:/Python/python.exe"


def test_config_page_download_btn_hidden_when_nothing_to_heal(window, monkeypatch):
    """用户定调: 没有可自愈缺失 -> 下载按钮整体隐藏 (不是灰着)。"""
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui._video_ready", lambda: _ready())
    page._check_video_models()
    assert not page.download_btn.isVisibleTo(page)
    monkeypatch.setattr("fungi.gui._video_ready", lambda: _ready(torch=False))
    page._check_video_models()
    assert not page.download_btn.isVisibleTo(page)  # 不可自愈 -> 也不给按钮
    monkeypatch.setattr("fungi.gui._video_ready", lambda: _ready(CLIP=False))
    page._check_video_models()
    assert page.download_btn.isVisibleTo(page)  # 有可自愈缺失 -> 出现


def _apply_check(window, monkeypatch, status):
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui.update.check", lambda: status)
    page._upd_thread = None
    page._upd_busy = False
    page.check_update()
    for _ in range(300):
        QApplication.processEvents()
        if page._upd_thread is None:
            break
        time.sleep(0.01)
    return page


def test_config_page_update_button_appears_only_when_behind(window, monkeypatch):
    """自动检查但绝不自动更新: 落后才亮按钮, 已是最新/出错 -> 没有按钮。"""
    page = _apply_check(window, monkeypatch, {
        "mode": "exe", "current": "0.1.1", "latest": "v0.2.0",
        "behind": True, "asset_url": "https://x/fungi-v0.2.0-windows-x64.zip",
        "error": None,
    })
    assert page.update_btn.isVisibleTo(page)
    assert "下载并更新" in page.update_btn.text()
    assert "v0.2.0" in page.update_status.text()

    page = _apply_check(window, monkeypatch, {
        "mode": "exe", "current": "9.9.9", "latest": "v0.2.0",
        "behind": False, "asset_url": None, "error": None,
    })
    assert not page.update_btn.isVisibleTo(page)
    assert "已是最新" in page.update_status.text()


def test_config_page_update_click_git_mode_pulls(window, monkeypatch):
    """点按钮才更新: git 模式走 update_source 线程; 拉取后复检回到"已是最新"。"""
    states = iter([
        {"mode": "git", "current": "0.1.1", "latest": "v0.2.0",
         "behind": True, "asset_url": None, "error": None},
        {"mode": "git", "current": "0.2.0", "latest": "v0.2.0",
         "behind": False, "asset_url": None, "error": None},
    ])
    monkeypatch.setattr("fungi.gui.update.check", lambda: next(states))
    page = window.cfg_page
    page._upd_thread = None
    page._upd_busy = False
    page.check_update()
    for _ in range(300):
        QApplication.processEvents()
        if page._upd_thread is None:
            break
        time.sleep(0.01)
    assert page.update_btn.isVisibleTo(page)

    pulls = []
    def fake_pull():
        pulls.append(1)
        return True, "fast-forward"
    monkeypatch.setattr("fungi.gui.update.update_source", fake_pull)
    page._do_update()
    assert page._upd_busy
    for _ in range(300):
        QApplication.processEvents()
        if not page._upd_busy:
            break
        time.sleep(0.01)
    for _ in range(300):
        QApplication.processEvents()
        if "已是最新" in page.update_status.text():
            break
        time.sleep(0.01)
    assert pulls  # pull 确实跑过
    assert "已是最新" in page.update_status.text()
    assert not page.update_btn.isVisibleTo(page)  # 更新完按钮退场
