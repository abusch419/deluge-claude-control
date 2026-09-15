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

Visual language (the grid is white-only, so brightness + blink rate carry the
state -- see config.py):
  needs approval -> bright, FAST blink (127 <-> 0, ~0.18s)
  working        -> bright, SLOW blink (127 <-> 60, ~0.9s)
  stopped / idle -> dull, steady        (velocity 25, no blink)
  closed / off   -> off                 (velocity 0)

The `watch` daemon is what animates the blinks: it repaints the grid from tracked
state many times a second, deriving each pad's brightness from its state and the
clock. Hooks still paint an immediate static frame so you get instant feedback,
but WITHOUT the daemon running nothing blinks -- pads are just bright or dull.

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
  refresh             re-sync the grid to tracked state (non-destructive)
  watch               run the self-healing daemon that keeps the grid in sync

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
BLINK_PID_DIR = CLAUDE_DIR  # legacy blink pidfiles, only ever cleaned up now

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

# Brightness / blink rates
SOLID_VELOCITY = config.SOLID_VELOCITY
WORK_LOW_VELOCITY = config.WORK_LOW_VELOCITY
IDLE_VELOCITY = config.IDLE_VELOCITY
PERM_VELOCITY = config.PERM_VELOCITY
PERM_LOW_VELOCITY = config.PERM_LOW_VELOCITY
PERM_BLINK_S = config.PERM_BLINK_S
WORK_BLINK_S = config.WORK_BLINK_S
WATCH_INTERVAL_S = config.WATCH_INTERVAL_S
WATCH_STATE_POLL_S = config.WATCH_STATE_POLL_S
WATCH_FULL_REPAINT_S = config.WATCH_FULL_REPAINT_S


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
    marked = blink_notes(state)
    if note_for(row, 0) in marked:
        return True
    for a in state["agents"].values():
        if a.get("session") == sid and note_for(row, a.get("col", 0)) in marked:
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


# --- Blink state -------------------------------------------------------------
# Blinking is no longer driven by a background process per pad. Every pad's
# brightness is a pure function of (tracked state, clock), and the `watch` daemon
# evaluates that function on every repaint. All that lives on disk is the durable
# set of notes that need human approval, so the signal survives sleep/reboot.
def _record_blink(note: int, active: bool) -> None:
    """Persist (or clear) the durable 'this pad needs approval' intent."""
    with state_lock():
        state = _norm(load_state())
        marked = set(state["blink"])
        if active:
            marked.add(note)
        else:
            marked.discard(note)
        state["blink"] = sorted(marked)
        save_state(state)


def blink_notes(state=None) -> set:
    """The set of notes currently waiting on human approval."""
    if state is None:
        state = _norm(load_state())
    try:
        return set(state["blink"])
    except Exception:
        return set()


def is_blinking(note: int, state=None) -> bool:
    return note in blink_notes(state)


def start_blink(note: int) -> None:
    """Mark `note` as needing approval and light it now. The watch daemon picks
    it up on its next pass and starts the fast blink."""
    _record_blink(note, True)
    send_note(note, PERM_VELOCITY)


def stop_blink(note: int, final_velocity=None) -> bool:
    """Clear the 'needs approval' mark on `note`. Returns True if it was set.
    If final_velocity is given, paint that immediately rather than waiting for
    the daemon's next pass."""
    was_marked = is_blinking(note)
    if was_marked:
        _record_blink(note, False)
    if final_velocity is not None:
        send_note(note, final_velocity)
    return was_marked


def clear_all_blinks() -> None:
    """Drop every 'needs approval' mark and blank those pads."""
    notes = blink_notes()
    with state_lock():
        state = _norm(load_state())
        state["blink"] = []
        save_state(state)
    for note in notes:
        send_note(note, 0)


# --- Brightness as a function of state + clock -------------------------------
def _blink_high(now: float, half_period_s: float) -> bool:
    """True during the lit half of a blink cycle. Driven off the wall clock so
    every pad in the same state blinks in unison instead of drifting apart."""
    if half_period_s <= 0:
        return True
    return int(now / half_period_s) % 2 == 0


def perm_velocity_at(now: float) -> int:
    """Needs approval: bright, FAST blink, dropping fully off."""
    return PERM_VELOCITY if _blink_high(now, PERM_BLINK_S) else PERM_LOW_VELOCITY


def work_velocity_at(now: float) -> int:
    """Working: bright, SLOW blink that never dims to the idle level."""
    return SOLID_VELOCITY if _blink_high(now, WORK_BLINK_S) else WORK_LOW_VELOCITY


# --- Legacy cleanup ----------------------------------------------------------
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


def kill_stray_workers() -> None:
    """Kill any blink workers left over from an older version of this script, and
    delete their pidfiles. Nothing spawns these any more; this only runs on
    `reset` / `disable` so an upgrade can't leave a pad pulsing forever."""
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
    try:
        for pf in BLINK_PID_DIR.glob("deluge_blink_*.pid"):
            try:
                pf.unlink()
            except Exception:
                pass
    except Exception:
        pass


# --- Main --------------------------------------------------------------------
def main() -> None:
    # Long-running watch daemon: continuously reconciles the grid with state.
    if len(sys.argv) >= 2 and sys.argv[1] == "watch":
        try:
            watch_loop()
        except KeyboardInterrupt:
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


