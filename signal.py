#!/usr/bin/env python3
"""
Deluge Status Display for Claude Code agent activity.

Sends MIDI notes to a Synthstrom Deluge (Norns/Highlight-Incoming-Notes grid
layout, MIDI channel 16) so grid pads act as live status indicators.

Layout: each chat (keyed by session_id) gets its own ROW of the grid; its own
status pad is the first pad in that row (column 0), and each subagent it spawns
(keyed by agent_id) lights the next pad to the right in that same row.

  row 0:  [chat A][A.sub1][A.sub2] ...
  row 1:  [chat B][B.sub1] ...
  (Deluge grid is 16 wide, so the next row up starts 16 notes higher.)

Visual language:
  working    -> solid lit   (velocity 127, held)
  idle       -> dim         (velocity 20; chat open, waiting)
  needs perm -> blinking    (background process toggles the note)
  closed/off -> off         (velocity 0)

Events (first CLI arg):
  session_start       chat opened           -> claim row, dim (idle)
  working             chat submitted prompt -> its pad solid
  permission_request  needs permission      -> blink that pad (subagent or chat)
  posttool            tool finished         -> clear a pending blink -> solid
  stop                chat finished turn    -> flash then dim (idle), keep row
  session_end         chat closed           -> free row + its subagents, off
  subagent_start      subagent spawned      -> next pad in its chat's row, solid
  subagent_stop       subagent finished     -> flash+off, free that pad
  disable             mute (for jamming): blank grid; all hooks no-op until enable
  enable              unmute; hooks resume lighting pads
  reset               blank the whole grid + wipe state
  _blink <note>       (internal) background blink worker

While disabled (a flag file exists), every hook exits immediately without
touching MIDI, so you can jam on the Deluge with Claude Code running.

Reads Claude Code's hook JSON payload from stdin. Never throws, always exits 0.
"""

import sys
import os

# This script is named signal.py. When run directly, its own directory is placed
# first on sys.path, which would shadow the stdlib `signal` module (and any
# dependency, e.g. mido/rtmidi, that does `import signal`). Scrub the script dir
# from sys.path BEFORE importing anything else so real stdlib modules resolve.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _SCRIPT_DIR]

import json  # noqa: E402
import time  # noqa: E402
import signal as signal_module  # noqa: E402
import argparse  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from datetime import datetime  # noqa: E402
from contextlib import contextmanager  # noqa: E402

try:
    import fcntl  # noqa: E402  (POSIX; present on macOS/Linux)
except Exception:
    fcntl = None

# Re-add the script dir at the END of sys.path (after the scrub above) so local
# modules like `config` resolve, without re-shadowing stdlib modules.
if _SCRIPT_DIR not in sys.path:
    sys.path.append(_SCRIPT_DIR)
import config  # noqa: E402  (local hardware config)

CLAUDE_DIR = Path.home() / ".claude"
STATE_FILE = CLAUDE_DIR / "deluge_slots.json"
LOCK_FILE = CLAUDE_DIR / "deluge_slots.lock"
DEBUG_LOG = CLAUDE_DIR / "hook_debug.log"
DISABLE_FILE = CLAUDE_DIR / "deluge_disabled"  # presence == muted (for jamming)
BLINK_PID_DIR = CLAUDE_DIR  # blink pidfiles: deluge_blink_<note>.pid

# --- Grid layout -------------------------------------------------------------
# All hardware-specific values live in config.py (edit there, or override via
# environment variables). See that file for what each one means.
#
# Each chat owns a ROW (note = BASE_NOTE + row*ROW_WIDTH + col):
#   col 0            = the chat's own status pad
#   col 1, 2, 3, ... = that chat's subagents, left to right
BASE_NOTE = config.BASE_NOTE
ROW_WIDTH = config.ROW_WIDTH
NUM_ROWS = config.NUM_ROWS
FILL_FROM_BOTTOM = config.FILL_FROM_BOTTOM
MIDI_CHANNEL = config.MIDI_CHANNEL
SESSION_TTL_S = config.SESSION_TTL_S

# Brightness / timing
SOLID_VELOCITY = config.SOLID_VELOCITY
IDLE_VELOCITY = config.IDLE_VELOCITY
PERM_VELOCITY = config.PERM_VELOCITY
FLASH_VELOCITY = config.FLASH_VELOCITY
FLASH_MS = config.FLASH_MS
BLINK_INTERVAL_S = config.BLINK_INTERVAL_S


