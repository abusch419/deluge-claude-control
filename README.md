# deluge-claude

Turn a [Synthstrom Deluge](https://synthstrom.com/product/deluge/) into a live
hardware status display for [Claude Code](https://docs.anthropic.com/en/docs/claude-code),
over USB MIDI. Each Claude Code chat lights up its own row of grid pads; the
chat's subagents extend along that row.

Everything is local. Claude Code hooks fire a small, fire-and-forget Python
script that sends MIDI to the Deluge. **If the Deluge is unplugged (or muted),
Claude Code runs completely normally** — every hook exits 0 with no error and
negligible latency.

> **How it's triggered: Claude Code hooks — required.** This tool does nothing on
> its own. Claude Code calls `signal.py` from hooks it fires on session/prompt/
> tool/subagent events, and those calls are what light the pads. Without the hooks
> wired up (step 5 below), nothing will happen.

## What the lights mean

At a glance, a pad tells you what a chat is doing:

| Pad appearance | Meaning |
| -------------- | ------- |
| **Blinking**   | needs your input (permission request) |
| **Bright**     | working |
| **Dim**        | idle (turn finished, chat still open) |
| **Off**        | done / chat closed |

Layout (the Deluge main grid is 16 pads wide):

```
row 0:  [chat A][A.sub1][A.sub2] ...
row 1:  [chat B][B.sub1] ...
row 2:  [chat C] ...
```

Each **chat** claims the next free **row**; its own pad is the first pad in that
row. Each **subagent** the chat spawns lights the next pad to the right.

---

## Setup

### 1. Install dependencies

Requires Python 3. **Use a dedicated virtualenv** and reference it by absolute
path everywhere (hooks + service). This is important: Claude Code runs hooks in a
non-interactive shell where bare `python3` may resolve to a Python that does
**not** have `mido` installed — in which case every MIDI send silently fails and
nothing lights up. A pinned venv path avoids that entirely.

```bash
cd /path/to/deluge-claude
python3 -m venv .venv
./.venv/bin/python3 -m pip install --upgrade pip
./.venv/bin/python3 -m pip install mido python-rtmidi
# verify:
./.venv/bin/python3 -c "import mido, rtmidi; print('ok', mido.get_output_names())"
```

Use `/path/to/deluge-claude/.venv/bin/python3` as the interpreter in the hooks
(step 5) and the watch service (step 6).

### 2. Configure the Deluge (on the device)

This project relies on the Deluge community firmware's **Midigrid** feature.

1. **Enable Midigrid:** `SETTINGS > COMMUNITY FEATURES > Midigrid` → **ON**.
   This is **off by default**. It's what lets incoming MIDI notes light the grid
   pads (white, velocity = brightness) *and* makes the pads send notes out.
2. **Build a kit clip** with rows, and make that clip the **active context** on
   the grid. Pad lighting only works while that clip view is active.

Both directions use **MIDI channel 16** (which mido calls channel `15`).

### 3. Find YOUR port name and note numbers

Note numbers and the MIDI port name are **hardware-specific** — they depend on
your grid layout. Do **not** assume the author's values. Discover yours:

```bash
python3 midi_probe.py      # lists MIDI input ports; tap pads to see their notes + channel
python3 light_test.py      # sends notes back; use `sweep` / `fill` to see which pads light
```

In `light_test.py`, useful commands: `fill` (light everything), `sweep`
(one at a time), `on <note>`, `off <note>`, `clear`.

> The author's setup happens to be **notes 60–67 on "Deluge Port 1", channel 16
> (mido 15)**. These are defaults, not universal — set your own below.

### 4. Set your config

Edit `config.py` (or override any value with an environment variable of the same
name). The important ones:

- `DELUGE_PORT_NAME` — the port from step 3 (e.g. `"Deluge Port 1"`).
- `MIDI_CHANNEL` — zero-indexed; `15` == MIDI channel 16.
- `BASE_NOTE` — the first grid pad to use.
- `ROW_WIDTH` — grid width / row stride (16 on a standard Deluge).
- `NUM_ROWS` — max concurrent chats.
- `SESSION_TTL_S` — a chat's pad auto-clears once it has **finished a turn and
  then stayed idle** this many seconds (default 7200 = 2h; `0` disables it). See
  "Idle expiry" below.
- `IDLE_VELOCITY` — brightness of a finished/idle pad (default 25; keep it well
  below `SOLID_VELOCITY` so working vs. idle is obvious, but high enough to see).
- `WATCH_INTERVAL_S` — how often the watch service reconciles the grid.

Verify it can talk to the device:

```bash
python3 signal.py reset < /dev/null   # should blank the grid and exit cleanly
```

### 5. Wire up the Claude Code hooks

**This is what makes the tool run.** The hooks live in **your own** Claude Code
settings, not in this repo. Put the block below into either:

- a project's `.claude/settings.local.json` (that project only), or
- `~/.claude/settings.json` (all projects).

**Replace `/path/to/deluge-claude`** with the absolute path where you cloned this
repo. If you already have a `hooks` block, merge these events into it. The same
block is in [`settings.example.json`](settings.example.json) if you prefer to
copy from a file.

Use the **absolute venv Python path** (from step 1) as the interpreter, not bare
`python3` (see the warning in step 1).

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "/path/to/deluge-claude/.venv/bin/python3 /path/to/deluge-claude/signal.py session_start" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "/path/to/deluge-claude/.venv/bin/python3 /path/to/deluge-claude/signal.py session_end" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "/path/to/deluge-claude/.venv/bin/python3 /path/to/deluge-claude/signal.py working" }] }],
    "PermissionRequest":[{ "hooks": [{ "type": "command", "command": "/path/to/deluge-claude/.venv/bin/python3 /path/to/deluge-claude/signal.py permission_request" }] }],
    "PostToolUse":      [{ "hooks": [{ "type": "command", "command": "/path/to/deluge-claude/.venv/bin/python3 /path/to/deluge-claude/signal.py posttool" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "/path/to/deluge-claude/.venv/bin/python3 /path/to/deluge-claude/signal.py stop" }] }],
    "SubagentStart":    [{ "hooks": [{ "type": "command", "command": "/path/to/deluge-claude/.venv/bin/python3 /path/to/deluge-claude/signal.py subagent_start" }] }],
    "SubagentStop":     [{ "hooks": [{ "type": "command", "command": "/path/to/deluge-claude/.venv/bin/python3 /path/to/deluge-claude/signal.py subagent_stop" }] }]
  }
}
```

Then **restart Claude Code** so it loads the hooks.

### 6. Run the watch service (keeps the grid in sync)

Hooks only paint a pad at the instant they fire, so after a Deluge power-cycle,
unplug, or a Mac sleep the grid would drift out of sync. The **watch daemon**
fixes this: it continuously repaints the grid from tracked state (every second)
and self-heals whenever the device reconnects.

Run it once to try it:

```bash
/path/to/deluge-claude/.venv/bin/python3 /path/to/deluge-claude/signal.py watch
```

To have it always running (auto-start at login, restart if it dies), install the
launchd agent. Copy [`com.deluge-claude.watch.plist.example`](com.deluge-claude.watch.plist.example)
to `~/Library/LaunchAgents/com.deluge-claude.watch.plist`, replace the paths
inside, then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.deluge-claude.watch.plist
launchctl enable   gui/$(id -u)/com.deluge-claude.watch
# check it: launchctl print gui/$(id -u)/com.deluge-claude.watch | grep state
```

