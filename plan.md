Build a local-only tool that turns my Synthstrom Deluge into a hardware status display for Claude Code agent activity, using MIDI over USB. This must NOT be committed to any git repo — it's a personal utility that lives outside any project.

## Goal
- When Claude Code needs my permission to run a command, send a MIDI note that makes a specific Deluge pad flash bright.
- When the main Claude Code session finishes responding, send a MIDI note that dims that same pad to "idle."
- When a subagent starts, claim the next free "slot" (MIDI note 61–67) and light that pad on.
- When a subagent stops, send velocity 0 for that slot's note to turn its pad off.
- Track which slot belongs to which subagent in a small local JSON state file so slots don't collide when multiple subagents run at once.

## Requirements

1. Location: everything goes in ~/scripts/deluge-status/ (a folder in my home directory, NOT inside any git repo). Create this folder.

2. Language/deps: Python 3, using the `mido` and `python-rtmidi` packages. Install with `pip3 install mido python-rtmidi --break-system-packages`. Check first whether they're already installed before reinstalling.

3. Main script (~/scripts/deluge-status/signal.py):
   - Takes one CLI arg: the event name (subagent_start, subagent_stop, permission_request, stop).
   - Reads Claude Code's hook JSON payload from stdin.
   - Finds the Deluge's MIDI output port by matching "Deluge" in mido.get_output_names(). If not found, exit 0 silently.
   - Maintains state in ~/.claude/deluge_slots.json: dict mapping agent identifier -> slot index (0-7).
   - Slot 0 (note 60) is reserved for the main session. Slots 1-7 (notes 61-67) are for subagents, first-available assignment.
   - subagent_start: claim a free slot, send note_on velocity 100.
   - subagent_stop: release that agent's slot, send note_on velocity 0 for its note.
   - permission_request: send note_on velocity 127 for slot 0 (or the relevant agent's slot if identifiable).
   - stop: send note_on velocity 20 for slot 0.
   - Wrap every MIDI call in try/except — must never throw or block Claude Code, always exit 0.
   - Add a --debug flag that appends raw stdin JSON to ~/.claude/hook_debug.log, so I can inspect real payload field names. I don't know for certain whether subagent identity comes as subagent_id, agent_id, or something else — check common candidates in that order, fall back to session_id, fall back to "main".

4. Claude Code hooks config: add a "hooks" block to .claude/settings.local.json in the current project (find it, merge into it, preserve the existing "permissions" block untouched). Wire up:
   - PermissionRequest -> python3 ~/scripts/deluge-status/signal.py permission_request
   - Stop -> python3 ~/scripts/deluge-status/signal.py stop
   - SubagentStart -> python3 ~/scripts/deluge-status/signal.py subagent_start
   - SubagentStop -> python3 ~/scripts/deluge-status/signal.py subagent_stop

5. Git safety: verify .claude/settings.local.json is gitignored (run git check-ignore -v .claude/settings.local.json). If NOT ignored, add it to .gitignore and tell me explicitly. Confirm nothing else you create lives inside the repo.

6. Test it: call signal.py manually for each event with a fake JSON payload piped to stdin, confirm it doesn't crash whether or not a Deluge is connected.

7. Give me a summary at the end: every file created/modified with full paths, the git-ignore check result, and the MIDI note numbers so I can assign pads in a Deluge kit clip (notes 60-67).

Do not ask me clarifying questions — make reasonable choices per the spec above and build it end to end.