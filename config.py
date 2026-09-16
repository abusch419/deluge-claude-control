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


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val in (None, ""):
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


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

# Fill order. New chats always claim the first free row and grow upward. Whether
# the FIRST row is the physical bottom of the grid depends on your layout: some
# Deluge kits number the bottom row with the lowest notes, others the highest.
# When True, the first chat maps to the highest-note block so it lands on the
# BOTTOM row and new chats fill upward. Flip this if it fills the wrong way.
FILL_FROM_BOTTOM = _env_bool("DELUGE_FILL_FROM_BOTTOM", False)

# --- Clearing closed chats ---------------------------------------------------
# A chat's pad clears when its Claude Code process exits -- see WATCH_LIVENESS_S
# below. That covers closing a window, quitting, and crashing, none of which fire
# any hook. The two timeouts here are only for chats that CAN'T be watched that
# way, and they're applied automatically per chat; you shouldn't need to touch
# either one.
#
# SESSION_TTL_S applies to a chat with its own identified process. That process
# dying already clears the pad, so there's nothing left for a timeout to catch
# and the default is 0 (never expire) -- leave a chat open and idle all afternoon
# and its pad stays put.
SESSION_TTL_S = _env_int("DELUGE_SESSION_TTL_S", 0)

# UNTRACKED_TTL_S applies to a chat we can't watch by process: either none was
# identified, or several chats share one process so its being alive says nothing
# about this chat (an editor running one Claude Code process behind several tabs
# looks like this). Kept short on purpose -- guessing late leaves dead pads lit,
# and guessing early costs nothing, since the pad returns on the next prompt.
UNTRACKED_TTL_S = _env_int("DELUGE_UNTRACKED_TTL_S", 900)  # 15 minutes

# --- Brightness (MIDI velocity) & blink rates -------------------------------
# The grid is white-only: Midigrid renders incoming notes as white with velocity
# as brightness, so state is carried by BRIGHTNESS + BLINK RATE, not colour.
#
#   working        -> bright, STEADY (no blink -- it needs nothing from you)
#   done working   -> dim, SLOW blink (wants you back, but it can wait)
#   needs approval -> bright, FAST blink (drops fully off, so it's unmissable)
#
# Blinking is reserved for the two states that actually want your attention, and
# the rate says how badly. A pad that's just working never moves.
SOLID_VELOCITY = _env_int("DELUGE_SOLID_VELOCITY", 127)       # working: steady

# Done working: blinks between these two. Keep both clearly below SOLID_VELOCITY
# so a finished chat never competes with a working one, and keep the low end
# visible -- if a done pad seems to vanish on your hardware, raise it.
IDLE_HIGH_VELOCITY = _env_int("DELUGE_IDLE_HIGH_VELOCITY",
                              _env_int("DELUGE_IDLE_VELOCITY", 60))
IDLE_LOW_VELOCITY = _env_int("DELUGE_IDLE_LOW_VELOCITY", 15)

# Needs approval: blinks between these two, dropping fully off at the low end.
PERM_VELOCITY = _env_int("DELUGE_PERM_VELOCITY", 127)
PERM_LOW_VELOCITY = _env_int("DELUGE_PERM_LOW_VELOCITY", 0)

# Blink half-periods in seconds (time at the high level, then time at the low
# level). The ~5x gap between them is the whole point: it's what separates
# "come back when you can" from "I'm stuck, come now".
# DELUGE_BLINK_INTERVAL_S is honoured as the old name for the permission rate.
PERM_BLINK_S = _env_float("DELUGE_PERM_BLINK_S",
                          _env_float("DELUGE_BLINK_INTERVAL_S", 0.18))
IDLE_BLINK_S = _env_float("DELUGE_IDLE_BLINK_S",
                          _env_float("DELUGE_WORK_BLINK_S", 0.9))

# --- Watch daemon ------------------------------------------------------------
# The `watch` service continuously repaints the grid from tracked state so the
# display always matches reality and self-heals after a Deluge unplug/power-cycle
# or a Mac sleep. It is ALSO what animates the blinks, so this interval has to be
# well under the fastest blink half-period above (it only sends a pad when that
# pad's brightness actually changes, so a tight loop here is cheap on MIDI).
WATCH_INTERVAL_S = _env_float("DELUGE_WATCH_INTERVAL_S", 0.05)
# How often the watcher re-reads the state file. The repaint loop runs far faster
# than state can change, so re-reading JSON every pass would be pure waste.
WATCH_STATE_POLL_S = _env_float("DELUGE_WATCH_STATE_POLL_S", 0.25)
# How often the watcher checks whether each chat's Claude Code process is still
# alive, and clears the pads of the ones that aren't. This is what makes a pad go
# out when you close a window or tab, which fires no hook at all. Costs one `ps`
# call each time, so don't set it very low. 0 disables the check entirely and
# leaves closed chats to idle expiry (SESSION_TTL_S).
WATCH_LIVENESS_S = _env_float("DELUGE_WATCH_LIVENESS_S", 5.0)
# Every this many seconds the watcher forces a full repaint (belt-and-suspenders
# in case the device silently forgot its LEDs without dropping the USB port).
WATCH_FULL_REPAINT_S = _env_float("DELUGE_WATCH_FULL_REPAINT_S", 20.0)