# --- Mute switch -------------------------------------------------------------
def is_disabled() -> bool:
    """True when the mute flag file exists (jamming mode)."""
    try:
        return DISABLE_FILE.exists()
    except Exception:
        return False


# --- Debug -------------------------------------------------------------------
def log_debug(payload_raw: str, event: str) -> None:
    try:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG, "a") as f:
            f.write(f"\n--- {datetime.now().isoformat()} | event={event} ---\n")
            f.write(payload_raw)
            f.write("\n")
    except Exception:
        pass


# --- State -------------------------------------------------------------------
def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_state(state: dict) -> None:
    try:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


@contextmanager
def state_lock():
    """Exclusive lock around read-modify-write of the shared slot state, so
    concurrent chats/subagents don't grab the same slot. No-op if fcntl absent."""
    f = None
    try:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        if fcntl is not None:
            f = open(LOCK_FILE, "w")
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    except Exception:
        yield
    finally:
        if f is not None:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass


def _norm(state) -> dict:
    """Normalize the on-disk state into {'sessions': {...}, 'agents': {...},
    'seen': {sid: epoch_seconds}}."""
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("sessions"), dict):
        state["sessions"] = {}
    if not isinstance(state.get("agents"), dict):
        state["agents"] = {}
    if not isinstance(state.get("seen"), dict):
        state["seen"] = {}
    if not isinstance(state.get("blink"), list):
        state["blink"] = []
    if not isinstance(state.get("idle_since"), dict):
        state["idle_since"] = {}
    return state


def _first_free(used, count: int, start: int = 0) -> int:
    for i in range(start, count):
        if i not in used:
            return i
    return count - 1  # overflow: reuse the last one


def note_for(row: int, col: int) -> int:
    # `row` is the logical fill index (0 = first chat). Map it to a physical grid
    # row: with FILL_FROM_BOTTOM, index 0 lands on the bottom (highest-note block)
    # so new chats stack upward.
    physical = (NUM_ROWS - 1 - row) if FILL_FROM_BOTTOM else row
    return BASE_NOTE + physical * ROW_WIDTH + col


# --- Identity ----------------------------------------------------------------
def get_agent_id(payload: dict):
    """Subagent identity: present only when the hook fires inside a subagent."""
    for key in ("agent_id", "subagent_id"):
        val = payload.get(key)
        if val:
            return str(val)
    return None


def get_session_id(payload: dict) -> str:
    """Main-chat identity."""
    val = payload.get("session_id")
    return str(val) if val else "main"


# --- Row/column assignment (all locked) --------------------------------------
def claim_session_row(sid: str) -> int:
    with state_lock():
        state = _norm(load_state())
        sessions = state["sessions"]
        if sid not in sessions:
            sessions[sid] = _first_free(set(sessions.values()), NUM_ROWS)
            save_state(state)
        row = sessions[sid]
    return row


def peek_session_note(sid: str):
    row = _norm(load_state())["sessions"].get(sid)
    return note_for(row, 0) if row is not None else None


def claim_agent_note(sid: str, aid: str) -> int:
    with state_lock():
        state = _norm(load_state())
        sessions, agents = state["sessions"], state["agents"]
        if sid not in sessions:  # ensure the parent chat has a row
            sessions[sid] = _first_free(set(sessions.values()), NUM_ROWS)
        row = sessions[sid]
        if aid in agents:
            col = agents[aid]["col"]
        else:
            used_cols = {a["col"] for a in agents.values() if a.get("session") == sid}
            col = _first_free(used_cols, ROW_WIDTH, start=1)
            agents[aid] = {"session": sid, "col": col}
        save_state(state)
    return note_for(row, col)


def peek_agent_note(aid: str):
    state = _norm(load_state())
    a = state["agents"].get(aid)
    if not a:
        return None
    row = state["sessions"].get(a.get("session"))
    return note_for(row, a["col"]) if row is not None else None


def free_agent(aid: str):
    with state_lock():
        state = _norm(load_state())
        a = state["agents"].pop(aid, None)
        note = None
        if a:
            row = state["sessions"].get(a.get("session"))
            if row is not None:
                note = note_for(row, a["col"])
            save_state(state)
    return note


