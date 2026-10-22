#!/usr/bin/env python3
"""Baseus Bass BS1 NC control over Classic Bluetooth RFCOMM (Windows, stdlib only).

Wire protocol:

    app -> device   BA <opcode> [payload...]
    device -> app   AA <opcode> [payload...]

No length field, no CRC; one frame per RFCOMM packet, channel 1.
Convention found on this device:
    BA <report_op>       = query  (safe, read-only)
    BA <report_op + 1>   = set    (changes state)
    BA 01                = sync   (device dumps every state frame it has)

An ack payload of 0x0B means the frame was malformed -- almost always the wrong
payload width. `BA 24 01` is rejected; `BA 24 01 00` works. Payload width is per
opcode and has to be found by trial.

Status of each setting on the BS1 NC -- see PROTOCOL.md for the full map.
Requires the earbuds to be paired and connected in Windows.

Usage:
    python baseus.py                             show everything (default)
    python baseus.py status
    python baseus.py monitor                     live event stream, Ctrl+C to stop
    python baseus.py devices                     list paired Baseus devices

    python baseus.py anc on|off                  noise cancelling
    python baseus.py spatial normal|music|cinema
    python baseus.py eq classic|powerbass|hifi|jazz|pop|treble|acoustic|custom
    python baseus.py bass on|off --level 1-5
    python baseus.py wind on|off                 wind noise reduction
    python baseus.py playasworn on|off
    python baseus.py lowlatency on|off
    python baseus.py ldac on|off                 (single-device audio)

    Every setting command with its argument omitted reads the current value.

    python baseus.py raw 34 01 68                send a raw frame
    python baseus.py -v status                   show raw hex traffic
    python baseus.py --addr XX:XX:XX:XX:XX:XX status
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

RFCOMM_CHANNEL = 1
DEFAULT_TIMEOUT = 6.0
TX_MAGIC = 0xBA
RX_MAGIC = 0xAA
ACK_REJECTED = 0x0B

# --- opcodes --------------------------------------------------------------
# Confirmed by write -> read-back -> restore against a physical BS1 NC, or by
# correlating a query response with the setting shown in the Baseus Android app.

OP_SYNC = 0x01          # write-only: triggers a full state dump
OP_BATTERY = 0x02       # AA 02 <L%> 00 <R%> 01
OP_FW = 0x19            # AA 19 <n> <L hi lo> <R hi lo> -- per bud; "firmware" is a guess
OP_ECHO = 0x21          # AA 21 <3 bytes> -- echoes the last change, encoding unclear
OP_LOW_LATENCY = 0x23   # AA 23 <0|1>   "Low Latency Mode"
OP_LOW_LATENCY_SET = 0x24  # BA 24 <0|1> 00 -- needs a 2-byte payload   CONFIRMED
OP_IN_EAR = 0x25        # AA 25 <0|1>   "Play as worn"
OP_IN_EAR_SET = 0x26    # BA 26 <0|1>                 CONFIRMED
OP_EQ = 0x30            # AA 30 <preset> "EQ mode"
OP_EQ_SET = 0x31        # BA 31 <preset>              CONFIRMED
OP_ANC = 0x33           # AA 33 <mode> <level>        "Ambient sound" in the app
OP_ANC_SET = 0x34       # BA 34 <mode> <level>        CONFIRMED
OP_UNK_36 = 0x36        # AA 36 <0|1>                 -- unidentified, always reads 01
OP_SPATIAL = 0x42       # AA 42 <0|1|2> "Spatial Audio"
OP_SPATIAL_SET = 0x43   # BA 43 <0|1|2>               CONFIRMED
OP_WIND = 0x51          # AA 51 <0|1>   "Wind noise reduction"
OP_WIND_SET = 0x52      # BA 52 <0|1>                 CONFIRMED
OP_BASS = 0x53          # AA 53 <0|1> <level>  "Bass Boost"
OP_BASS_SET = 0x54      # BA 54 <0|1> <level>  -- the 1-byte form clobbers the level
OP_LDAC = 0x74          # AA 74 <0|1>   "LDAC"
OP_LDAC_SET = 0x75      # BA 75 <0|1>                 CONFIRMED

# Confirmed setters: their AA echo is an ack, so a 0x0B payload there means
# "malformed frame". Only setters proven by write -> read-back belong here -- a
# guessed one would mislabel a report whose value happens to be 0x0B. Every one
# of them is state + 1.
SET_OPS = frozenset({0x24, 0x26, 0x31, 0x34, 0x43, 0x52, 0x54, 0x75})

# 0x37 acks, re-dumps state, changes nothing, and then kills the RFCOMM link.
UNSAFE_OPS = frozenset({0x37})

# Payload length (bytes after the opcode), used only to split back-to-back
# frames inside one RFCOMM read. An opcode missing here, or one whose width does
# not land on the next frame, is cut at the next 0xAA instead -- see _split.
# Every width here has been seen from this model.
FRAME_LEN = {
    0x02: 4, 0x12: 13, 0x19: 5, 0x21: 3, 0x23: 1, 0x24: 1, 0x25: 1,
    0x26: 1, 0x30: 1, 0x31: 1, 0x33: 2, 0x34: 1,
    0x36: 1, 0x37: 1, 0x42: 1, 0x43: 1, 0x51: 1, 0x52: 1, 0x53: 2,
    0x54: 1, 0x74: 1, 0x75: 1,
}

# Only two modes. The earbuds have no transparency state to report -- see PROTOCOL.md.
ANC_MODES = {"off": 0x00, "on": 0x01}
ANC_MODE_NAMES = {0x00: "off", 0x01: "on"}
ANC_DEFAULT_LEVEL = {0x00: 0xFF, 0x01: 0x68}

SPATIAL_MODES = {"normal": 0, "music": 1, "cinema": 2}
SPATIAL_NAMES = {0: "Normal", 1: "Music", 2: "Cinema"}

# Reading order of the app's EQ grid. Turning Spatial Audio on resets this to 0.
EQ_PRESETS = {
    "classic": 0, "powerbass": 1, "hifi": 2, "jazz": 3,
    "pop": 4, "treble": 5, "acoustic": 6, "custom": 7,
}
EQ_NAMES = {
    0: "Baseus Classic", 1: "Powerful bass", 2: "Hi-Fi Live", 3: "Jazz rock",
    4: "Pop", 5: "Clear treble", 6: "Acoustic", 7: "Customizable",
}

BASS_LEVELS = range(1, 6)

# The order `status` prints settings in, chosen to read like the phone app.
# Every decodable opcode is in this list or in STATUS_HIDDEN today; the fallback
# in cmd_status only matters when one is added to LABELS and forgotten here, and
# then it is printed last instead of being silently dropped.
STATUS_ORDER = (
    OP_BATTERY, OP_ANC, OP_WIND, OP_SPATIAL, OP_EQ,
    OP_BASS, OP_IN_EAR, OP_LOW_LATENCY, OP_LDAC,
)
# 0x21 is undecoded, and 0x19 is only guessed to be a firmware version -- see
# PROTOCOL.md. `status` never shows them; `raw 21` and `raw 19` decode them, and
# -v shows their raw frames.
STATUS_HIDDEN = frozenset({OP_ECHO, OP_FW})
# Everything `status` reads from a dump: its rows, and 0x19 for "in case".
STATUS_NEEDS = frozenset(STATUS_ORDER) | {OP_FW}


def _fw(p: bytes) -> str:
    # Left bud, then right; a bud sitting in the case reads 00 00.
    def one(hi: int, lo: int) -> str:
        return "--" if hi == lo == 0 else f"{hi}.{lo}"
    return f"L {one(p[1], p[2])}   R {one(p[3], p[4])}"


def battery_text(battery: bytes, per_bud: bytes | None) -> str:
    """Both buds' battery, saying "in case" for one that is sitting in it.

    AA 02 reports a seated bud as 0 %, which reads like a flat battery. AA 19
    zeroes that bud's half as well, and the two have only ever been seen to
    agree, so a bud counts as seated only when both say so. If one lags the
    other after the bud goes in, this shows the plain percentage for that
    moment rather than guess.
    """
    parts = []
    for side, pct, at in (("left", battery[0], 1), ("right", battery[2], 3)):
        seated = (pct == 0 and per_bud is not None and len(per_bud) >= at + 2
                  and per_bud[at] == per_bud[at + 1] == 0)
        parts.append(f"{side} in case" if seated else f"{side} {pct}%")
    return "   ".join(parts)


def _bass(p: bytes) -> str:
    # The level survives being switched off, but showing it then is just noise.
    return f"on  (level {p[1]})" if p[0] else "off"


# Label and decoder for the `status` table.
LABELS = {
    OP_BATTERY: ("Battery", lambda p: f"left {p[0]}%   right {p[2]}%"),
    OP_ANC: ("ANC", lambda p: ANC_MODE_NAMES.get(p[0], p[0])),
    OP_SPATIAL: ("Spatial Audio", lambda p: SPATIAL_NAMES.get(p[0], p[0])),
    OP_EQ: ("EQ mode", lambda p: EQ_NAMES.get(p[0], f"preset {p[0]}")),
    OP_WIND: ("Wind noise reduction", lambda p: "on" if p[0] else "off"),
    OP_LOW_LATENCY: ("Low Latency Mode", lambda p: "on" if p[0] else "off"),
    OP_IN_EAR: ("Play as worn", lambda p: "on" if p[0] else "off"),
    OP_LDAC: ("LDAC", lambda p: "on" if p[0] else "off"),
    OP_BASS: ("Bass Boost", _bass),
    OP_ECHO: ("Last change echo", lambda p: p.hex(" ").upper()),
    OP_FW: ("Firmware?", _fw),
}


class Frame:
    __slots__ = ("op", "payload")

    def __init__(self, op: int, payload: bytes) -> None:
        self.op = op
        self.payload = payload

    def __eq__(self, other) -> bool:
        return isinstance(other, Frame) and (self.op, self.payload) == (other.op, other.payload)

    def hex(self) -> str:
        return f"AA {self.op:02X} " + " ".join(f"{b:02X}" for b in self.payload)

    def fields(self) -> tuple[str, str] | None:
        """(label, value), or None when the frame decodes to nothing to show."""
        entry = LABELS.get(self.op)
        if not entry or not self.payload:
            return None
        label, fn = entry
        try:
            return label, str(fn(self.payload))
        except (IndexError, KeyError):
            return None

    def describe(self) -> str:
        p = self.payload
        if not p:
            return ""
        # 0x0B is the device's complaint about a malformed frame -- in practice
        # the wrong payload width, not a missing feature. Only acks (the echo of
        # a set opcode) carry it; a state frame with a 0x0B payload is a value.
        if p[0] == ACK_REJECTED and self.op in SET_OPS:
            return "REJECTED (malformed frame -- try another payload width)"
        parts = self.fields()
        return f"{parts[0]}: {parts[1]}" if parts else ""


def _split(buf: bytes) -> tuple[list[Frame], bytes]:
    """Frames in `buf`, plus trailing bytes that may be the start of the next one."""
    frames: list[Frame] = []
    i = 0
    while i < len(buf) - 1:
        if buf[i] != RX_MAGIC:
            i += 1
            continue
        op = buf[i + 1]
        n = FRAME_LEN.get(op)
        if n is not None:
            end = i + 2 + n
            if end > len(buf):
                if RX_MAGIC not in buf[i + 2:]:
                    # A known frame cut off by the end of this read: keep it for
                    # the next read rather than emit a payload that cannot decode.
                    return frames, buf[i:]
                # Another frame starts inside the claimed span, so the table's
                # width is wrong for this device -- fall through.
            elif end == len(buf) or buf[end] == RX_MAGIC:
                frames.append(Frame(op, buf[i + 2:end]))
                i = end
                continue
            # The width does not land on a frame boundary. Trusting it would
            # swallow the frames behind this one, so treat it as unknown.
        # Unknown width: cut at the next plausible frame start. An 0xAA inside a
        # payload splits one frame in two, which is recoverable; swallowing the
        # rest of the buffer is not.
        nxt = buf.find(RX_MAGIC, i + 2)
        end = len(buf) if nxt == -1 else nxt
        frames.append(Frame(op, buf[i + 2:end]))
        i = end
    rest = buf[i:] if i < len(buf) and buf[i] == RX_MAGIC else b""
    return frames, rest


def split_frames(buf: bytes) -> list[Frame]:
    return _split(buf)[0]


# --- transport ------------------------------------------------------------

class Device:
    def __init__(self, addr: str, channel: int = RFCOMM_CHANNEL, verbose: bool = False):
        self.addr = addr
        self.channel = channel
        self.verbose = verbose
        self.sock: socket.socket | None = None
        self._pending = b""     # a frame split across two reads

    def __enter__(self) -> "Device":
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            s.settimeout(DEFAULT_TIMEOUT)
            s.connect((self.addr, self.channel))
        except BaseException:
            # __exit__ never runs when __enter__ raises, so close it here.
            s.close()
            raise
        self.sock = s
        return self

    def __exit__(self, *exc) -> None:
        if self.sock:
            self.sock.close()

    def send(self, op: int, *payload: int) -> None:
        frame = bytes([TX_MAGIC, op, *payload])
        if self.verbose:
            print(f"  TX {frame.hex(' ').upper()}", file=sys.stderr)
        self.sock.sendall(frame)

    def read(self, seconds: float, until=None, idle: float | None = None) -> list[Frame]:
        """Frames that arrive within `seconds`.

        Returns early once `until(frames so far)` is true, or once `idle` seconds
        pass with nothing new after the first frame. Either way `seconds` stays
        the upper bound, so a reply that never comes costs what it always did.

        Raises ConnectionError when the earbuds hang up. Returning early and empty
        instead made a dead link look like a quiet one: `monitor` spun at full CPU
        printing nothing, and every `wait=` stopped being a wait.
        """
        out: list[Frame] = []
        end = time.monotonic() + seconds
        last_rx = None
        while True:
            now = time.monotonic()
            remaining = end - now
            if idle is not None and last_rx is not None:
                remaining = min(remaining, last_rx + idle - now)
            if remaining <= 0:
                break
            self.sock.settimeout(max(0.1, remaining))
            try:
                data = self.sock.recv(1024)
            except (TimeoutError, socket.timeout):
                # socket.timeout only became an alias of TimeoutError in 3.10;
                # on 3.9 it is a separate OSError subclass and has to be named.
                continue
            if not data:
                raise ConnectionError("the earbuds closed the connection")
            if self.verbose:
                print(f"  RX {data.hex(' ').upper()}", file=sys.stderr)
            frames, self._pending = _split(self._pending + data)
            out.extend(frames)
            if frames:
                last_rx = time.monotonic()
            if until is not None and until(out):
                break
        return out

    def query(self, op: int, *payload: int, wait: float = 2.0,
              until=None, idle: float | None = None) -> list[Frame]:
        self.send(op, *payload)
        return self.read(wait, until=until, idle=idle)

    def get(self, op: int, wait: float = 2.0) -> bytes | None:
        """Query one report opcode and return its payload.

        Returns as soon as the report arrives (measured at 45-125 ms) instead of
        sitting out `wait`. A report pushed after a write is the new state, so if
        it gets here first it is still the right answer. If several arrive in one
        read, the last one is the current one.
        """
        payload = None
        for f in self.query(op, wait=wait, until=lambda fs: any(f.op == op for f in fs)):
            if f.op == op:
                payload = f.payload
        return payload


# --- device discovery -----------------------------------------------------

def find_devices() -> list[tuple[str, str]]:
    # PowerShell writes the OEM code page (cp857 on Turkish Windows) and Python
    # reads the ANSI one (cp1254), so a paired device whose name has a dotless i
    # or a u-umlaut made the decode fail, and every command without --addr
    # crashed with it. Pin both ends to UTF-8.
    ps = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Get-PnpDevice -Class Bluetooth -PresentOnly | "
        "Where-Object { $_.InstanceId -like 'BTHENUM\\DEV_*' } | "
        "ForEach-Object { $_.FriendlyName + '|' + $_.InstanceId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return []

    # Windows lists one PnP entry per Bluetooth service, so the same pair of
    # earbuds shows up several times -- keep the first entry per address.
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, instance = line.split("|", 1)
        if "baseus" not in name.lower():
            continue
        # -like above ignores case; split() does not.
        instance = instance.upper()
        if "DEV_" not in instance:
            continue
        mac = instance.split("DEV_", 1)[1][:12]
        if len(mac) != 12:
            continue
        addr = ":".join(mac[i:i + 2] for i in range(0, 12, 2)).upper()
        if addr not in seen:
            seen.add(addr)
            found.append((name.strip(), addr))
    return found


# find_devices starts PowerShell, which costs 1-2 s -- more than a whole command
# once replies return early. The first address found is kept here, outside the
# project folder, and dropped when a connection to it fails (see main).
CACHE_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "baseus-cli" / "device.txt"
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def load_cached_device() -> tuple[str, str] | None:
    try:
        addr, name = CACHE_FILE.read_text(encoding="utf-8").strip().split("|", 1)
    except (OSError, ValueError):
        return None
    return (name, addr) if MAC_RE.match(addr) else None


def save_cached_device(device: tuple[str, str] | None) -> None:
    """Remember `device` as (name, addr), or forget the saved one with None."""
    try:
        if device is None:
            CACHE_FILE.unlink()
        else:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(f"{device[1]}|{device[0]}\n", encoding="utf-8")
    except OSError:
        pass    # a cache that cannot be written only costs the next lookup


def resolve_address(explicit: str | None, quiet: bool = False) -> str:
    if explicit:
        return explicit
    device = load_cached_device()
    if device is None:
        devices = find_devices()
        if not devices:
            sys.exit(
                "No paired Baseus device found. Pair and connect the earbuds first, "
                "or pass --addr XX:XX:XX:XX:XX:XX"
            )
        device = devices[0]
        save_cached_device(device)
    name, addr = device
    if not quiet:
        # stderr, so `status` output stays parseable when piped.
        print(f"{name}  ({addr})", file=sys.stderr)
    return addr


# --- commands -------------------------------------------------------------

def show(op: int, payload: bytes | None) -> None:
    """Print one setting the way `status` would, from its report payload."""
    label, fn = LABELS[op]
    if payload is None:
        print(f"  {label}: no response")
        return
    try:
        print(f"  {label}: {fn(payload)}")
    except (IndexError, KeyError):
        print(f"  {label}: {payload.hex(' ').upper()}")


def write_checked(dev: Device, report_op: int, set_op: int,
                  payload: list[int], expect: list[int]) -> None:
    """Write `payload`, print the read-back, and fail unless it starts with `expect`.

    The ack is checked too: a 0x0B there means the frame was malformed, and
    without saying so a rejected write looks exactly like a refused one.
    """
    acks = dev.query(set_op, *payload, until=lambda fs: any(f.op == set_op for f in fs))
    rejected = any(f.op == set_op and f.payload[:1] == bytes([ACK_REJECTED]) for f in acks)
    # The earbuds usually push the new state right after the ack. Re-query to be
    # sure, but keep the pushed frame in case that answer goes missing.
    pushed = [f.payload for f in acks if f.op == report_op]
    state = dev.get(report_op)
    if state is None and pushed:
        state = pushed[-1]
    show(report_op, state)
    if rejected:
        sys.exit("  Rejected by the earbuds (0x0B: malformed frame) -- nothing changed.")
    if state is None or list(state[:len(expect)]) != list(expect):
        sys.exit("  The earbuds did not take the change.")


def get_or_set(args, report_op: int, set_op: int, payload: list[int] | None) -> None:
    """Read `report_op`, or write `payload` and check the read-back."""
    with Device(resolve_address(args.addr), verbose=args.verbose) as dev:
        if payload is None:
            show(report_op, dev.get(report_op))
        else:
            # The first byte is the value for every setter routed through here;
            # what follows is a level or a pad the earbuds may report differently.
            write_checked(dev, report_op, set_op, payload, payload[:1])


def cmd_devices(_args) -> None:
    devices = find_devices()
    # A fresh lookup anyway, so refresh the saved address with it.
    save_cached_device(devices[0] if devices else None)
    if not devices:
        sys.exit("No paired Baseus devices found.")
    for name, addr in devices:
        print(f"{addr}  {name}")


def cmd_status(args) -> None:
    with Device(resolve_address(args.addr), verbose=args.verbose) as dev:
        # The dump has no end marker. It takes ~550 ms with gaps of up to ~115 ms,
        # so stop once every frame `status` uses is in, and fall back on a quiet
        # gap for a bud in the case or a frame that never comes.
        frames = dev.query(OP_SYNC, wait=3.0, idle=0.8,
                           until=lambda fs: STATUS_NEEDS <= {f.op for f in fs})
        if not frames:
            sys.exit("No response. Are the earbuds out of the case and connected?")
        latest = {f.op: f for f in frames}
        extra = [op for op in sorted(latest)
                 if op not in STATUS_ORDER and op not in STATUS_HIDDEN]

        rows = []
        for op in STATUS_ORDER + tuple(extra):
            frame = latest.get(op)
            parts = frame.fields() if frame else None
            if parts and op == OP_BATTERY and len(frame.payload) >= 3:
                per_bud = latest[OP_FW].payload if OP_FW in latest else None
                parts = (parts[0], battery_text(frame.payload, per_bud))
            if parts:
                rows.append(parts)
        if not rows:
            print("Connected, but nothing decodable came back.")
            return

        width = max(len(label) for label, _ in rows)
        print()
        for label, value in rows:
            print(f"  {label:<{width}}   {value}")
        print()


def cmd_monitor(args) -> None:
    with Device(resolve_address(args.addr), verbose=args.verbose) as dev:
        print("Monitoring. Change a setting on the phone app or the earbuds; Ctrl+C to stop.\n")
        state: dict[int, Frame] = {}
        try:
            for f in dev.query(OP_SYNC, wait=3.0):
                state[f.op] = f
            while True:
                for f in dev.read(1.0):
                    ts = time.strftime("%H:%M:%S")
                    changed = state.get(f.op) is not None and state[f.op] != f
                    mark = "CHANGED" if changed else ""
                    state[f.op] = f
                    print(f"[{ts}] {f.hex():<24}  {f.describe():<40} {mark}")
        except KeyboardInterrupt:
            print("\nstopped")
        except OSError as exc:
            # read() raises once the earbuds hang up: back in the case, out of
            # range, or taken over by the phone app.
            sys.exit(f"\nconnection lost: {exc}")


def cmd_anc(args) -> None:
    if args.mode is None and args.level is not None:
        sys.exit("anc: --level goes with a mode, e.g. anc on --level 0x68")
    payload = None
    if args.mode is not None:
        mode = ANC_MODES[args.mode]
        level = args.level if args.level is not None else ANC_DEFAULT_LEVEL[mode]
        payload = [mode, level]
    get_or_set(args, OP_ANC, OP_ANC_SET, payload)


def cmd_spatial(args) -> None:
    if args.mode is not None and SPATIAL_MODES[args.mode] != 0:
        print("Note: the earbuds turn EQ mode back to Baseus Classic when "
              "Spatial Audio is enabled -- the two are mutually exclusive.")
    payload = None if args.mode is None else [SPATIAL_MODES[args.mode]]
    get_or_set(args, OP_SPATIAL, OP_SPATIAL_SET, payload)


def cmd_eq(args) -> None:
    payload = None if args.preset is None else [EQ_PRESETS[args.preset]]
    get_or_set(args, OP_EQ, OP_EQ_SET, payload)


def cmd_wind(args) -> None:
    payload = None if args.state is None else [1 if args.state == "on" else 0]
    get_or_set(args, OP_WIND, OP_WIND_SET, payload)


def cmd_playasworn(args) -> None:
    payload = None if args.state is None else [1 if args.state == "on" else 0]
    get_or_set(args, OP_IN_EAR, OP_IN_EAR_SET, payload)


def cmd_bass(args) -> None:
    if args.state is None and args.level is None:
        get_or_set(args, OP_BASS, OP_BASS_SET, None)
        return
    # The setter needs both bytes: `BA 54 <on>` alone lands, but resets the
    # level to whatever it likes. Read the current pair and edit one half.
    with Device(resolve_address(args.addr), verbose=args.verbose) as dev:
        current = dev.get(OP_BASS)
        if current is None or len(current) < 2:
            sys.exit("  Bass Boost: no usable response")
        on = current[0] if args.state is None else (1 if args.state == "on" else 0)
        level = args.level if args.level is not None else current[1]
        write_checked(dev, OP_BASS, OP_BASS_SET, [on, level], [on, level])
    if args.level is not None and not on:
        # `status` hides the level while Bass Boost is off, but here it is the
        # thing that was just changed, so confirm it.
        print(f"  level {level} stored -- it applies when Bass Boost is on")


def cmd_lowlatency(args) -> None:
    # The payload must be two bytes -- a one-byte payload is rejected with 0x0B.
    payload = None if args.state is None else [1 if args.state == "on" else 0, 0x00]
    get_or_set(args, OP_LOW_LATENCY, OP_LOW_LATENCY_SET, payload)


def read_after_reconnect(addr: str, op: int, verbose: bool = False,
                         attempts: int = 4, delay: float = 2.5) -> bytes | None:
    """Read `op` on a fresh connection, for writes that tear the current one down."""
    for _ in range(attempts):
        time.sleep(delay)
        try:
            with Device(addr, verbose=verbose) as dev:
                return dev.get(op)
        except OSError:
            continue
    return None


def cmd_ldac(args) -> None:
    addr = resolve_address(args.addr)
    if args.state is None:
        with Device(addr, verbose=args.verbose) as dev:
            show(OP_LDAC, dev.get(OP_LDAC))
        return

    want = 1 if args.state == "on" else 0
    if want:
        print("Note: LDAC and dual-device connection are mutually exclusive; "
              "turning LDAC on drops the earbuds to a single device.")
    with Device(addr, verbose=args.verbose) as dev:
        dev.send(OP_LDAC_SET, want)
        try:
            dev.read(0.5)       # the ack
        except OSError:
            pass
    # Switching codec renegotiates the audio link and takes this connection down
    # shortly after the ack, so read the result on a new one once it settles.
    print("  waiting for the audio link to settle...")
    p = read_after_reconnect(addr, OP_LDAC, verbose=args.verbose)
    if p is None:
        sys.exit("  Could not reconnect to read the result. Run 'ldac' again in a few seconds.")
    show(OP_LDAC, p)
    if p[0] != want:
        sys.exit("  The earbuds did not take the change.")


def cmd_raw(args) -> None:
    try:
        data = [int(b, 16) for b in args.bytes]
    except ValueError as exc:
        sys.exit(f"raw: {exc}")
    if data and data[0] == TX_MAGIC:
        data = data[1:]
    if not data:
        sys.exit("raw: give at least an opcode, e.g. raw 33")
    if any(b > 0xFF or b < 0 for b in data):
        sys.exit("raw: bytes must be in 00..FF")
    if data[0] in UNSAFE_OPS and not args.force:
        sys.exit(
            f"raw: {data[0]:02X} is known to drop the RFCOMM connection without "
            "changing anything. Pass --force if you really mean it."
        )
    with Device(resolve_address(args.addr), verbose=True) as dev:
        for f in dev.query(data[0], *data[1:], wait=args.wait):
            print(f"  {f.hex():<24}  {f.describe()}")


# --- cli ------------------------------------------------------------------

def byte_arg(text: str) -> int:
    """argparse type: an integer 0..255, in any base int() reads (0x68, 104)."""
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if not 0 <= value <= 0xFF:
        raise argparse.ArgumentTypeError(f"{text} is outside 0..255")
    return value


def main() -> None:
    # Held in a parent parser so --addr and -v work on either side of the
    # subcommand. SUPPRESS is what makes that work: `parents` shares the action
    # objects, so a subparser copy with a concrete default would write it back
    # over a value given before the subcommand. Nothing may call set_defaults()
    # for these dests either -- it mutates the shared actions the same way; the
    # fallbacks are applied on the parsed namespace instead.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--addr", default=argparse.SUPPRESS,
                        help="Bluetooth address, XX:XX:XX:XX:XX:XX; found automatically if omitted")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="show raw hex traffic")

    p = argparse.ArgumentParser(
        parents=[common],
        description="Control a Baseus Bass BS1 NC from Windows over Bluetooth RFCOMM.",
    )
    sub = p.add_subparsers(dest="cmd")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, parents=[common])

    add("devices", "list paired Baseus devices").set_defaults(func=cmd_devices)
    add("status", "dump full device state").set_defaults(func=cmd_status)
    add("monitor", "stream live events, flagging changes").set_defaults(func=cmd_monitor)

    a = add("anc", "get or set noise cancelling")
    a.add_argument("mode", nargs="?", choices=list(ANC_MODES))
    a.add_argument("--level", type=byte_arg,
                   help="level byte sent with the mode (default 0x68 on, 0xFF off); "
                        "the earbuds report 00 while ANC is on, whatever is sent")
    a.set_defaults(func=cmd_anc)

    s = add("spatial", "get or set spatial audio mode")
    s.add_argument("mode", nargs="?", choices=list(SPATIAL_MODES))
    s.set_defaults(func=cmd_spatial)

    e = add("eq", "get or set the EQ preset")
    e.add_argument("preset", nargs="?", choices=list(EQ_PRESETS))
    e.set_defaults(func=cmd_eq)

    b = add("bass", "get or set Bass Boost")
    b.add_argument("state", nargs="?", choices=["on", "off"])
    b.add_argument("--level", type=int, choices=list(BASS_LEVELS),
                   help="boost level 1-5; kept as-is when omitted")
    b.set_defaults(func=cmd_bass)

    w = add("wind", "get or set wind noise reduction")
    w.add_argument("state", nargs="?", choices=["on", "off"])
    w.set_defaults(func=cmd_wind)

    pw = add("playasworn", "get or set play-as-worn (wear detection)")
    pw.add_argument("state", nargs="?", choices=["on", "off"])
    pw.set_defaults(func=cmd_playasworn)

    ll = add("lowlatency", "get or set low-latency mode")
    ll.add_argument("state", nargs="?", choices=["on", "off"])
    ll.set_defaults(func=cmd_lowlatency)

    ld = add("ldac", "get or set LDAC (single-device audio)")
    ld.add_argument("state", nargs="?", choices=["on", "off"])
    ld.set_defaults(func=cmd_ldac)

    r = add("raw", "send a raw frame, e.g. raw 34 01 68")
    r.add_argument("bytes", nargs="+", help="hex bytes; leading BA optional")
    r.add_argument("--wait", type=float, default=2.5)
    r.add_argument("--force", action="store_true", help="send even a known-unsafe opcode")
    r.set_defaults(func=cmd_raw)

    args = p.parse_args()
    args.addr = getattr(args, "addr", None)
    args.verbose = getattr(args, "verbose", False)
    if not getattr(args, "func", None):
        args.func = cmd_status

    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except OSError as exc:
        if args.addr is None:
            # Most failures are the earbuds being in the case or held by the
            # phone app, but the saved address may also be one that was unpaired
            # since. Telling those apart costs a lookup, so forget it instead:
            # the next run looks it up again.
            save_cached_device(None)
        sys.exit(f"Bluetooth error: {exc}")


if __name__ == "__main__":
    main()
