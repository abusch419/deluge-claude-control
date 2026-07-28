#!/usr/bin/env python3
"""
Send test MIDI notes to the Deluge one at a time.
Watch which pad lights up on the Deluge to map note numbers to kit rows.
"""

import sys
import time

try:
    import mido
except ImportError:
    print("mido is not installed. Run: pip install mido python-rtmidi")
    raise SystemExit(1)


def find_deluge_output() -> list[str]:
    return [n for n in mido.get_output_names() if "Deluge" in n]


def send_note(port, note: int, velocity: int, channel: int = 0) -> None:
    port.send(mido.Message("note_on", note=note, velocity=velocity, channel=channel))


def main() -> None:
    ports = find_deluge_output()
    if not ports:
        print("No Deluge output port found. Available ports:")
        for n in mido.get_output_names():
            print(f"  {n!r}")
        raise SystemExit(1)

    # Default: Port 1, but allow override: python3 probe_midi.py 2
    port_index = int(sys.argv[1]) - 1 if len(sys.argv) > 1 else 0
    port_name = ports[min(port_index, len(ports) - 1)]

    # Default channel 0 (= MIDI channel 1), override: python3 probe_midi.py 1 9 (port 1, channel 9)
    channel = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    print(f"Sending to {port_name!r} on MIDI channel {channel + 1}")
    print("Watch the Deluge — the active pad will light up.")
    print("Press Enter to advance to the next note, Ctrl+C to quit.\n")

    start_note = 36  # start low and sweep up
    end_note = 80

    import threading

    stop_event = threading.Event()

    def retrigger(port, note, channel):
        """Keep re-firing the note every 300ms so one-shot samples keep flashing."""
        while not stop_event.is_set():
            send_note(port, note, 100, channel)
            time.sleep(0.3)

    with mido.open_output(port_name) as port:
        note = start_note
        while note <= end_note:
            stop_event.clear()
            t = threading.Thread(target=retrigger, args=(port, note, channel), daemon=True)
            t.start()

            try:
                input(f"  Note {note:3d}  (retriggering every 300ms) → Enter for next, Ctrl+C to stop")
            except KeyboardInterrupt:
                print("\nDone.")
                stop_event.set()
                send_note(port, note, 0, channel)
                return

            stop_event.set()
            t.join(timeout=0.5)
            send_note(port, note, 0, channel)
            time.sleep(0.05)
            note += 1

    print("Done.")


if __name__ == "__main__":
    main()