def _remove_session(state: dict, sid: str):
    """Remove a chat + its subagents from `state` (unlocked). Returns notes."""
    notes = []
    sessions, agents = state["sessions"], state["agents"]
    row = sessions.pop(sid, None)
    state.get("seen", {}).pop(sid, None)
    state.get("idle_since", {}).pop(sid, None)
    if row is not None:
        notes.append(note_for(row, 0))
        for aid in [k for k, v in agents.items() if v.get("session") == sid]:
            a = agents.pop(aid)
            notes.append(note_for(row, a["col"]))
    return notes


def free_session(sid: str):
    """Remove a chat and all its subagents. Returns list of notes to blank."""
    with state_lock():
        state = _norm(load_state())
        notes = _remove_session(state, sid)
        if notes:
            save_state(state)
    return notes


def _session_is_blinking(state: dict, sid: str) -> bool:
    """True if this chat's own pad or any of its subagents' pads is blinking
    (i.e. waiting for human intervention). Such chats must never be expired."""
    row = state["sessions"].get(sid)
    if row is None:
        return False
    if is_blinking(note_for(row, 0)):
        return True
    for a in state["agents"].values():
        if a.get("session") == sid and is_blinking(note_for(row, a.get("col", 0))):
            return True
    return False


# Events after which a chat is considered "finished / idle" (eligible to expire
# once it stays quiet), vs. events that mean it's actively working (never expire).
_IDLE_EVENTS = {"session_start", "stop"}
_ACTIVE_EVENTS = {"working", "permission_request", "posttool", "subagent_start"}


def touch_and_prune(sid: str, event: str):
    """Update `sid`'s idle/active bookkeeping for this event, then expire any OTHER
    chat that FINISHED a turn and then stayed idle longer than SESSION_TTL_S.

    The VS Code extension never fires SessionEnd on tab close, so we approximate
    "closed" as "finished and then abandoned". A chat that is actively working
    (its last event was a prompt/tool use, so it has no idle timestamp) is never
    expired, and a blinking pad (needs intervention) is never expired. With
    SESSION_TTL_S == 0, nothing is expired. Returns notes to blank."""
    expired_notes = []
    now = time.time()
    with state_lock():
        state = _norm(load_state())
        seen = state["seen"]
        idle_since = state["idle_since"]
        if sid:
            seen[sid] = now
            if event in _IDLE_EVENTS:
                idle_since[sid] = now          # finished/waiting -> start idle clock
            elif event in _ACTIVE_EVENTS:
                idle_since.pop(sid, None)       # actively working -> not expirable
        if SESSION_TTL_S > 0:
            stale = []
            for s in list(state["sessions"].keys()):
                if s == sid:
                    continue
                if _session_is_blinking(state, s):  # needs intervention: never expire
                    continue
                ts = idle_since.get(s)
                if ts is None:  # never finished a turn (still working): keep
                    continue
                if now - ts > SESSION_TTL_S:
                    stale.append(s)
            for s in stale:
                expired_notes.extend(_remove_session(state, s))
        save_state(state)
    return expired_notes


def claim_key_note(payload: dict) -> int:
    """Note for whoever the hook is about: the subagent if inside one, else chat."""
    aid = get_agent_id(payload)
    sid = get_session_id(payload)
    if aid:
        return claim_agent_note(sid, aid)
    return note_for(claim_session_row(sid), 0)


def peek_key_note(payload: dict):
    aid = get_agent_id(payload)
    if aid:
        return peek_agent_note(aid)
    return peek_session_note(get_session_id(payload))


# --- MIDI --------------------------------------------------------------------
def find_deluge_port():
    try:
        import mido
        names = mido.get_output_names()
        if config.DELUGE_PORT_NAME in names:  # exact configured match first
            return config.DELUGE_PORT_NAME
        for name in names:  # fall back to any port containing the configured name
            if config.DELUGE_PORT_NAME in name:
                return name
        for name in names:  # last resort: any Deluge port
            if "Deluge" in name:
                return name
    except Exception:
        pass
    return None


def send_note(note: int, velocity: int, port_name=None) -> None:
    try:
        import mido
        if port_name is None:
            port_name = find_deluge_port()
        if port_name is None:
            return
        with mido.open_output(port_name) as port:
            port.send(mido.Message("note_on", channel=MIDI_CHANNEL, note=note, velocity=velocity))
    except Exception:
        pass


