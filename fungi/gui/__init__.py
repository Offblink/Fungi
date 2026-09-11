"""Fungi GUI: the window, its pages, and the LAN plumbing behind them.

This used to be one 1700-line module. The pages now live beside the helpers they
use, so a change to one page cannot reach across a neighbour by accident, and
the tests can target the module that actually owns a name (patching
`fungi.gui.net.probe_room_port` patches what JoinPage calls).

Everything the outside world reaches through `fungi.gui.<name>` - start.py,
`python -m fungi`, the GUI tests - is re-exported here, so the split stays
invisible to them.
"""

import shutil  # the GUI tests replace shutil.which / sys.frozen / subprocess.Popen
import subprocess
import sys

from .. import todos, update
from ..config import load_config, save_config
from ..protocol import valid_host_name
from ..tools.video import _HEALABLE, _module_available, _video_ready
from ..tray import make_icon
from .app import _GUI_IPC, FungiGui, _activate_running_instance, _singleton_taken, run_gui
from .config import ConfigPage, _hf_hub_missing
from .const import (
    _ACCENT,
    _ACCENT_GUI,
    GUI_PORT,
    GUI_SCALE,
    PORT_SCAN_LIMIT,
    PROBE_TIMEOUT,
    SETTINGS_APP,
    SETTINGS_ORG,
    SWEEP_TIMEOUT,
)
from .courier import CourierPage, _DayDialog
from .help import HELP_SECTIONS, HelpPage
from .host import HostPage
from .join import JoinPage
from .mobile import MobilePage
from .net import (
    default_host_name,
    discover_room,
    find_free_port,
    lan_ip,
    local_subnet_hosts,
    probe_room_port,
    start_client_room,
    start_server_room,
)
from .ring import TONE_IDS, TONE_LABELS, TONES, Ringer, tone_path
from .trayicon import _Tray
from .widgets import _copy, _copy_button, _row

__all__ = [
    "GUI_PORT",
    "GUI_SCALE",
    "HELP_SECTIONS",
    "PORT_SCAN_LIMIT",
    "PROBE_TIMEOUT",
    "SETTINGS_APP",
    "SETTINGS_ORG",
    "SWEEP_TIMEOUT",
    "TONES",
    "TONE_IDS",
    "TONE_LABELS",
    "_ACCENT",
    "_ACCENT_GUI",
    "_GUI_IPC",
    "_HEALABLE",
    "ConfigPage",
    "CourierPage",
    "FungiGui",
    "HelpPage",
    "HostPage",
    "JoinPage",
    "MobilePage",
    "Ringer",
    "_DayDialog",
    "_Tray",
    "_activate_running_instance",
    "_copy",
    "_copy_button",
    "_hf_hub_missing",
    "_module_available",
    "_row",
    "_singleton_taken",
    "_video_ready",
    "default_host_name",
    "discover_room",
    "find_free_port",
    "lan_ip",
    "load_config",
    "local_subnet_hosts",
    "make_icon",
    "probe_room_port",
    "run_gui",
    "save_config",
    "shutil",
    "start_client_room",
    "start_server_room",
    "subprocess",
    "sys",
    "todos",
    "tone_path",
    "update",
    "valid_host_name",
]
