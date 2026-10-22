#!/usr/bin/env python3
"""Bounded search for the set-opcode of a setting we can read but not write.

Method: send a candidate frame, query the state opcode, and accept it only if
the read-back actually changed. Restore immediately on success and stop trying
that setting. An ack proves nothing -- `BA 74 <anything>` acked for weeks while
changing nothing, which is how LDAC's setter ended up recorded as `0x74` instead
of `0x75`.

Candidates stay next to the known state opcode. No blind sweep: unknown opcodes
may include factory reset or OTA entry. Anything in `baseus.UNSAFE_OPS` is
refused before a single byte is sent -- today that is `0x37`, which acks, changes
nothing, and drops the RFCOMM connection. Note that `0x37` is exactly what the
`state + 1` rule gives for the unidentified report `0x36`, so a target for `0x36`
needs a different candidate list.

If a read-back is lost after a write, the run stops: that write may have landed,
and carrying on to the next candidate would hide it.

Each candidate writes a value that differs from the current one, because a write
that lands on the value already in place is indistinguishable from a rejected
frame.

**TARGETS is empty: every setting the Android app exposes has been solved, and
all eight setters turned out to be `state + 1`.** See PROTOCOL.md. Add an entry
here when a new state opcode turns up, in this shape:

    "wind": ("Wind noise reduction", 0x51, lambda p: 1 - (p[0] & 1), [
        (0x52, [V]),            # state + 1, the shape that always won
        (0x52, [V, 0x00]),      # ... some opcodes want a pad byte
        (0x50, [V]),
    ]),

The one target worth adding next is the gesture write (`AA 21`), and it is left
out on purpose: its setter is unmapped, `0x21` may double as its own setter, and
a wrong guess overwrites one of the earbuds' gesture slots. Decide that
deliberately rather than by running this script.

    python tools/find_setters.py           # every target
    python tools/find_setters.py spatial   # one target, by name
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from baseus import UNSAFE_OPS, Device, resolve_address  # noqa: E402

V = "V"  # placeholder in a payload template: replaced by the new value

# name -> (label, state_op, next_value(current) -> new, [(set_op, template), ...])
TARGETS: dict = {}


def render(template: list, value: int) -> list[int]:
    return [value if b == V else b for b in template]


def restore_payload(before: bytes, template_len: int) -> list[int]:
    """The original value, shaped to the width the working setter accepted.

    The state frame and the setter often differ in width -- state `AA 23 <v>` is
    one byte, setter `BA 24 <v> 00` is two -- and echoing the state payload back
    at the wrong width earns a 0x0B and leaves the setting changed.

    This assumes the setter's leading bytes mirror the state's and anything extra
    is trailing padding. That held for every setter found so far; a setter that
    puts a constant byte in the middle would be restored wrongly, so check the
    restore line in the output rather than trusting it.
    """
    payload = list(before[:template_len])
    return payload + [0x00] * (template_len - len(payload))


def search(dev: Device, label: str, state_op: int, next_value, candidates) -> tuple | None:
    before = dev.get(state_op)
    if before is None:
        print(f"{label}: no read-back on AA {state_op:02X}, skipping\n")
        return None
    value = next_value(before)
    print(f"{label}  (AA {state_op:02X} = {before.hex(' ').upper()}, trying value {value:#04x})")

    for set_op, template in candidates:
        payload = render(template, value)
        tx = "BA " + " ".join(f"{b:02X}" for b in [set_op, *payload])
        lost = None
        try:
            acks = [f.hex() for f in dev.query(set_op, *payload, wait=2.0)]
            after = dev.get(state_op)
        except OSError as exc:
            acks, after, lost = [], None, exc
        if after is None:
            # Not knowing is not the same as "unchanged". Stop rather than move on
            # to the next candidate with a setting possibly left changed.
            raise SystemExit(
                f"\n   Lost the read-back after {tx}"
                + (f" ({lost})" if lost else "") + ".\n"
                f"   That write may have landed. AA {state_op:02X} was "
                f"{before.hex(' ').upper()} before it -- run `baseus.py status` and\n"
                f"   put it back by hand if it changed. Stopping here."
            )
        shown = after.hex(" ").upper()
        print(f"   {tx:<20} ack={acks}  ->  AA {state_op:02X} {shown}")

        if after is not None and after != before:
            print(f"   >>> FOUND: {tx}")
            restore = restore_payload(before, len(payload))
            dev.query(set_op, *restore, wait=2.0)
            back = dev.get(state_op)
            ok = back == before
            print(f"   restored to {back.hex(' ').upper() if back else '?'}"
                  f"{'' if ok else '   <-- RESTORE FAILED'}")
            return (set_op, payload)
        time.sleep(0.3)

    print("   >>> not found in the candidate set")
    return None


def main() -> None:
    if not TARGETS:
        raise SystemExit(
            "No targets. Every setter the Android app exposes is already confirmed\n"
            "(see PROTOCOL.md); add an entry to TARGETS when a new state opcode\n"
            "appears. Nothing was sent to the earbuds."
        )

    unknown = [a for a in sys.argv[1:] if a not in TARGETS]
    if unknown:
        # Falling back to "every target" on a typo would write to the earbuds.
        raise SystemExit(f"Unknown target(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(TARGETS)}.")
    wanted = sys.argv[1:] or list(TARGETS)

    unsafe = sorted({op for name in wanted for op, _ in TARGETS[name][3] if op in UNSAFE_OPS})
    if unsafe:
        raise SystemExit("Refusing to run: candidate opcode(s) "
                         + ", ".join(f"0x{op:02X}" for op in unsafe)
                         + " are in UNSAFE_OPS. Nothing was sent.")

    addr = resolve_address(None)
    print()
    results = []

    with Device(addr) as dev:
        for name in wanted:
            label, state_op, next_value, candidates = TARGETS[name]
            results.append((label, search(dev, label, state_op, next_value, candidates)))
            print()

    print("--- summary ---")
    for label, found in results:
        if found:
            op, payload = found
            print(f"  {label}: BA {op:02X} <value>  (payload width {len(payload)})")
        else:
            print(f"  {label}: still unknown")


if __name__ == "__main__":
    main()
