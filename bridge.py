#!/usr/bin/env python3
"""
Centcom -> Deluge bridge.

Replaces the Claude Code hooks. Reads the session snapshot written by the
Centcom dashboard's observer (~/.centcom/state/host-sessions.json) and lights
one Deluge row per open Claude chat:

  busy  -> bright   (working)
  idle  -> dim      (turn finished, chat still open)
  open  -> very dim (open, activity unknown)
  gone  -> off      (row freed)

Low CPU by design: it sleeps inside the OS file-event wait (kqueue on macOS)
and only wakes when Centcom replaces the snapshot. It sends MIDI only for pads
whose brightness changed. No threads, no busy loops, no subprocesses.

Not available from Centcom, so not shown: permission-prompt blinking and
subagent pads.

Usage:
  bridge.py run       run the bridge (what the LaunchAgent starts)
  bridge.py once      read the snapshot, paint once, print what it did, exit
  bridge.py mute      blank the grid and pause the bridge (for jamming)
  bridge.py unmute    resume
  bridge.py reset     forget row assignments (rows reassign on next update)
"""

import json
import os
import sys
import time
from pathlib import Path

import config

try:
    import select
    HAVE_KQUEUE = hasattr(select, "kqueue")
except Exception:  # pragma: no cover
    HAVE_KQUEUE = False

CLAUDE_DIR = Path.home() / ".claude"
ROWS_FILE = CLAUDE_DIR / "deluge_bridge_rows.json"
DISABLE_FILE = CLAUDE_DIR / "deluge_disabled"   # same mute flag as signal.py
SNAPSHOT = Path(config.CENTCOM_SNAPSHOT)
MAX_BYTES = 2_000_000

GRID = list(range(config.BASE_NOTE, config.BASE_NOTE + config.NUM_ROWS * config.ROW_WIDTH))
VELOCITY = {
    "busy": config.SOLID_VELOCITY,
    "idle": config.IDLE_VELOCITY,
    "open": config.OPEN_VELOCITY,
}


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def is_muted() -> bool:
    return DISABLE_FILE.exists()


def note_for(row: int) -> int:
    physical = (config.NUM_ROWS - 1 - row) if config.FILL_FROM_BOTTOM else row
    return config.BASE_NOTE + physical * config.ROW_WIDTH


# --- Snapshot ----------------------------------------------------------------
def read_snapshot():
    """Return (collected_at, {session_id: status}) from a fresh, valid snapshot,
    or None if the
    snapshot is missing, stale, malformed, or reports collector errors. None
    means "don't know": the caller keeps the grid as it is."""
    try:
        if SNAPSHOT.stat().st_size > MAX_BYTES:
            return None
        with open(SNAPSHOT) as f:  # reopen by name every time (file is replaced)
            data = json.load(f)
        if data.get("version") != 1 or data.get("errors"):
            return None
        age = time.time() - float(data["collected_at"])
        if not (0 <= age <= config.SNAPSHOT_MAX_AGE_S):
            return None
        out = {}
        for s in data["sessions"][:500]:
            sid = str(s["session_id"])
            status = s.get("status")
            out[sid] = status if status in VELOCITY else "open"
        return data["collected_at"], out
    except Exception:
        return None


# --- Row assignment (persisted, stable, gaps kept) ---------------------------
class Rows:
    def __init__(self):
        self.rows = {}     # session_id -> row
        self.absent = {}   # session_id -> consecutive healthy snapshots missing
        try:
            raw = json.loads(ROWS_FILE.read_text())
            self.rows = {k: int(v) for k, v in raw.get("rows", {}).items()
                         if 0 <= int(v) < config.NUM_ROWS}
        except Exception:
            pass

    def save(self) -> None:
        try:
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = ROWS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"rows": self.rows}, indent=2))
            os.replace(tmp, ROWS_FILE)
        except Exception:
            pass

    def update(self, sessions: dict) -> None:
        changed = False
        for sid in list(self.rows):
            if sid in sessions:
                self.absent.pop(sid, None)
                continue
            self.absent[sid] = self.absent.get(sid, 0) + 1
            if self.absent[sid] >= config.ABSENT_SCANS_TO_FREE:
                del self.rows[sid]
                self.absent.pop(sid, None)
                changed = True
        used = set(self.rows.values())
        for sid in sessions:  # snapshot order; only new IDs get a row
            if sid in self.rows:
                continue
            free = next((r for r in range(config.NUM_ROWS) if r not in used), None)
            if free is None:
                continue  # grid full: this chat isn't shown until a row frees
            self.rows[sid] = free
            used.add(free)
            changed = True
        if changed:
            self.save()

    def desired(self, sessions: dict, last: dict) -> dict:
        """note -> velocity. A chat missing from this snapshot but not yet freed
        keeps its last brightness."""
        out = {}
        for sid, row in self.rows.items():
            n = note_for(row)
            if sid in sessions:
                out[n] = VELOCITY[sessions[sid]]
            else:
                out[n] = last.get(n, 0)
        return out


