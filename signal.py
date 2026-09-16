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
state -- see config.py). Blinking is reserved for the states that want your
attention, and the rate says how badly:
  working        -> bright, STEADY      (velocity 127, no blink)
  done working   -> dim, SLOW blink     (60 <-> 15, ~0.9s)
  needs approval -> bright, FAST blink  (127 <-> 0, ~0.18s)
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
  sessions            print tracked chats + whether their processes are alive
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
UNTRACKED_TTL_S = config.UNTRACKED_TTL_S

# Brightness / blink rates
SOLID_VELOCITY = config.SOLID_VELOCITY
IDLE_HIGH_VELOCITY = config.IDLE_HIGH_VELOCITY
IDLE_LOW_VELOCITY = config.IDLE_LOW_VELOCITY
PERM_VELOCITY = config.PERM_VELOCITY
PERM_LOW_VELOCITY = config.PERM_LOW_VELOCITY
PERM_BLINK_S = config.PERM_BLINK_S
IDLE_BLINK_S = config.IDLE_BLINK_S
WATCH_INTERVAL_S = config.WATCH_INTERVAL_S
WATCH_STATE_POLL_S = config.WATCH_STATE_POLL_S
WATCH_LIVENESS_S = config.WATCH_LIVENESS_S
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
    if not isinstance(state.get("owners"), dict):
        state["owners"] = {}
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


# --- Owning process ----------------------------------------------------------
# Claude Code can't always tell us a chat closed: closing a terminal window or an
# editor tab kills the process outright, so there's no clean shutdown in which a
# SessionEnd hook could run. Rather than trust an event that may never arrive, we
# record WHICH PROCESS owns each chat and let the watch daemon notice when that
# process is gone. Pads then clear on close no matter how the chat died.
#
# A hook runs as a descendant of the Claude Code process that fired it, so we
# find the owner by walking up our own ancestry. We store the pid together with
# its start time, because pids get recycled and a stale pid that some unrelated
# process later inherits would keep a dead chat's pad lit forever.
_CLAUDE_MARKERS = ("@anthropic-ai/claude-code", "claude-code/cli.js")


def _looks_like_claude(command: str) -> bool:
    """True if this process is a Claude Code CLI.

    Matched on argv[0]'s basename, never a substring of the whole command line:
    this script usually lives in a directory with `claude` in its name, so a
    substring test would happily identify our own hook process as the chat.
    """
    argv0 = command.split()[0] if command.split() else ""
    if os.path.basename(argv0) == "claude":
        return True
    return any(marker in command for marker in _CLAUDE_MARKERS)


def _process_table() -> dict:
    """{pid: (ppid, command)} for every process, in one `ps` call."""
    table = {}
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,command="],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3:
                table[int(parts[0])] = (int(parts[1]), parts[2])
    except Exception:
        pass
    return table


