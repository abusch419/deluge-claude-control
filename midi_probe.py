#!/usr/bin/env python3
"""
MIDI probe: open every available MIDI input port at once and print
every incoming message, prefixed by the port it came from.

Use this to confirm whether a device (e.g. a Synthstrom Deluge) is
sending any MIDI at all over USB.
"""

import sys
import threading
import time

try:
    import mido
    import rtmidi  # noqa: F401  (ensures the python-rtmidi backend is available)
except ImportError:
    print("Missing dependencies. Install both with:")
    print("    pip install mido python-rtmidi")
    raise SystemExit(1)


def format_message(port_name: str, msg) -> str:
    parts = [msg.type]
    for attr in ("note", "velocity", "channel", "control", "value", "program", "pitch"):
        if hasattr(msg, attr):
            parts.append(f"{attr}={getattr(msg, attr)}")
    return f"[{port_name}] " + " ".join(parts)


def listen(port_name: str, stop_event: threading.Event) -> None:
    try:
        with mido.open_input(port_name) as port:
            while not stop_event.is_set():
                for msg in port.iter_pending():
                    print(format_message(port_name, msg), flush=True)
                time.sleep(0.001)
    except Exception as e:
        print(f"[{port_name}] could not open/read: {e}", flush=True)


def main() -> None:
    names = mido.get_input_names()

    print("Available MIDI input ports:")
    if not names:
        print("  (none found — check USB connection)")
        raise SystemExit(1)
    for i, name in enumerate(names):
        print(f"  [{i}] {name}")
    print()
    print(f"Opening all {len(names)} port(s). Tap a pad / turn a knob. Ctrl+C to quit.\n")

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=listen, args=(name, stop_event), daemon=True)
        for name in names
    ]
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        stop_event.set()
        print("\nDone.")


if __name__ == "__main__":
    main()
