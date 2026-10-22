#!/usr/bin/env python3
"""Snapshot the full device state and diff it against the previous snapshot.

For when the PC cannot hold the control channel at the same time as the phone
app (`WinError 10064`), so a live capture is impossible. Instead of watching the
frame go by, take a snapshot, change exactly one setting on the phone, take
another, and let the diff name the opcode:

    python tools/diff_state.py before-playasworn
    ... open the app, toggle Play as worn, close the app ...
    python tools/diff_state.py after-playasworn     # prints the diff

This identifies the *state* opcode, not the setter. That is the expensive half:
once the state opcode and payload width are known, `find_setters.py` can verify
a candidate setter by write -> read-back -> restore.

Snapshots land in `snapshots/` as plain text, newest wins.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from baseus import Device, OP_SYNC, resolve_address  # noqa: E402

SNAP_DIR = Path(__file__).resolve().parent.parent / "snapshots"
CONNECT_ATTEMPTS = 4

# These change for reasons other than the setting you just touched: the battery
# drains, 0x19's first byte flips unexplained, and 0x21 echoes whichever setting
# moved last without saying which one it was. None of them answers the question.
NOISY = {0x02, 0x19, 0x21}


def take_snapshot(addr: str) -> dict[int, str]:
    """Full state dump as {opcode: payload hex}."""
    last_error: OSError | None = None
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            with Device(addr) as dev:
                state: dict[int, str] = {}
                for f in dev.query(OP_SYNC, wait=3.0):
                    state[f.op] = " ".join(f"{b:02X}" for b in f.payload)
                if state:
                    return state
                print("  connected but the device said nothing; retrying...", flush=True)
        except OSError as exc:
            last_error = exc
            print(f"  connect attempt {attempt + 1} failed ({exc}); retrying...", flush=True)
        if attempt < CONNECT_ATTEMPTS - 1:
            time.sleep(2.5)
    raise SystemExit(
        "Could not read the device. The phone's Baseus app holds the control "
        f"channel while it is open, close it and try again.\n  last error: {last_error}"
    )


def load(path: Path) -> dict[int, str]:
    state: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("AA "):
            continue
        parts = line.split()
        state[int(parts[1], 16)] = " ".join(parts[2:])
    return state


def save(path: Path, state: dict[int, str]) -> None:
    path.write_text(
        "".join(f"AA {op:02X} {state[op]}\n".rstrip() + "\n" for op in sorted(state)),
        encoding="utf-8",
    )


def diff(old: dict[int, str], new: dict[int, str]) -> list[str]:
    lines = []
    for op in sorted(set(old) | set(new)):
        a, b = old.get(op), new.get(op)
        if a == b:
            continue
        note = "   (not the answer -- see PROTOCOL.md)" if op in NOISY else ""
        if a is None:
            lines.append(f"  AA {op:02X}   APPEARED   {b}{note}")
        elif b is None:
            lines.append(f"  AA {op:02X}   GONE       (was {a}){note}")
        else:
            lines.append(f"  AA {op:02X}   CHANGED    {a}  ->  {b}{note}")
    return lines


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "snap"
    SNAP_DIR.mkdir(exist_ok=True)
    previous = sorted(SNAP_DIR.glob("*.txt"))

    addr = resolve_address(None)
    print(f"\nSnapshot '{label}'...", flush=True)
    state = take_snapshot(addr)

    path = SNAP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.txt"
    save(path, state)
    for op in sorted(state):
        print(f"  AA {op:02X} {state[op]}")
    print(f"\nsaved to {path.relative_to(SNAP_DIR.parent)}")

    if not previous:
        print("\nNo earlier snapshot to compare against, this one is the baseline.")
        return

    base = previous[-1]
    lines = diff(load(base), state)
    print(f"\n--- diff against {base.name} ---")
    if lines:
        print("\n".join(lines))
    else:
        print("  (identical, the setting you changed is not exposed over this protocol,\n"
              "   or the change did not take effect before the snapshot)")


if __name__ == "__main__":
    main()
