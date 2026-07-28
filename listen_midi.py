#!/usr/bin/env python3
"""
Listen for MIDI note_on messages from the Synthstrom Deluge.
Tap each kit row's audition pad to see its note number.
"""

import sys
import threading
import time

try:
    import mido
except ImportError:
    print("mido is not installed. Run: pip install mido python-rtmidi")
    raise SystemExit(1)


def listen_port(name: str, note_only: bool) -> None:
    try:
        with mido.open_input(name) as port:
            for msg in port:
                if note_only:
                    if msg.type == "note_on" and msg.velocity > 0:
                        print(f"Note: {msg.note}  (vel {msg.velocity}, ch {msg.channel}, port: {name!r})")
                else:
                    print(f"[{name}] {msg}")
    except Exception as e:
        print(f"[{name}] error: {e}")


def main() -> None:
    all_names = mido.get_input_names()
    if not all_names:
        print("No MIDI input ports found at all. Check USB connection.")
        raise SystemExit(1)

    # --all flag: dump every raw message from every port (for debugging)
    raw_mode = "--all" in sys.argv

    deluge_ports = [n for n in all_names if "Deluge" in n]

    if not deluge_ports:
        print("No port with 'Deluge' in the name. Available ports:")
        for n in all_names:
            print(f"  {n!r}")
        raise SystemExit(1)

    if raw_mode:
        print(f"RAW MODE — printing every message from all {len(deluge_ports)} Deluge port(s). Tap anything. Ctrl+C to quit.\n")
    else:
        print(f"Listening on {len(deluge_ports)} Deluge port(s). Tap each kit row's audition pad. Ctrl+C to quit.")
        print("(Run with --all to see every raw MIDI message instead.)\n")

    threads = [
        threading.Thread(target=listen_port, args=(name, not raw_mode), daemon=True)
        for name in deluge_ports
    ]
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
