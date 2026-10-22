# Baseus Bass BS1 NC — protocol map

Device: `Baseus Bass BS1 NC`, Classic Bluetooth `54:84:50:XX:XX:XX`, RFCOMM **channel 1**.

## Transport

Classic Bluetooth SPP, RFCOMM channel 1. No BLE needed, no pairing beyond the
normal audio pairing. Reachable from Python's stdlib:

```python
s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
s.connect(("XX:XX:XX:XX:XX:XX", 1))   # the earbuds' Classic address
```

A channel sweep of 1–30 found channel 1 as the only open one.

## Framing

```
app -> device    BA <opcode> [payload...]
device -> app    AA <opcode> [payload...]
```

No length field, no CRC, one frame per RFCOMM packet.

## Conventions

| Pattern | Meaning |
|---|---|
| `BA <report_op>` | query — read-only on every opcode tested |
| `BA <report_op + 1>` | set — changes state |
| `BA 01` | sync — device dumps every state frame it has |
| ack payload `0x0B` | **malformed frame — almost always the wrong payload width** |

The `0x0B` reading was initially wrong: it looked like "feature not supported on
this model", because `BA 24 01` came back with it. It is a length complaint.
`BA 24 01 00` — the same opcode with a two-byte payload — works and flips the
setting. Payload width is per opcode and has to be found by trial; a `0x0B`
means try another width before concluding anything.

The report/set split is a pattern that held for every confirmed pair, not a
safety guarantee. An unknown opcode is unknown whichever side of the pattern it
falls on, and a bare query on a *set* opcode is known to act: see the hazards
below.

Every confirmed setter is `state + 1`, with no exceptions. Only the payload
width varies, and it has to be found per opcode:

| Shape | Example |
|---|---|
| 1-byte payload | Spatial Audio `BA 43 <mode>` |
| 2-byte payload | ANC `BA 34 <mode> <level>` |
| 2-byte payload with a pad | Low latency `BA 24 <on> 00` |

`BA 01` was found during this work and is the cheapest way to enumerate a device.

## Opcode map

Status legend: **confirmed** = verified by write → read-back → restore, or by a
query response matching the Android app's displayed value.

| Report | Set | App setting | Payload | Status |
|---|---|---|---|---|
| `02` | — | Battery | `<L%> 00 <R%> 01` | confirmed |
| `23` | `24` | Low Latency Mode | `<0|1> 00` | **confirmed (read+write)** |
| `25` | `26` | Play as worn | `<0|1>` | **confirmed (read+write)** |
| `30` | `31` | EQ mode | `<preset 0-7>` | **confirmed (read+write)** |
| `33` | `34` | Ambient sound (ANC) | `<mode> <level>` | **confirmed (read+write)** |
| `42` | `43` | Spatial Audio | `<0=Normal 1=Music 2=Cinema>` | **confirmed (read+write)** |
| `51` | `52` | Wind noise reduction | `<0|1>` | **confirmed (read+write)** |
| `53` | `54` | Bass Boost | `<0|1> <level 1-5>` | **confirmed (read+write)** |
| `74` | `75` | LDAC | `<0|1>` | **confirmed (read+write)** — drops the link |
| `21` | ? | Echo of the last change | 3 bytes | encoding not established |
| `19` | — | Per-bud value, probably firmware | `<n> <L hi lo> <R hi lo>` | layout confirmed; meaning a guess |
| `36` | — | ? | `<0|1>`, always reads `01` | unidentified |

Every row was read off a physical unit. The mapping between opcode and app
setting comes from changing one setting at a time in the Android app and diffing
full state dumps (see *Method* below). Four rows of an earlier version of this
table were wrong: `0x42` was labelled EQ mode, `0x51` Spatial Audio, `0x36` Play
as worn, and LDAC's setter was given as `0x74` rather than `0x75`. In each case
the entry had been inferred rather than watched: three from values that looked
plausible next to the app, and LDAC from a dropped connection being read as
proof that the write had landed.

### ANC (`33` / `34`)

```
BA 34 <mode> <level>       mode 0 = Normal, 1 = Noise reduction
                           level 0x68 default, 0xFF when off
AA 34 01                   flat ack — carries no state
AA 33 <mode> <level>       the authoritative state; read it back after setting
```

There is no transparency mode, and this time the conclusion rests on more than
the app:

- the app offers only Normal and Noise reduction;
- `BA 32`, a query for a transparency state, gets no `AA 32` reply and changes
  nothing (snapshots before and after are identical) — every known report
  opcode answers its own query, so silence here means it is not one;
- `AA 32` has never appeared in a full `BA 01` dump;
- the BS1 NC is an open-fit design with no silicone tips, so Normal mode already
  lets outside sound through. A transparency mode would have no seal to undo.

