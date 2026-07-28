"""
Central configuration for the Deluge + Claude Code status display.

Everything here is HARDWARE-SPECIFIC — it depends on how your Synthstrom Deluge
grid is laid out and what it's named on your machine. The defaults below are the
author's setup and are NOT universal.

Discover your own values first:
  - `python3 midi_probe.py`  -> find your Deluge's port name, and tap pads to see
                                which note numbers (and channel) they send.
  - `python3 light_test.py`  -> send notes back to confirm which pads light up.

Then either edit the values below, or override any of them with an environment
variable of the same name (the env var wins). For example:

  export DELUGE_PORT_NAME="Deluge Port 1"
  export DELUGE_MIDI_CHANNEL=15
"""

import os


def _env_str(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# --- MIDI port ---------------------------------------------------------------
# Exact name of the Deluge's MIDI port (output + input). Run midi_probe.py to
# see the list. The author's Deluge enumerates as "Deluge Port 1".
DELUGE_PORT_NAME = _env_str("DELUGE_PORT_NAME", "Deluge Port 1")

# --- MIDI channel ------------------------------------------------------------
# Zero-indexed. The Deluge's Midigrid feedback uses MIDI channel 16, which mido
# represents as channel 15. Change only if your firmware uses a different one.
MIDI_CHANNEL = _env_int("DELUGE_MIDI_CHANNEL", 15)

# --- Grid layout -------------------------------------------------------------
# Pad note = BASE_NOTE + row*ROW_WIDTH + col
#   - Each chat gets a row; column 0 is the chat, columns 1+ are its subagents.
#   - The Deluge main grid is 16 pads wide, so a new chat's row starts +16.
# The author's kit rows happen to send notes 60-67, but the status display can
# drive whatever contiguous grid notes you point it at. Set BASE_NOTE to the
# first pad you want to use (find it with midi_probe.py / light_test.py).
BASE_NOTE = _env_int("DELUGE_BASE_NOTE", 0)
ROW_WIDTH = _env_int("DELUGE_ROW_WIDTH", 16)   # grid width / row stride
NUM_ROWS = _env_int("DELUGE_NUM_ROWS", 8)      # grid height -> max concurrent chats

# --- Brightness (MIDI velocity) & timing ------------------------------------
SOLID_VELOCITY = _env_int("DELUGE_SOLID_VELOCITY", 127)  # working
IDLE_VELOCITY = _env_int("DELUGE_IDLE_VELOCITY", 20)     # chat open but idle
PERM_VELOCITY = _env_int("DELUGE_PERM_VELOCITY", 127)    # permission blink (on phase)
FLASH_VELOCITY = _env_int("DELUGE_FLASH_VELOCITY", 127)  # transition flash
FLASH_MS = _env_int("DELUGE_FLASH_MS", 200)              # flash duration
BLINK_INTERVAL_S = _env_float("DELUGE_BLINK_INTERVAL_S", 0.4)  # permission blink rate
