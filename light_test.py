#!/usr/bin/env python3
"""
Interactive test for driving the Synthstrom Deluge's grid LEDs by sending MIDI
from the computer. Requires Midigrid mode enabled on the Deluge (SETTINGS >
COMMUNITY FEATURES) with an active kit clip. Port and channel come from config.py.

Commands (defaults from config.py):
  on <note> [velocity] [channel]   send note_on
  off <note> [channel]             send note_off
  sweep [start] [end]              light notes one at a time, 500ms apart
  blink <note> [count]             pulse a note on/off so it visibly blinks (default 8 pulses)
  vel <note>                       cycle note through velocities 1,32,64,96,127
  fill [velocity]                  light ALL notes 0-127 at once (find the grid mapping)
  clear                            note_off for all notes 0-127
  quit                             exit
"""

import os
import sys
import time

try:
    import mido
    import rtmidi  # noqa: F401  (ensures python-rtmidi backend present)
except ImportError:
    print("Missing dependencies. Install both with:")
    print("    pip install mido python-rtmidi")
    raise SystemExit(1)

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402  (local hardware config)

# Port name and channel come from config.py (edit there or use env vars).
OUTPUT_PORT_NAME = config.DELUGE_PORT_NAME
DEFAULT_CHANNEL = config.MIDI_CHANNEL
DEFAULT_VELOCITY = config.SOLID_VELOCITY


def send(port, msg_type: str, note: int, velocity: int, channel: int) -> None:
    try:
        msg = mido.Message(msg_type, note=note, velocity=velocity, channel=channel)
        port.send(msg)
        print(f"  sent: {msg}")
    except Exception as e:
        print(f"  send failed: {e}")


def do_sweep(port, start: int = config.BASE_NOTE, end: int = config.BASE_NOTE + 7) -> None:
    for note in range(start, end + 1):
        send(port, "note_on", note, DEFAULT_VELOCITY, DEFAULT_CHANNEL)
        time.sleep(0.5)
        send(port, "note_off", note, 0, DEFAULT_CHANNEL)


def do_fill(port, velocity: int = DEFAULT_VELOCITY) -> None:
    for note in range(0, 128):
        send(port, "note_on", note, velocity, DEFAULT_CHANNEL)
        time.sleep(0.01)


def do_clear(port) -> None:
    for note in range(0, 128):
        send(port, "note_off", note, 0, DEFAULT_CHANNEL)
        time.sleep(0.005)


def do_blink(port, note: int, count: int = 8) -> None:
    for _ in range(count):
        send(port, "note_on", note, DEFAULT_VELOCITY, DEFAULT_CHANNEL)
        time.sleep(0.25)
        send(port, "note_on", note, 0, DEFAULT_CHANNEL)
        time.sleep(0.25)


def do_vel(port, note: int) -> None:
    for v in (1, 32, 64, 96, 127):
        print(f"  velocity {v}")
        send(port, "note_on", note, v, DEFAULT_CHANNEL)
        time.sleep(0.8)
    send(port, "note_off", note, 0, DEFAULT_CHANNEL)


def parse_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return fallback


def main() -> None:
    outputs = mido.get_output_names()
    print("Available MIDI OUTPUT ports:")
    for i, name in enumerate(outputs):
        print(f"  [{i}] {name}")
    print()

    if OUTPUT_PORT_NAME not in outputs:
        print(f"WARNING: '{OUTPUT_PORT_NAME}' not found in the list above.")
        print("Edit OUTPUT_PORT_NAME at the top of this script to match one of them.\n")

    try:
        port = mido.open_output(OUTPUT_PORT_NAME)
    except Exception as e:
        print(f"Could not open '{OUTPUT_PORT_NAME}': {e}")
        raise SystemExit(1)

    print(f"Opened output: {OUTPUT_PORT_NAME!r}")
    print(f"Defaults: velocity {DEFAULT_VELOCITY}, channel {DEFAULT_CHANNEL} (MIDI ch {DEFAULT_CHANNEL + 1})\n")
    print("Commands: on <note> [vel] [ch] | off <note> [ch] | sweep [start] [end] | blink <note> [count] | vel <note> | fill [vel] | clear | quit\n")

    with port:
        while True:
            try:
                raw = input("light> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nbye")
                break

            if not raw:
                continue

            parts = raw.split()
            cmd = parts[0].lower()

            if cmd == "quit" or cmd == "q":
                print("bye")
                break

            elif cmd == "on":
                if len(parts) < 2:
                    print("  usage: on <note> [velocity] [channel]")
                    continue
                note = parse_int(parts[1], -1)
                if note < 0:
                    print("  invalid note")
                    continue
                velocity = parse_int(parts[2], DEFAULT_VELOCITY) if len(parts) > 2 else DEFAULT_VELOCITY
                channel = parse_int(parts[3], DEFAULT_CHANNEL) if len(parts) > 3 else DEFAULT_CHANNEL
                send(port, "note_on", note, velocity, channel)

            elif cmd == "off":
                if len(parts) < 2:
                    print("  usage: off <note> [channel]")
                    continue
                note = parse_int(parts[1], -1)
                if note < 0:
                    print("  invalid note")
                    continue
                channel = parse_int(parts[2], DEFAULT_CHANNEL) if len(parts) > 2 else DEFAULT_CHANNEL
                send(port, "note_off", note, 0, channel)

            elif cmd == "sweep":
                start = parse_int(parts[1], config.BASE_NOTE) if len(parts) > 1 else config.BASE_NOTE
                end = parse_int(parts[2], config.BASE_NOTE + 7) if len(parts) > 2 else config.BASE_NOTE + 7
                do_sweep(port, start, end)

            elif cmd == "blink":
                if len(parts) < 2:
                    print("  usage: blink <note> [count]")
                    continue
                note = parse_int(parts[1], -1)
                if note < 0:
                    print("  invalid note")
                    continue
                count = parse_int(parts[2], 8) if len(parts) > 2 else 8
                do_blink(port, note, count)

            elif cmd == "fill":
                velocity = parse_int(parts[1], DEFAULT_VELOCITY) if len(parts) > 1 else DEFAULT_VELOCITY
                do_fill(port, velocity)

            elif cmd == "clear":
                do_clear(port)

            elif cmd == "vel":
                if len(parts) < 2:
                    print("  usage: vel <note>")
                    continue
                note = parse_int(parts[1], -1)
                if note < 0:
                    print("  invalid note")
                    continue
                do_vel(port, note)

            else:
                print(f"  unknown command: {cmd!r}")


if __name__ == "__main__":
    main()
