"""Shared constants for the GUI (window, ports, settings keys, accents)."""

import os

GUI_PORT = 8899  # scan anchor (Face convention); actual port found by scanning up


PORT_SCAN_LIMIT = 32


SWEEP_TIMEOUT = 0.2  # TCP connect sweep: LAN answers are ms-fast, dead IPs wait it out


PROBE_TIMEOUT = 1.5


SETTINGS_ORG = "Offblink"


SETTINGS_APP = "FungiGUI"


GUI_SCALE = float(os.environ.get("FUNGI_GUI_SCALE", "1.0"))  # fonts + window, uniform


_ACCENT = "#e07a5f"


_ACCENT_GUI = _ACCENT  # calendar today-marker shares the mushroom accent
