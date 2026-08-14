# anker-prime-ble

Debugging and reverse-engineering tools for the two Anker Prime devices on my
desk, which turned out to speak the same BLE protocol:

| device | model | what it reports |
|---|---|---|
| Prime Power Bank (20K, 220W) | A110G | battery %, per-port V/A/W both directions, totals, time to full, thermal state, 2 temperatures, Pomodoro timer |
| Prime 160W charger | A2687 | per-port V/A/W, cable rating, fast-charge protocol, attached-device identity |

Both push telemetry at roughly 1 Hz over BLE once a session is open. Everything
here is **read-only**: the only writes are the session handshake and a status
read. No control or settings command is implemented.

## Why one repository

The two devices share the entire transport — same GATT service, same `FF 09`
framing, same fixed-key AES-GCM, same ECDH P-256 handshake. Only the payload
inside the TLVs differs. So the split is:

```
anker_prime_ble/
├── ff09.py       shared transport: framing, TLV, AES-GCM, ECDH handshake
├── decode.py     shared field annotator + differential tracker
├── device.py     shared port/state shapes and the device interface
├── session.py    shared BLE session, handshake driver, capture recording
├── charger.py    A2687 telemetry decoding
├── powerbank.py  A110G telemetry decoding
└── cli.py        one CLI for both
```

A device module is just a `DeviceProfile` plus a decoder. Adding a third Anker
Prime device means writing one file — the transport, session, and CLI do not
change.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Verified on CPython 3.14; any 3.11+ should work.

## Use

```bash
.venv/bin/python -m anker_prime_ble scan                          # find devices
.venv/bin/python -m anker_prime_ble status <addr>                 # live dashboard
.venv/bin/python -m anker_prime_ble watch  <addr>                 # only what changes
.venv/bin/python -m anker_prime_ble session <addr> --record cap.jsonl
.venv/bin/python -m anker_prime_ble replay cap.jsonl --decode     # offline, no radio
```

`scan` guesses the device from its advertised name. Everything else takes
`--device charger|powerbank` (default `powerbank`, or set `$ANKER_DEVICE`).

The charger additionally needs an Anker account ID — `--user-id` or
`$ANKER_USER_ID` — or its stream never starts. The power bank needs nothing.

```
AJ7DLCH0F49600286  fw v0.0.5.2  [Charging]
  battery 58.85%   time left 0h00m   charging=False
  in 0.0W    out 117.7W
    C1: 14.9V 1.9A 28.6W out
    C2: 19.9V 4.4A 89.1W out
    A : idle
  pomodoro 20 min (on)
  temps 45C / 46C
```

### macOS Bluetooth

CoreBluetooth is gated by TCC, and a process whose host app has no
`NSBluetoothAlwaysUsageDescription` is **killed on sight** — no traceback, no
output, just an abort that looks like a hang. Run these from Terminal.app or
another terminal that has been granted Bluetooth access.

A device already connected to a phone app will not advertise, so quit the Anker
app before scanning. The power bank also sleeps on its own; press its button if
a command sits waiting.

## Documentation

- **[NOTES.md](NOTES.md)** — how this was worked out, including every hypothesis
  that turned out to be wrong and what killed it. Read this before extending a
  decoder.
- **[docs/powerbank.md](docs/powerbank.md)** — A110G field map with the evidence
  for each field.
- **[docs/charger.md](docs/charger.md)** — A2687 field map.
- **[captures/](captures/)** — 11 recorded sessions covering charge, discharge,
  pass-through, dual output, thermal throttling, and a firmware upgrade. These
  are the evidence behind the field maps and double as regression fixtures:
  change a decoder, replay all eleven, see whether they still make sense.

## Status

The power bank's telemetry is decoded and confirmed. What remains is a handful
of `0x0200` settings bytes and the 150 W A1903 charging base, which unlike the
100 W A1902 allows output while docked. See the end of
[docs/powerbank.md](docs/powerbank.md).
