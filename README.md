# baseus-cli

Command-line control for **Baseus Bass BS1 NC** earbuds on Windows.

`baseus.py` is a single file using only the Python standard library — it talks
to the earbuds over Classic Bluetooth RFCOMM through `socket.AF_BLUETOOTH`.

The protocol was worked out against a physical BS1 NC; full map in
[PROTOCOL.md](PROTOCOL.md).

## Requirements

- Windows 10 or 11, Python 3.9+
- The earbuds paired **and connected** in Windows Bluetooth settings

Two things will make every command fail with a connection error:

- **The earbuds are in the case or charging.** They drop Bluetooth entirely, so
  there is nothing to talk to.
- **The Baseus phone app is open on a phone that is also connected.** The earbuds
  expose exactly one control channel and the app holds it. Close the app.

## Usage

```
python baseus.py                             show everything (default)
python baseus.py status
python baseus.py monitor                     live event stream, Ctrl+C to stop
python baseus.py devices                     list paired Baseus devices

python baseus.py anc on|off                  noise cancelling
python baseus.py spatial normal|music|cinema
python baseus.py eq acoustic                 classic powerbass hifi jazz
                                             pop treble acoustic custom
python baseus.py bass on|off --level 1-5
python baseus.py wind on|off                 wind noise reduction
python baseus.py playasworn on|off
python baseus.py lowlatency on|off
python baseus.py ldac on|off                 (single-device audio)

python baseus.py raw 34 01 68                send a raw frame
python baseus.py -v status                   show raw hex traffic
python baseus.py --addr XX:XX:XX:XX:XX:XX status
```

Leave the argument off any setting command to read the current value instead:
`python baseus.py eq` prints the active preset.

The Bluetooth address is found automatically from Windows' device list; `--addr`
is only needed if you have more than one Baseus device paired. The lookup is
saved to `%LOCALAPPDATA%\baseus-cli\device.txt` so later runs skip it; it is
dropped after a failed connection and refreshed by `devices`.

`status` prints only the settings, and only what is confirmed. The raw frames
behind it — opcodes, the reports still unidentified, and the one that is merely
guessed to be a firmware version — are one `-v` away.

The app calls ANC *Ambient sound*, with modes *Normal* and *Noise reduction*;
the CLI uses the name it is better known by.

### Example

```
> python baseus.py status
Baseus Bass BS1 NC  (54:84:50:XX:XX:XX)

  Battery                left 90%   right 90%
  ANC                    on
  Wind noise reduction   on
  Spatial Audio          Normal
  EQ mode                Acoustic
  Bass Boost             on  (level 5)
  Play as worn           on
  Low Latency Mode       on
  LDAC                   off
```

## What works

| Setting | Read | Write |
|---|---|---|
| Battery (L/R) | yes | — |
| ANC | yes | yes |
| Spatial Audio | yes | yes |
| EQ mode | yes | yes |
| Bass Boost (on/off + level) | yes | yes |
| Wind noise reduction | yes | yes |
| Play as worn | yes | yes |
| Low Latency Mode | yes | yes |
| LDAC | yes | yes |
| Gestures | no — `AA 21` is undecoded | no |

Spatial Audio and EQ are mutually exclusive on this model: turning Spatial Audio
on resets the EQ preset to Baseus Classic, exactly as the phone app warns.

### Things that are not possible

- **Case battery.** The case knows its own level — its LED shows it — but the
  earbuds never report it: they stay silent when asked directly, even with a
  bud seated on the pins, the Baseus phone app does not show it either, and the
  BS1 NC does not implement Google Fast Pair (all verified — see PROTOCOL.md).
  There is no channel that carries it.
- **Charging state.** The earbuds are offline while charging, so nothing can be
  read at that moment.
- **ANC strength.** There is no user-facing strength setting. A level byte goes
  out with every ANC write, but while ANC is on the earbuds report `00` whatever
  was sent; while it is off they report `FF`.
- **Transparency mode.** The app offers only Normal and Noise reduction, and the
  earbuds do not answer a query for a transparency state. On this open-fit
  design Normal already lets outside sound through.

## LDAC

`ldac on|off` works, with two side effects worth knowing:

- Toggling renegotiates the audio link, which takes the control connection
  down shortly after the command. `ldac` waits for the link to settle and reads
  the result back on a fresh connection, so it takes a few seconds.
- With LDAC **on**, the earbuds connect to one device only; dual-device
  connection is disabled and an already-connected phone gets kicked off and does
  not reconnect on its own.

Whether the audio link actually carries LDAC afterwards is up to the host:
this only flips the setting in the earbuds.

## Layout

```
baseus.py        the CLI
PROTOCOL.md      opcode map, framing, what is confirmed vs guessed
tools/           research scripts used to work out the protocol
  diff_state.py    snapshot the full state and diff it against the last one
  find_setters.py  bounded write -> read-back -> restore search for set-opcodes
```

## Adding a setting

The method that worked, in order of preference:

1. Snapshot the state, change **one** setting in the phone app, snapshot again.
   `tools/diff_state.py` does that and prints the diff, which names the opcode.
   Do not skip the control step: start with a setting whose opcode is already
   known, so a broken run shows up before the unknowns are spent on it.
2. Guess the setter and verify it by **write → read-back → restore**. Never trust
   an ack; only a changed read-back counts. `tools/find_setters.py` does this.
3. An ack payload of `0x0B` means the payload width is wrong, not that the
   feature is unsupported — try another width before giving up.

Inferring instead of observing is how four rows of an earlier version of this
map ended up wrong. A dropped connection is not evidence that a write landed,
and a plausible-looking value is not evidence of anything. Only a read-back that
changed counts.

Do not sweep opcodes blindly. Unknown opcodes may include factory reset or OTA
entry, and `0x37` is already known to kill the connection.

## Disclaimer

This is an independent project, not affiliated with, endorsed by or supported
by Baseus. "Baseus" and "Bass BS1 NC" are used only to name the hardware this
works with; the trademarks belong to their owners.

The protocol was reverse-engineered on a single pair of earbuds and is not
documented by the manufacturer. Every setting command writes to the device, and
`raw` sends whatever it is given. Use it at your own risk: the software comes
with no warranty (see [LICENSE](LICENSE)), and a different firmware or hardware
revision may behave differently from what is described here.

## License

[MIT](LICENSE)