def _started_at(pid: int) -> str:
    """The process's start time, as a stable string. Empty if it's gone."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


def find_owner_process():
    """Walk up our own ancestry to the Claude Code process that fired this hook.
    Returns {'pid': int, 'started': str}, or None if we can't identify one (in
    which case that chat just falls back to idle expiry)."""
    table = _process_table()
    if not table:
        return None
    pid = os.getpid()
    for _ in range(12):  # generous depth cap; the chain is normally 2-3 deep
        entry = table.get(pid)
        if entry is None:
            return None
        ppid, _cmd = entry
        if ppid <= 1:
            return None
        parent = table.get(ppid)
        if parent is None:
            return None
        if _looks_like_claude(parent[1]):
            started = _started_at(ppid)
            return {"pid": ppid, "started": started} if started else None
        pid = ppid
    return None


def record_owner(sid: str) -> None:
    """Remember which process owns `sid`, if we don't already know. Costs one or
    two `ps` calls the first time a chat is seen, then nothing."""
    try:
        if not sid or sid in _norm(load_state())["owners"]:
            return
        owner = find_owner_process()
        if owner is None:
            return
        with state_lock():
            state = _norm(load_state())
            state["owners"][sid] = owner
            save_state(state)
    except Exception:
        pass


def sweep_sessions():
    """Clear chats that are gone, by whichever test applies. Returns notes.

    Run by the watch daemon rather than by hooks, because a closed chat fires no
    hooks: if it were only checked on someone else's event, the last chat you
    close would leave its pad lit until you started another one.
    """
    notes = list(prune_dead_sessions())
    try:
        now = time.time()
        with state_lock():
            state = _norm(load_state())
            idle_notes = _expire_idle(state, now)
            if idle_notes:
                save_state(state)
        notes.extend(idle_notes)
    except Exception:
        pass
    return notes


def prune_dead_sessions():
    """Clear every chat whose owning process has exited. Returns notes to blank.

    A recorded pid counts as dead if it's gone, or if it's alive but started at a
    different time than we recorded -- that means the pid was recycled and now
    belongs to something else entirely.
    """
    try:
        owners = _norm(load_state())["owners"]
        if not owners:
            return []
        pids = sorted({int(o["pid"]) for o in owners.values() if o.get("pid")})
        alive = {}
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in pids)],
                capture_output=True, text=True, timeout=5)
            for line in out.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    alive[int(parts[0])] = parts[1].strip()
        except Exception:
            return []  # can't tell; never guess a chat dead

        notes = []
        with state_lock():
            state = _norm(load_state())
            for sid, owner in list(state["owners"].items()):
                pid, started = owner.get("pid"), owner.get("started", "")
                if alive.get(pid) == started:
                    continue  # same process, still running
                if sid in state["sessions"]:
                    notes.extend(_remove_session(state, sid))
                else:
                    state["owners"].pop(sid, None)
            if notes:
                save_state(state)
        return notes
    except Exception:
        return []


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
    state.get("owners", {}).pop(sid, None)
    if row is not None:
        notes.append(note_for(row, 0))
        for aid in [k for k, v in agents.items() if v.get("session") == sid]:
            a = agents.pop(aid)
            notes.append(note_for(row, a["col"]))
    if notes:
        marked = set(state.get("blink") or [])
        if marked & set(notes):
            state["blink"] = sorted(marked - set(notes))
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


def _pid_share_counts(state: dict) -> dict:
    """{pid: how many chats claim it}. Snapshotted before any expiry runs: each
    removal drops an owner entry, so counting lazily would make the last chat
    sharing a process look like it had one to itself and stop expiring."""
    counts = {}
    for owner in state["owners"].values():
        pid = owner.get("pid")
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def _effective_ttl(state: dict, sid: str, shares=None) -> int:
    """How long this chat may sit idle before we assume it's gone.

    A chat we can watch by process needs no timeout at all: it disappears the
    moment its process does, so leaving one open and untouched all afternoon
    keeps its pad. A timeout is only a guess for chats we CAN'T watch, and there
    are two of those:

      - no owner process was identified at all, and
      - several chats share one process, so the process being alive says nothing
        about whether this particular chat is still open (an editor that runs one
        Claude Code process behind several tabs looks like this).

    Those fall back to UNTRACKED_TTL_S, which is short on purpose -- guessing
    late leaves dead pads lit, and guessing early costs nothing, since the pad
    comes straight back on the chat's next prompt.
    """
    owner = state["owners"].get(sid)
    if not owner:
        return UNTRACKED_TTL_S
    if shares is None:
        shares = _pid_share_counts(state)
    return UNTRACKED_TTL_S if shares.get(owner.get("pid"), 0) > 1 else SESSION_TTL_S


def _expire_idle(state: dict, now: float, skip_sid=None):
    """Drop chats that finished a turn and then sat idle past their TTL
    (unlocked; caller holds the lock). Returns notes to blank.

    A chat that is actively working has no idle clock and is never expired, no
    matter how long it runs, and a pad waiting on your approval is never expired
    either -- that signal has to survive until you deal with it.
    """
    notes = []
    shares = _pid_share_counts(state)
    for sid in list(state["sessions"].keys()):
        if sid == skip_sid:
            continue
        ttl = _effective_ttl(state, sid, shares)
        if ttl <= 0:
            continue
        if _session_is_blinking(state, sid):
            continue
        ts = state["idle_since"].get(sid)
        if ts is None:  # never finished a turn (still working): keep
            continue
        if now - ts > ttl:
            notes.extend(_remove_session(state, sid))
    return notes


def touch_and_prune(sid: str, event: str):
    """Update `sid`'s idle/active bookkeeping for this event, then expire any
    OTHER chat that has been idle past its TTL. Returns notes to blank."""
    now = time.time()
    with state_lock():
        state = _norm(load_state())
        if sid:
            state["seen"][sid] = now
            if event in _IDLE_EVENTS:
                state["idle_since"][sid] = now   # finished/waiting -> idle clock
            elif event in _ACTIVE_EVENTS:
                state["idle_since"].pop(sid, None)  # working -> not expirable
        expired_notes = _expire_idle(state, now, skip_sid=sid)
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


def idle_velocity_at(now: float) -> int:
    """Done working: dim, SLOW blink -- wants you back, but it isn't urgent."""
    return IDLE_HIGH_VELOCITY if _blink_high(now, IDLE_BLINK_S) else IDLE_LOW_VELOCITY


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