# --- MIDI --------------------------------------------------------------------
class Deluge:
    """Holds one open output port. Sends only changed pads. On any send error,
    drops the port and repaints everything once it's back."""

    def __init__(self):
        self.port = None
        self.painted = {}
        self.next_try = 0.0

    def _find(self):
        import mido
        names = mido.get_output_names()
        for match in (lambda n: n == config.DELUGE_PORT_NAME,
                      lambda n: config.DELUGE_PORT_NAME in n,
                      lambda n: "Deluge" in n):
            for n in names:
                if match(n):
                    return n
        return None

    def ensure(self) -> bool:
        if self.port is not None:
            return True
        now = time.monotonic()
        if now < self.next_try:
            return False
        self.next_try = now + 5.0
        try:
            import mido
            name = self._find()
            if name is None:
                return False
            self.port = mido.open_output(name)
            self.painted = {}  # device may have been power-cycled: full repaint
            log(f"connected to {name}")
            return True
        except Exception as e:
            log(f"connect failed: {e}")
            self.port = None
            return False

    def drop(self) -> None:
        try:
            if self.port is not None:
                self.port.close()
        except Exception:
            pass
        self.port = None

    def paint(self, desired: dict) -> None:
        if not self.ensure():
            return
        try:
            import mido
            for n in GRID:
                vel = desired.get(n, 0)
                if self.painted.get(n) != vel:
                    self.port.send(mido.Message("note_on", channel=config.MIDI_CHANNEL,
                                                note=n, velocity=vel))
                    self.painted[n] = vel
        except Exception as e:
            log(f"send failed, will reconnect: {e}")
            self.drop()

    def still_present(self) -> bool:
        """Cheap check that the port still exists (catches unplug on macOS,
        where sends to a vanished port can fail silently)."""
        if self.port is None:
            return False
        try:
            import mido
            if self.port.name in mido.get_output_names():
                return True
        except Exception:
            return True  # can't tell; assume fine
        log("Deluge disappeared")
        self.drop()
        return False


# --- Event wait ----------------------------------------------------------------
class Watcher:
    """Blocks until the snapshot directory changes or `timeout` passes.
    macOS: kqueue on the directory (zero CPU while waiting).
    Elsewhere: sleeps and checks the file's mtime."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.kq = None
        self.fd = None
        self.last_mtime = None

    def _arm(self) -> None:
        if not HAVE_KQUEUE or self.kq is not None:
            return
        try:
            self.fd = os.open(str(self.dir), os.O_RDONLY)
            self.kq = select.kqueue()
            ev = select.kevent(self.fd, filter=select.KQ_FILTER_VNODE,
                               flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                               fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE
                               | select.KQ_NOTE_RENAME)
            self.kq.control([ev], 0, 0)
        except Exception:
            self.close()

    def close(self) -> None:
        for obj in (self.kq,):
            try:
                obj and obj.close()
            except Exception:
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
        self.kq = self.fd = None

    def wait(self, timeout: float) -> None:
        self._arm()
        if self.kq is not None:
            try:
                events = self.kq.control(None, 4, timeout)
                if any(e.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME)
                       for e in events):
                    self.close()  # directory itself moved: re-arm next time
                return
            except Exception:
                self.close()
        # Fallback: no kqueue (Linux) or directory not there yet.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2.0)
            try:
                m = SNAPSHOT.stat().st_mtime
            except Exception:
                m = None
            if m != self.last_mtime:
                self.last_mtime = m
                return


# --- Main loop -----------------------------------------------------------------
def step(rows: Rows, deluge: Deluge, state: dict) -> None:
    if is_muted():
        if not state.get("muted"):
            deluge.paint({})
            state["muted"] = True
        return
    if state.get("muted"):
        state["muted"] = False
        deluge.painted = {}
    snap = read_snapshot()
    if snap is None or snap[0] == state.get("last_at"):
        # Unknown (observer offline, stale, or errors), or the same snapshot as
        # last time (one file replace can wake us twice). Keep pads as they are,
        # but still repaint if the Deluge reconnected and lost its LEDs.
        if deluge.port is None or not deluge.painted:
            deluge.paint(state.get("last", {}))
        return
    state["last_at"], sessions = snap
    rows.update(sessions)
    desired = rows.desired(sessions, state.get("last", {}))
    state["last"] = desired
    deluge.paint(desired)


def run() -> None:
    log(f"bridge started; watching {SNAPSHOT}"
        f" ({'kqueue' if HAVE_KQUEUE else 'mtime check every 2s'})")
    rows, deluge, watcher, state = Rows(), Deluge(), Watcher(SNAPSHOT.parent), {}
    last_presence_check = 0.0
    while True:
        try:
            step(rows, deluge, state)
            now = time.monotonic()
            if now - last_presence_check >= config.BRIDGE_IDLE_WAKE_S:
                last_presence_check = now
                if not deluge.still_present():
                    deluge.ensure()
        except Exception as e:
            log(f"error: {e}")
        watcher.wait(config.BRIDGE_IDLE_WAKE_S)


def once() -> None:
    snap = read_snapshot()
    if snap is None:
        print(f"snapshot unavailable (missing, stale, or has errors): {SNAPSHOT}")
        return
    sessions = snap[1]
    rows, deluge = Rows(), Deluge()
    rows.update(sessions)
    desired = rows.desired(sessions, {})
    for sid, row in sorted(rows.rows.items(), key=lambda kv: kv[1]):
        print(f"row {row}  note {note_for(row):3d}  {sessions.get(sid, '?'):5s}  {sid}")
    if not is_muted():
        deluge.paint(desired)
        print("painted" if deluge.port else "Deluge not found; nothing sent")


def main(argv) -> None:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        run()
    elif cmd == "once":
        once()
    elif cmd == "mute":
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        DISABLE_FILE.touch()
        print("muted (the running bridge blanks the grid on its next wake)")
    elif cmd == "unmute":
        DISABLE_FILE.unlink(missing_ok=True)
        print("unmuted")
    elif cmd == "reset":
        ROWS_FILE.unlink(missing_ok=True)
        print("row assignments cleared (restart the bridge to apply)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