def flash_to(note: int, final_velocity: int) -> None:
    """Brief bright flash then settle to `final_velocity`, to signal a transition."""
    try:
        port_name = find_deluge_port()
        if port_name is None:
            return
        import mido
        with mido.open_output(port_name) as port:
            port.send(mido.Message("note_on", channel=MIDI_CHANNEL, note=note, velocity=FLASH_VELOCITY))
            time.sleep(FLASH_MS / 1000.0)
            port.send(mido.Message("note_on", channel=MIDI_CHANNEL, note=note, velocity=final_velocity))
    except Exception:
        pass


def flash_off(note: int) -> None:
    flash_to(note, 0)


# --- Blink management --------------------------------------------------------
def blink_pidfile(note: int) -> Path:
    return BLINK_PID_DIR / f"deluge_blink_{note}.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0: existence check, doesn't actually signal
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else (shouldn't happen here)
    except Exception:
        return False


def is_blinking(note: int) -> bool:
    """True only if a blink worker for `note` is genuinely running. A pidfile can
    outlive its process (e.g. the Mac slept overnight and killed the worker); in
    that case we clean up the stale file and report False so the pad can be
    revived by resync_blinks() instead of being silently believed to be blinking."""
    pf = blink_pidfile(note)
    try:
        if not pf.exists():
            return False
        pid = int(pf.read_text().strip())
    except Exception:
        return False
    if _pid_alive(pid):
        return True
    try:
        pf.unlink()  # stale: worker is gone
    except Exception:
        pass
    return False


def _record_blink(note: int, active: bool) -> None:
    """Persist (or clear) the durable 'this pad needs intervention' intent in the
    state file, so a blink survives worker death, sleep, or reboot."""
    with state_lock():
        state = _norm(load_state())
        marked = set(state["blink"])
        if active:
            marked.add(note)
        else:
            marked.discard(note)
        state["blink"] = sorted(marked)
        save_state(state)


def resync_blinks() -> None:
    """Ensure every pad marked as needing intervention has a live worker.
    Restarts any workers lost to sleep/reboot/disconnect so a 'needs
    intervention' signal is never silently dropped."""
    try:
        for note in list(_norm(load_state())["blink"]):
            if not is_blinking(note):
                _spawn_blink_worker(note)
    except Exception:
        pass


def _kill_pid(pid: int) -> None:
    """SIGTERM, then escalate to SIGKILL if the process survives (it can defer
    SIGTERM while inside CoreMIDI init). Cheap: only waits if still alive."""
    try:
        os.kill(pid, signal_module.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        return
    for _ in range(5):  # up to ~0.1s
        time.sleep(0.02)
        try:
            os.kill(pid, 0)  # probe; raises if gone
        except ProcessLookupError:
            return
        except Exception:
            return
    try:
        os.kill(pid, signal_module.SIGKILL)
    except Exception:
        pass


def stop_blink(note: int, final_velocity=None, clear_record: bool = True) -> bool:
    """Kill a running blink worker for `note`. Returns True if one was running.
    If final_velocity is given, set the note to that state afterwards.
    By default also clears the durable 'needs intervention' record so it won't be
    revived by resync_blinks (pass clear_record=False to only swap the worker)."""
    if clear_record:
        _record_blink(note, False)
    was_running = False
    pf = blink_pidfile(note)
    try:
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
                _kill_pid(pid)
                was_running = True
            except Exception:
                pass
            try:
                pf.unlink()
            except Exception:
                pass
    except Exception:
        pass
    if final_velocity is not None:
        send_note(note, final_velocity)
    return was_running


def _spawn_blink_worker(note: int) -> None:
    """Spawn a detached process that pulses `note`. Does NOT touch durable state;
    used both by start_blink (first request) and resync_blinks (revival)."""
    try:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_blink", str(note)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        blink_pidfile(note).write_text(str(proc.pid))
    except Exception:
        pass


def start_blink(note: int) -> None:
    """Mark `note` as needing intervention (durably) and spawn its blink worker."""
    stop_blink(note, clear_record=False)  # never run two blinkers on the same note
    _record_blink(note, True)             # durable intent: survives sleep/reboot
    _spawn_blink_worker(note)


def blink_worker(note: int) -> None:
    """Runs in a detached process: pulse `note` until SIGTERM.

    Resilient by design so a "needs intervention" signal is never lost:
      - If the Deluge isn't present yet, wait and keep retrying.
      - If the connection drops mid-blink, reopen the port and resume (this also
        re-lights the pad after the device is unplugged/replugged or rebooted).
    It only ever stops when explicitly killed (stop_blink / reset).
    """
    try:
        import mido
    except Exception:
        return

    on = True
    while True:  # outer loop: (re)acquire the port forever
        port_name = find_deluge_port()
        if port_name is None:
            time.sleep(1.0)  # device absent; wait and retry (don't die)
            continue
        try:
            with mido.open_output(port_name) as port:
                while True:  # inner loop: blink while the port is healthy
                    vel = PERM_VELOCITY if on else 0
                    try:
                        port.send(mido.Message("note_on", channel=MIDI_CHANNEL,
                                               note=note, velocity=vel))
                    except Exception:
                        break  # connection likely dropped -> reopen it
                    on = not on
                    time.sleep(BLINK_INTERVAL_S)
        except Exception:
            time.sleep(1.0)  # couldn't open (e.g. mid-reconnect); retry


def kill_stray_workers() -> None:
    """Belt-and-suspenders: kill any lingering blink workers by command match,
    even ones whose pidfile was lost. Only used by `reset`, not the hot path."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "signal.py _blink"],
            capture_output=True, text=True, timeout=2,
        )
        for line in out.stdout.split():
            try:
                pid = int(line)
                if pid == os.getpid():
                    continue
                _kill_pid(pid)
            except Exception:
                pass
    except Exception:
        pass


def clear_all_blinks() -> None:
    try:
        for pf in BLINK_PID_DIR.glob("deluge_blink_*.pid"):
            try:
                note = int(pf.stem.replace("deluge_blink_", ""))
            except Exception:
                note = None
            try:
                pid = int(pf.read_text().strip())
                _kill_pid(pid)
            except Exception:
                pass
            try:
                pf.unlink()
            except Exception:
                pass
            if note is not None:
                send_note(note, 0)
    except Exception:
        pass


# --- Main --------------------------------------------------------------------
def main() -> None:
    # Internal blink worker mode (spawned detached; not a real hook event).
    if len(sys.argv) >= 3 and sys.argv[1] == "_blink":
        try:
            blink_worker(int(sys.argv[2]))
        except Exception:
            pass
        sys.exit(0)

    parser = argparse.ArgumentParser()
    parser.add_argument("event")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    event = args.event

    # Mute gate: while disabled, ordinary hooks do nothing (cheap file check on
    # the hot path). Control commands still work so you can re-enable/reset.
    if event not in ("enable", "disable", "reset") and is_disabled():
        sys.exit(0)

    try:
        payload_raw = sys.stdin.read()
    except Exception:
        payload_raw = ""

    if args.debug:
        log_debug(payload_raw, event)

    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else {}
    except Exception:
        payload = {}

    try:
        _dispatch(event, payload)
    except Exception:
        pass

    sys.exit(0)


# Events that represent activity from a chat (used to refresh its idle timer and
# to trigger pruning of other chats that have gone idle past SESSION_TTL_S).
_STATUS_EVENTS = {
    "session_start", "working", "permission_request", "posttool",
    "stop", "session_end", "subagent_start", "subagent_stop",
}


def _refresh_grid() -> None:
    """Redraw the whole grid from the tracked state in one MIDI pass.

    Chats settle to IDLE (dim), still-tracked subagents show SOLID, every other
    pad is blanked, and notes with a live blink worker are left untouched so a
    pending permission request keeps pulsing.
    """
    try:
        import mido
    except Exception:
        return
    resync_blinks()  # revive any intervention blinks lost to sleep/reboot first
    port_name = find_deluge_port()
    if port_name is None:
        return

    state = _norm(load_state())
    targets = {}
    for sid, row in state["sessions"].items():
        targets[note_for(row, 0)] = IDLE_VELOCITY
    for a in state["agents"].values():
        row = state["sessions"].get(a.get("session"))
        if row is not None:
            targets[note_for(row, a.get("col", 0))] = SOLID_VELOCITY

    try:
        with mido.open_output(port_name) as port:
            for note in range(BASE_NOTE, BASE_NOTE + NUM_ROWS * ROW_WIDTH):
                if is_blinking(note):
                    continue  # let the blink worker own this pad
                try:
                    port.send(mido.Message(
                        "note_on", channel=MIDI_CHANNEL,
                        note=note, velocity=targets.get(note, 0)))
                except Exception:
                    pass
    except Exception:
        pass


def _dispatch(event: str, payload: dict) -> None:
    sid = get_session_id(payload)

    # Any activity refreshes this chat's idle timer and expires stale chats
    # (whose close event Claude Code never delivered), blanking their pads.
    if event in _STATUS_EVENTS:
        # Self-heal: if any intervention blink lost its worker (Mac slept,
        # reboot, device unplugged), revive it so the signal isn't lost.
        resync_blinks()
        for note in touch_and_prune(sid, event):
            stop_blink(note)
            send_note(note, 0)

    if event == "session_start":
        # A new chat opened -> claim its row; its pad shows dim (idle, waiting).
        note = note_for(claim_session_row(sid), 0)
        send_note(note, IDLE_VELOCITY)

    elif event == "working":
        # Chat submitted a prompt -> its pad solid (working).
        note = note_for(claim_session_row(sid), 0)
        stop_blink(note)
        send_note(note, SOLID_VELOCITY)

    elif event == "permission_request":
        # Blink the pad of whoever asked (subagent if inside one, else the chat).
        note = claim_key_note(payload)
        start_blink(note)

    elif event == "posttool":
        # A tool finished, so this chat is actively working.
        note = peek_key_note(payload)
        if note is None:
            # We have no row for it yet (e.g. it was mid-work when you `reset`
            # the grid). Re-register it now so an active chat reappears within
            # one tool call instead of waiting for your next prompt.
            note = claim_key_note(payload)
            send_note(note, SOLID_VELOCITY)
        elif is_blinking(note):
            # Pad was blinking for a permission prompt -> prompt resolved, go
            # back to solid. Otherwise stay cheap: no MIDI on the hot path.
            stop_blink(note, final_velocity=SOLID_VELOCITY)

    elif event == "stop":
        # Chat finished responding -> flash then settle to dim (idle). Keep the
        # row; the chat's pad stays visible (dim) until the chat closes.
        note = peek_session_note(sid)
        if note is not None:
            stop_blink(note)
            flash_to(note, IDLE_VELOCITY)

    elif event == "session_end":
        # Chat closed -> free its row and all its subagents, blank their pads.
        for note in free_session(sid):
            stop_blink(note)
            send_note(note, 0)

    elif event == "subagent_start":
        aid = get_agent_id(payload)
        note = claim_agent_note(sid, aid) if aid else note_for(claim_session_row(sid), 0)
        send_note(note, SOLID_VELOCITY)

    elif event == "subagent_stop":
        aid = get_agent_id(payload)
        note = free_agent(aid) if aid else peek_session_note(sid)
        if note is not None:
            stop_blink(note)
            flash_off(note)

    elif event == "disable":
        # Mute for jamming: set the flag, stop blinks, blank the whole grid.
        # State is kept so re-enabling picks up where chats left off.
        try:
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            DISABLE_FILE.write_text("1")
        except Exception:
            pass
        clear_all_blinks()
        kill_stray_workers()
        for note in range(BASE_NOTE, BASE_NOTE + NUM_ROWS * ROW_WIDTH):
            send_note(note, 0)

    elif event == "enable":
        try:
            if DISABLE_FILE.exists():
                DISABLE_FILE.unlink()
        except Exception:
            pass
        # Restore the display to match tracked state (and resume any blinks that
        # were muted for jamming) instead of waiting for the next hook event.
        _refresh_grid()

    elif event == "refresh":
        # Re-sync the physical grid to the tracked state without wiping it:
        # blank stray pads, redraw chats (dim/idle) and subagents (solid),
        # and leave any active permission blinks running.
        _refresh_grid()

    elif event == "reset":
        clear_all_blinks()
        kill_stray_workers()
        for note in range(BASE_NOTE, BASE_NOTE + NUM_ROWS * ROW_WIDTH):
            send_note(note, 0)
        try:
            if STATE_FILE.exists():
                STATE_FILE.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
