# deluge-claude

Turn a [Synthstrom Deluge](https://synthstrom.com/product/deluge/) into a live
hardware status display for [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
agent activity, over USB MIDI. Every Claude Code chat lights up its own row of
grid pads; the chat's subagents extend along that row.

Everything is local. Claude Code hooks fire a fire-and-forget Python script that
sends MIDI to the Deluge. If the Deluge is unplugged (or muted), Claude Code runs
completely normally — every hook exits 0 with no error and negligible latency.

## How it looks

The Deluge grid becomes a status board (grid is 16 pads wide):

```
row 0:  [chat A][A.sub1][A.sub2] ...
row 1:  [chat B][B.sub1] ...
row 2:  [chat C] ...
```

- Each **chat** (Claude Code session) claims the next free **row**; its own pad
  is the first pad in that row.
- Each **subagent** the chat spawns lights the next pad to the right in the same
  row, and clears when it finishes.

### Visual language

| State            | Pad appearance              |
| ---------------- | --------------------------- |
| Working          | solid bright white          |
| Idle (waiting)   | dim                         |
| Needs permission | blinking                    |
| Done / closed    | brief flash, then off / dim |

## Requirements

- Python 3
- [`mido`](https://mido.readthedocs.io/) and
  [`python-rtmidi`](https://pypi.org/project/python-rtmidi/):

  ```bash
  pip3 install mido python-rtmidi --break-system-packages
  ```

- A Deluge on community firmware with the **Norns / "Highlight Incoming Notes"**
  grid layout enabled, so incoming notes on **MIDI channel 16** light pads
  (velocity = brightness). Connect it over USB; it enumerates as `Deluge Port 1`.

## Setup

1. Clone this repo to `~/dev/deluge-claude` (the path the hooks reference; edit
   `.claude/settings.json` if you put it elsewhere).
2. The committed `.claude/settings.json` wires all the hooks. To use it in
   another project, copy that `hooks` block into that project's Claude Code
   settings, or into `~/.claude/settings.json` to apply it globally.
3. Restart Claude Code so it loads the hooks.

## Usage

The status display runs itself via hooks. The only manual commands you need:

```bash
# Mute everything (for jamming): blanks the grid, all hooks no-op until enabled
python3 ~/dev/deluge-claude/signal.py disable < /dev/null

# Unmute: hooks resume lighting pads
python3 ~/dev/deluge-claude/signal.py enable < /dev/null

# Blank the whole grid and wipe all chat/slot state
python3 ~/dev/deluge-claude/signal.py reset < /dev/null
```

`disable`/`enable`/`reset` always work, even while muted, so you can't get stuck.
The mute flag persists across restarts (it's a file at `~/.claude/deluge_disabled`),
so if pads stop lighting up, check whether you left it disabled.

## Events

`signal.py` takes the event name as its first argument and reads Claude Code's
hook JSON payload on stdin:

| Event                | Fires when                | Effect                              |
| -------------------- | ------------------------- | ----------------------------------- |
| `session_start`      | chat opened               | claim a row; pad dim (idle)         |
| `working`            | prompt submitted          | chat's pad solid                    |
| `permission_request` | Claude needs permission   | blink that pad                      |
| `posttool`           | a tool finished           | clear a pending blink → solid       |
| `stop`               | chat finished a turn      | flash, then dim (idle); keeps row   |
| `session_end`        | chat closed               | free the row + its subagents, off   |
| `subagent_start`     | subagent spawned          | next pad in its chat's row, solid   |
| `subagent_stop`      | subagent finished         | flash, then off; free that pad      |
| `disable` / `enable` | manual                    | mute / unmute                       |
| `reset`              | manual                    | blank grid + wipe state             |

Add `--debug` to append raw stdin payloads to `~/.claude/hook_debug.log`.

## Files

- `signal.py` — the hook script (the whole tool).
- `.claude/settings.json` — committed Claude Code hook wiring.
- `light_test.py`, `midi_probe.py`, `listen_midi.py`, `probe_midi.py` — dev
  utilities for probing the Deluge's MIDI in/out and testing pad lighting.

## Tuning

Constants at the top of `signal.py`:

- `BASE_NOTE` — where the block starts (default `0`, top-left).
- `ROW_WIDTH` — grid width / row stride (default `16`). If new chats appear a row
  down instead of up, or the width differs, adjust this.
- `NUM_ROWS` — max concurrent chats (default `8`).
- `MIDI_CHANNEL` — `15` (zero-indexed) == MIDI channel 16.
- Velocities/timing: `SOLID_VELOCITY`, `IDLE_VELOCITY`, `FLASH_MS`, `BLINK_INTERVAL_S`.

## Notes & limits

- **Permission prompts can't be attributed to a specific subagent** — permission
  events don't carry an `agent_id`, so a blink lands on the main chat's pad.
- Colors are white only; brightness + blink convey state.
- Runtime state lives in `~/.claude/` (outside this repo): `deluge_slots.json`,
  blink pidfiles, `deluge_disabled`, `hook_debug.log`.