def _desired_grid(state: dict, now: float) -> dict:
    """Target velocity for every pad, a pure function of tracked state + clock:

      needs approval -> fast blink between PERM_VELOCITY and PERM_LOW_VELOCITY
      working        -> slow blink between SOLID_VELOCITY and WORK_LOW_VELOCITY
      stopped / idle -> steady IDLE_VELOCITY (dull, no blink)

    A chat counts as working until it finishes a turn (which is what puts it in
    `idle_since`); subagents only exist in state while they're running, so their
    pads always show the working blink. Notes not listed here are off (0).
    Needing approval wins over working -- that's the one you have to act on.
    """
    desired = {}
    sessions = state["sessions"]
    idle = state["idle_since"]
    work_vel = work_velocity_at(now)
    perm_vel = perm_velocity_at(now)
    for sid, row in sessions.items():
        desired[note_for(row, 0)] = IDLE_VELOCITY if sid in idle else work_vel
    for a in state["agents"].values():
        row = sessions.get(a.get("session"))
        if row is not None:
            desired[note_for(row, a.get("col", 0))] = work_vel
    for note in blink_notes(state):
        desired[note] = perm_vel
    return desired


def _refresh_grid() -> None:
    """Redraw the whole grid from the tracked state in one MIDI pass.

    A single frame only -- the `watch` daemon is what keeps the blinks moving.
    Used by `enable` and `refresh` so the display catches up immediately instead
    of waiting for the next hook.
    """
    try:
        import mido
    except Exception:
        return
    port_name = find_deluge_port()
    if port_name is None:
        return

    desired = _desired_grid(_norm(load_state()), time.time())
    try:
        with mido.open_output(port_name) as port:
            for note in range(BASE_NOTE, BASE_NOTE + NUM_ROWS * ROW_WIDTH):
                try:
                    port.send(mido.Message(
                        "note_on", channel=MIDI_CHANNEL,
                        note=note, velocity=desired.get(note, 0)))
                except Exception:
                    pass
    except Exception:
        pass


def watch_loop() -> None:
    """Continuously reconcile the Deluge grid with tracked state.

    This is both the reliability backbone and the animator. Instead of only
    painting when a hook fires, it repaints from state every WATCH_INTERVAL_S,
    so the display always reflects reality and SELF-HEALS after a Deluge
    unplug/power-cycle or a Mac sleep -- and because each pad's brightness is a
    function of the clock, repainting fast enough is exactly what produces the
    slow (working) and fast (needs approval) blinks.

    It diff-paints (only sends pads whose brightness actually changed) so a tight
    loop stays quiet on the wire, re-reads the state file only every
    WATCH_STATE_POLL_S, forces a full repaint periodically, and blanks the grid
    while muted.
    """
    try:
        import mido
    except Exception:
        return
    grid = list(range(BASE_NOTE, BASE_NOTE + NUM_ROWS * ROW_WIDTH))
    while True:  # outer loop: (re)acquire the port forever
        port_name = find_deluge_port()
        if port_name is None:
            time.sleep(2.0)
            continue
        painted = {}            # note -> last velocity we sent (reset on reconnect)
        last_full = 0.0         # force an initial full paint
        state = None
        last_poll = 0.0
        try:
            with mido.open_output(port_name) as out:
                while True:
                    now = time.time()
                    if now - last_full > WATCH_FULL_REPAINT_S:
                        painted.clear()  # heal any silent drift
                        last_full = now

                    muted = is_disabled()
                    if muted:           # muted for jamming: keep the grid dark
                        desired = {}
                    else:
                        if state is None or now - last_poll >= WATCH_STATE_POLL_S:
                            state = _norm(load_state())
                            last_poll = now
                        desired = _desired_grid(state, now)

                    for n in grid:
                        vel = desired.get(n, 0)
                        if painted.get(n) != vel:
                            out.send(mido.Message("note_on", channel=MIDI_CHANNEL,
                                                  note=n, velocity=vel))
                            painted[n] = vel
                    time.sleep(WATCH_INTERVAL_S)
        except Exception:
            time.sleep(2.0)  # port lost (unplug/power-cycle) -> reconnect + full repaint


def _dispatch(event: str, payload: dict) -> None:
    sid = get_session_id(payload)

    # Any activity refreshes this chat's idle timer and expires stale chats
    # (whose close event Claude Code never delivered), blanking their pads.
    if event in _STATUS_EVENTS:
        for note in touch_and_prune(sid, event):
            stop_blink(note)
            send_note(note, 0)

    if event == "session_start":
        # A new chat opened -> claim its row; its pad shows dim (idle, waiting).
        note = note_for(claim_session_row(sid), 0)
        send_note(note, IDLE_VELOCITY)

    elif event == "working":
        # Chat submitted a prompt -> working. Light it bright now; the watch
        # daemon takes over and gives it the slow working blink.
        note = note_for(claim_session_row(sid), 0)
        stop_blink(note)
        send_note(note, SOLID_VELOCITY)

    elif event == "permission_request":
        # Whoever asked (subagent if inside one, else the chat) needs approval:
        # mark it, and the watch daemon switches that pad to the fast blink.
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
            # Pad was fast-blinking for a permission prompt -> approved, so drop
            # back to the working blink. Otherwise stay cheap: no MIDI here.
            stop_blink(note, final_velocity=SOLID_VELOCITY)

    elif event == "stop":
        # Chat finished responding -> stop blinking and settle to steady dull.
        # Keep the row; the pad stays visible (dull) until the chat closes.
        note = peek_session_note(sid)
        if note is not None:
            stop_blink(note)
            send_note(note, IDLE_VELOCITY)

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
            send_note(note, 0)

    elif event == "disable":
        # Mute for jamming: set the flag and blank the whole grid. State is
        # kept -- including which pads need approval -- so `enable` picks up
        # exactly where the chats left off.
        try:
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            DISABLE_FILE.write_text("1")
        except Exception:
            pass
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
        # blank stray pads and redraw every tracked chat and subagent. Paints one
        # frame; the watch daemon resumes animating from there.
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