def print_sessions() -> None:
    """Print what the display currently thinks is going on. Mostly for checking
    that owner-process tracking actually found your Claude Code processes: a chat
    showing `owner=?` couldn't be tied to one, so its pad will hang around until
    idle expiry instead of clearing the moment you close it."""
    state = _norm(load_state())
    if not state["sessions"]:
        print("No chats tracked. The grid should be blank.")
        return
    marked = blink_notes(state)
    print(f"{'chat':<40} {'pad':>4}  {'state':<14} owner")
    for sid, row in sorted(state["sessions"].items(), key=lambda kv: kv[1]):
        note = note_for(row, 0)
        if note in marked:
            label = "needs approval"
        elif sid in state["idle_since"]:
            label = "done"
        else:
            label = "working"
        owner = state["owners"].get(sid)
        if not owner:
            where = "?  (falls back to idle expiry)"
        else:
            live = _started_at(int(owner["pid"])) == owner.get("started", "")
            where = f"pid {owner['pid']} {'alive' if live else 'GONE -> clearing'}"
        subs = sum(1 for a in state["agents"].values() if a.get("session") == sid)
        print(f"{sid:<40} {note:>4}  {label:<14} {where}"
              + (f"   (+{subs} subagent{'s' if subs != 1 else ''})" if subs else ""))


# --- Main --------------------------------------------------------------------
def main() -> None:
    # Diagnostic: dump tracked chats and whether their processes are still alive.
    if len(sys.argv) >= 2 and sys.argv[1] == "sessions":
        try:
            print_sessions()
        except Exception as exc:
            print(f"couldn't read state: {exc}")
        sys.exit(0)

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

      working        -> steady SOLID_VELOCITY (bright, no blink)
      done working   -> slow blink between IDLE_HIGH_VELOCITY and IDLE_LOW_VELOCITY
      needs approval -> fast blink between PERM_VELOCITY and PERM_LOW_VELOCITY

    A chat counts as working until it finishes a turn (which is what puts it in
    `idle_since`); subagents only exist in state while they're running, so their
    pads are always solid. Notes not listed here are off (0). Needing approval
    wins over everything -- that's the one you have to act on.
    """
    desired = {}
    sessions = state["sessions"]
    idle = state["idle_since"]
    idle_vel = idle_velocity_at(now)
    perm_vel = perm_velocity_at(now)
    for sid, row in sessions.items():
        desired[note_for(row, 0)] = idle_vel if sid in idle else SOLID_VELOCITY
    for a in state["agents"].values():
        row = sessions.get(a.get("session"))
        if row is not None:
            desired[note_for(row, a.get("col", 0))] = SOLID_VELOCITY
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
    It also sweeps for dead chats: a chat whose owning Claude Code process has
    exited is cleared here, which is how a pad goes out when you close a window
    or tab rather than exiting cleanly.
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
        last_liveness = 0.0
        try:
            with mido.open_output(port_name) as out:
                while True:
                    now = time.time()
                    if now - last_full > WATCH_FULL_REPAINT_S:
                        painted.clear()  # heal any silent drift
                        last_full = now

                    # Clear chats that are gone: process exited (window closed,
                    # or Claude Code crashed), or idle past their TTL. Neither
                    # case fires a hook, so the daemon has to notice on its own.
                    if WATCH_LIVENESS_S > 0 and now - last_liveness >= WATCH_LIVENESS_S:
                        last_liveness = now
                        if sweep_sessions():
                            state = None  # state changed: re-read it below

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
        if event != "session_end":
            # Remember the process behind this chat, so its pad clears even if
            # the chat is later killed without firing session_end.
            record_owner(sid)
        for note in touch_and_prune(sid, event):
            stop_blink(note)
            send_note(note, 0)

    if event == "session_start":
        # A new chat opened -> claim its row; it starts in the waiting-on-you
        # state, so the daemon gives its pad the slow "done" blink.
        note = note_for(claim_session_row(sid), 0)
        send_note(note, IDLE_HIGH_VELOCITY)

    elif event == "working":
        # Chat submitted a prompt -> working: bright and steady, no blink. It
        # wants nothing from you, so it shouldn't pull your eye.
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
            # Pad was fast-blinking for a permission prompt -> approved, so go
            # back to steady working. Otherwise stay cheap: no MIDI here.
            stop_blink(note, final_velocity=SOLID_VELOCITY)

    elif event == "stop":
        # Chat finished responding -> drop out of the fast blink and hand the pad
        # to the slow "done, come back" blink. Keep the row; the pad stays
        # visible until the chat closes.
        note = peek_session_note(sid)
        if note is not None:
            stop_blink(note)
            send_note(note, IDLE_HIGH_VELOCITY)

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