Writing a third mode value (`BA 34 02`) was deliberately not tried: with all of
the above pointing one way, a write with unknown effect is not worth it.

The level byte is live on the wire but not user-facing. While ANC is on, `AA 33`
reports `00` whatever level was written (`0x68` by default); while it is off it
reports `FF`, which is also the value written for off, so whether that is an echo
or a fixed value is untested. Either way it fits an adaptive ANC with no strength
slider.

### Spatial Audio (`42` / `43`) and EQ (`30` / `31`)

```
BA 43 <0|1|2>              set spatial: 0 Normal, 1 Music, 2 Cinema
AA 43 01                   flat ack
AA 42 <value>              state

BA 31 <preset>             set EQ preset
AA 31 01                   flat ack
AA 30 <preset>             state
```

The two are **mutually exclusive**: enabling Spatial Audio makes the earbuds
emit `AA 30 00`, resetting EQ to Baseus Classic, and the Android app asks for
confirmation before disabling one to enable the other. EQ preset indices follow
the reading order of the app's grid:

| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| Baseus Classic | Powerful bass | Hi-Fi Live | Jazz rock | Pop | Clear treble | Acoustic | Customizable |

### Bass Boost (`53` / `54`)

```
BA 54 <0|1> <level>        level 1-5, matching the app's five steps
AA 54 01                   flat ack
AA 53 <0|1> <level>        state
```

The one-byte form `BA 54 <0|1>` is accepted and does flip the switch, but the
device then reports a level it picked itself (`01 05` became `00 02` in one
run). Always send both bytes.

### The `21` echo — unexplained

`AA 21` carries three bytes that move after almost any change, and it does not
drift on its own (ten queries over 30 s with nothing touched returned the same
value every time). What the three bytes mean is **not** established.

A gesture reading fits part of the data and was briefly adopted, then dropped:

| after | `AA 21` | gesture reading | holds? |
|---|---|---|---|
| (idle, before any gesture change) | `01 04 04` | Triple-tap, L+R Voice assistant | matches the app |
| (idle) | `02 06 06` | Tap & hold, L+R Ambient sound | matches the app |
| Triple-tap L → Next | `01 03 04` | Triple-tap, L Next, R Voice assistant | matches the app |
| Triple-tap R → Volume + | `01 03 0B` | L Next, R Volume + | matches the app |
| Double-tap L → Previous | `00 02 01` | Double-tap, L Previous, R Play/Pause | matches the app |
| EQ → Acoustic (`BA 31 06`) | `06 00 01` | gesture `06` does not exist | **breaks** |
| EQ → Acoustic, earlier run | `58 00 01` | gesture `58` does not exist | **breaks** |

So five gesture changes decode perfectly as
`<gesture> <L action> <R action>` — with `00` Double-tap, `01` Triple-tap,
`02` Tap & hold, and actions `01` Play/Pause, `02` Previous, `03` Next,
`04` Voice assistant, `06` Ambient sound, `0B` Volume + — and then an EQ write
produces a frame that cannot be a gesture at all. Either the frame is reused for
more than one purpose, or the gesture fit is a coincidence of small numbers.

Until that is settled the CLI prints the raw bytes and claims nothing. Reading
the whole gesture table would need a parameterised query (`BA 21 <slot>`), which
has not been tried because nothing rules out `0x21` taking a payload as a write
— a bare `BA 34` already acts on ANC — and a failed guess would overwrite a
gesture.

## Case battery — settled: not reported

A 45-second capture across a full lid cycle (lid open → closed → open → one bud
removed) produced **zero frames for the lid transitions**. Closing the lid emitted
nothing; opening it emitted nothing. No frame that could carry a case
percentage appeared at all, in this or any other capture.

The Baseus Android app does not show a case percentage for this model either —
only L and R with charging bolts.

The case was then asked directly. `BA 27` is the obvious query for a case report
(its frame-length entry had been sitting in the client unexplained), and it was
sent in both configurations where a reply could make sense:

| Earbuds | Lid | Reply to `BA 27` | State change |
|---|---|---|---|
| both out | closed | none (only the routine `AA 02`) | none |
| one seated in the case | open | none (only the routine `AA 02`) | none |

The second row is the one that matters: a bud on the charging pins is the only
path by which the earbuds could know the case's level, and they still had
nothing to say.

The case **does** know its own level. Its LED lights for about ten seconds when
a bud is seated, green above 40 % and other colours below. So this is not a case
without a gauge; it is a gauge whose reading never reaches the earbuds' control
channel. An earlier version of this section said "the case has no fuel gauge",
inferred from the silence; the LED shows that was wrong, even though the
conclusion that the CLI cannot read it stands.

Charging state is also not located. The `00`/`01` bytes in `AA 02` are bud IDs,
not charging flags.

## Events on bud removal

Removing one bud from the case was the only physical action that produced
traffic:

```
AA 33 00 FF     ANC switched itself off   (the earbuds' own wear detection)
AA 02 50 00 00 01   the bud left in the case reports 0%
AA 12 xx xx xx 36 84 54 00 00 00 00 00 00 00
```

`AA 12` is a device identity frame: six MAC bytes in reverse order, here
`54:84:36:XX:XX:XX`. That is *not* the Classic address the PC connects to
(`54:84:50:XX:XX:XX`) — same `54:84` prefix, different device — so it is most likely the
second bud's own address, emitted when the pair re-negotiates roles.

A bud sitting in the case reads **0%**. Note this only appeared once the pair
re-negotiated; before that both buds read their true percentage while in the
case with the lid open.

## Google Fast Pair — not emitted

Google Fast Pair BLE advertisements (service `0xFE2C`) carry left/right/**case**
level plus charging bits, on a channel completely independent of this RFCOMM
protocol. That would be the only plausible source of case battery for the
BS1 NC.

A purpose-built BLE advertisement scanner (Windows `BluetoothLEAdvertisementWatcher`,
active scanning) was used to check. Across ~40 s with the buds out of the case and
the lid open, **no `0xFE2C` advertisement appeared** from the earbuds. The scanner
itself works — it decoded service data and manufacturer data from several
neighbouring devices in the same run.

**Positive control passed.** Re-run with another pair of earbuds that support
Fast Pair connected, the same scanner decoded their Fast Pair frame, battery
levels included, on the first pass.

In the same 25-second window the Baseus emitted **no BLE advertisement at all** —
not a Fast Pair frame, not even a name. It was the only paired device that never
appeared. Conclusion: the BS1 NC does not implement Google Fast Pair, and case
battery is unavailable from Windows through any channel. Closed.

## The SPP control channel is single-host

After enabling the earbuds' **Dual-Device Connection** and letting the phone
reconnect, RFCOMM channel 1 stopped accepting connections from the PC —
`WinError 10064`, consistently, across repeated attempts — while A2DP and
Hands-Free audio endpoints stayed present and healthy.

The likely reading is that the earbuds expose exactly one SPP control channel and
the phone's Baseus app is holding it. If that is right, the dual-connection
capture plan (watch frames on the PC while toggling settings on the phone) cannot
work: the phone app and this tool contend for the same channel.

The manual also notes that **enabling LDAC disables dual connection** — the two
are mutually exclusive. Consistent with `AA 74` reading `00` (LDAC off) while dual
connection is active, which is corroborating evidence that `0x74` really is LDAC.

## LDAC (`74` / `75`)

```
BA 75 <0|1>                set
AA 75 01                   flat ack — payload is 01 either way
AA 74 <0|1>                state
```

Toggling it renegotiates the audio link, so the RFCOMM connection drops shortly
after the ack. The CLI therefore reads the result back on a fresh connection once
the link has settled, a few seconds in all; the write does land.

Per the manual, **LDAC on disables dual-device connection**; turning it off
restores it. So turning LDAC on will silently kick a connected phone off.

### The `74` / `74` correction

This row read `74` / `74` — "the setter is the same opcode as the state" — for a
long time, and it was wrong. `BA 74 <anything>` is not a write at all: the device
answers every one of `BA 74 00`, `BA 74 01`, `BA 74 01 00` and `BA 74 02` with
the same `AA 74 <current state>`, with no ack and no change. It behaves as a
query that ignores its payload.

What made the wrong entry survive was the reasoning behind it: the connection
dropped after the write, and a dropped connection was taken as evidence that the
write had landed. The client printed *"connection dropped as expected — the write
still lands"* on exactly the path where nothing had happened, so a silent no-op
was indistinguishable from success.

The real setter is `0x75`, which follows the same `state + 1` rule as every other
setter on this device — the "same opcode" shape was the single exception in the
table, and exceptions in this table have all turned out to be mistakes. It was
found by write → read-back → restore, in both directions:

```
BA 75 01  ->  AA 75 01  ->  AA 74 01
BA 75 00  ->  AA 75 01  ->  AA 74 00
```

Firmware is unchanged across the two observations (`AA 19 <n> 10 23 10 23` both
before and after), so this was never a device-side change.

## Still missing

- **`0x21`**. Moves after nearly every change and is stable when nothing is
  touched, but the three bytes are not decoded. See above.
- **`0x36`**. A single byte that has read `01` on every capture, through every
  setting change made so far. It is not Play as worn — that turned out to be
  `0x25`.
- **`0x19`**. The layout is settled, the meaning is not.

  ```
  both out          AA 19 00  10 23  10 23
  right in case     AA 19 02  10 23  00 00
  left in case      AA 19 02  00 00  10 23
  ```

  Bytes 1–2 belong to the left bud and bytes 3–4 to the right; a bud sitting in
  the case reads `00 00`, just as its battery reads 0 % in `AA 02`. That was
  predicted before the left-bud capture was taken and held in both directions.

  The layout is enough to be useful: the CLI shows a bud as *in case* when
  `AA 02` reads 0 % for it **and** its half of `AA 19` is `00 00`. Both signals
  have always moved together; requiring both means a lag in either one shows
  the plain percentage for a moment instead of a wrong label.

  What the 16-bit value *is* remains a guess. `10 23` reads as 16.35, identical
  on both buds, which is exactly how a per-bud firmware version would look —
  but a hardware revision or any other per-bud constant would look the same. The
  CLI keeps the *Firmware?* label, with the question mark, out of `status`.

  There is nothing to cross-check it against: the Baseus Android app shows no
  firmware version for this model (only the app's own version), and the earbuds
  publish no SDP Device ID record, so Windows has no VID/PID/REV for them —
  their hardware ID is a bare `BTHENUM\Dev_<mac>`. Short of an OTA package to
  compare with, the label stays a guess.

  Byte 0 is not explained. It read `00` and `01` on different occasions with both
  buds out, and `02` with either bud in the case, so it does not say *which* bud
  is seated. A code for how many buds are active, or for which one holds the
  link, would fit; neither is tested.
- ~~**`0x27`**~~. Queried directly and silent in every configuration; see the
  case battery section. Its unexplained frame-length entry has been removed.
- **Find headphones** and the **Settings** submenu have not been captured.

## Commands snapshot diffing cannot reach

Three things the app can do leave no readable state behind, so the diff method
above is blind to them:

- **Find headphones** — plays a tone, changes nothing. The tone stops when the
  app is closed, which is most likely the earbuds reacting to the control
  channel dropping rather than the app sending a stop; if so the CLI could hold
  the channel open the same way and stop on Ctrl+C.
- **Restore default settings** — changes every setting at once, which says
  nothing about which opcode carried the command.
- **Gesture writes** — `AA 21` echoes something after a change, but the encoding
  is not established (see above).

The route to these is the phone's Bluetooth HCI snoop log, which records what
the Baseus app actually sends; the frames sit inside RFCOMM UIH packets, or ATT
writes if the app uses the BLE path.

**That route was blocked on the test phone** (Android 15). Recorded
so it is not retried blindly:

- The Developer options *Bluetooth HCI snoop log* toggle has no effect. After
  enabling it and restarting Bluetooth, `dumpsys bluetooth_manager` still
  reports `sSnoopLogSettingAtEnable = EMPTY`, and no `persist.bluetooth.btsnoop*`
  property exists at all.
- `setprop persist.bluetooth.btsnooplogmode full` is refused without root.
- `settings put global bluetooth_btsnoop_enable 1` is accepted but ignored.
- Neither an on-device bug report nor `adb bugreport` contains a btsnoop log;
  `/data/misc/bluetooth/logs/` is unreadable without root.

So this needs a phone whose snoop toggle works (or root), or static analysis of
the Baseus APK, where the opcodes are compiled-in constants.

Blind probing is not an option here: these commands sit in the same opcode
neighbourhood as factory reset and OTA entry, and a wrong guess costs a reset.

## Notes and hazards

- `BA 34` with **no payload** changes the ANC mode — bare queries on *set*
  opcodes are not safe. Only query report opcodes.
- `BA 37 00` produced an `AA 37 01` ack, a full state re-dump, and then dropped
  the RFCOMM connection, without changing `AA 36`. Treat `0x37` as unsafe.
- A blind sweep of all 256 opcodes was deliberately not run: unknown opcodes may
  include factory reset or OTA-mode entry.

## Method: how the table above was filled

Watching the phone's traffic live does **not** work. Enabling Dual-Device
Connection so the buds hold the phone and the PC at once does not let the PC
listen in: the control channel is single-host, and while the Baseus app is open
on the phone it holds that channel — the PC gets `WinError 10064` for as long as
the app is in the foreground.

What works is snapshot diffing, which needs no live channel at all:

1. Close the Baseus app on the phone. Merely closing it is enough; force-stop is
   not needed, and the phone can stay connected over Bluetooth.
2. `python tools/diff_state.py before-<setting>` — a full `BA 01` dump.
3. Open the app, change **exactly one** setting, close the app again.
4. `python tools/diff_state.py after-<setting>` — the diff names the opcode.

One change per cycle, or the diff cannot say which opcode belongs to which
setting. Start with a setting whose opcode is already known (Spatial Audio) as a
control: if its diff does not show up, the method is broken and running the
unknowns is a waste of time.

That identifies the *state* opcode. The setter is then found with
`tools/find_setters.py`, which tries the adjacent opcodes at several payload
widths and accepts a candidate only when the read-back changes, then restores
the original value. All five setters found this way were `state + 1`.