**Verify the hooks fire:** append `--debug` to any hook command (e.g.
`... signal.py working --debug`), restart, use a chat, then check that entries
appear in `~/.claude/hook_debug.log`. If the file stays empty, the hooks aren't
wired into the settings Claude Code is actually reading, or it needs a restart.

---

## Usage

Once wired up, the display runs itself. The only manual commands you need:

```bash
# Mute everything (e.g. to jam on the Deluge): blanks the grid, hooks become no-ops
python3 /path/to/deluge-claude/signal.py disable < /dev/null

# Unmute: hooks resume lighting pads
python3 /path/to/deluge-claude/signal.py enable < /dev/null

# Blank the whole grid and wipe all chat/slot state
python3 /path/to/deluge-claude/signal.py reset < /dev/null
```

`disable` / `enable` / `reset` always work, even while muted, so you can't get
stuck. The mute flag persists across restarts (a file at `~/.claude/deluge_disabled`);
if pads stop lighting up, check whether you left it disabled.

---

## How it works

`signal.py` takes an event name as its first argument and reads Claude Code's
hook JSON payload from stdin:

| Event                | Fires when              | Effect                             |
| -------------------- | ----------------------- | ---------------------------------- |
| `session_start`      | chat opened             | claim a row; pad dim (idle)        |
| `working`            | prompt submitted        | chat's pad solid                   |
| `permission_request` | Claude needs permission | blink that pad                     |
| `posttool`           | a tool finished         | clear a pending blink → solid      |
| `stop`               | chat finished a turn    | flash, then dim (idle); keeps row  |
| `session_end`        | chat closed             | free the row + its subagents, off  |
| `subagent_start`     | subagent spawned        | next pad in its chat's row, solid  |
| `subagent_stop`      | subagent finished       | flash, then off; free that pad     |
| `disable` / `enable` | manual                  | mute / unmute                      |
| `reset`              | manual                  | blank grid + wipe state            |
| `refresh`            | manual                  | re-sync grid to state (no wipe)    |
| `watch`              | service                 | continuously reconcile the grid    |

Add `--debug` to append raw stdin payloads to `~/.claude/hook_debug.log`.

### Idle expiry

Claude Code doesn't reliably fire a "chat closed" event — in particular, the VS
Code extension does **not** fire `SessionEnd` when you close a tab. So pads would
otherwise pile up and drift from the chats you actually have open. To prevent
that, a chat's pad **auto-clears once it has finished a turn (`Stop`) and then
stayed idle for `SESSION_TTL_S` seconds** (default 2h). A chat that is actively
working has no idle clock and is **never** expired, no matter how long it runs;
a blinking (needs-you) pad is never expired either. Set `SESSION_TTL_S=0` to
disable expiry entirely and only clear via a manual `reset`.

Runtime state lives in `~/.claude/` (outside this repo): `deluge_slots.json`,
blink pidfiles, `deluge_disabled`, `hook_debug.log`.

---

## Files

| File                    | Purpose |
| ----------------------- | ------- |
| `signal.py`             | the hook script + watch daemon — the whole status display |
| `config.py`             | hardware config (port, channel, notes, timing) |
| `settings.example.json` | hooks block to copy into your Claude Code settings |
| `com.deluge-claude.watch.plist.example` | launchd agent for the auto-start watch service |
| `midi_probe.py`         | list MIDI inputs and see what notes your pads send |
| `light_test.py`         | send notes to the Deluge to find which pads light |

---

## Notes & limits

- **Permission prompts can't be attributed to a specific subagent** — Claude
  Code's permission events don't carry an agent id, so a blink lands on the main
  chat's pad.
- Colors are white only; brightness + blink convey state (Midigrid limitation).
- Requires the Deluge community firmware with Midigrid; stock firmware won't
  light pads from incoming MIDI.
