# Anker Prime 160W charger — A2687

Field map for the charger's telemetry. This decoder predates the power bank work
and came from a Python port of `AnkerPrimeWebBle_A2687.js`; it is included here
because the two devices share their entire transport, and because having both in
one place is what made the shared layer obvious.

| | |
|---|---|
| advertised name | `ASHDJW…` (stable prefix, unlike the power bank) |
| service | `0000ff09`, same as the whole Prime line |
| firmware observed | `v0.0.5.1` |

## What differs from the power bank

**It needs an Anker account ID.** Without `0x0027` carrying a 40-character
account ID, the pushed `0x0300` stream never starts, and `0x020A` cannot be built
at all. Pass `--user-id` or set `$ANKER_USER_ID`.

That ID is not just authentication. A controlled A/B test — same device, same
handshake, only the ID changed — showed the current account's ID preserved the
selected lock-screen image while an older captured ID made it stop displaying.
The charger ties personalized screen state to that identity.

**Never ship a captured ID as a constant.** It is an account identifier, a
40-character lowercase hex string, recovered from the plain `uid` HTTP header
while the Anker app loads its screensaver endpoints. Supply it at runtime.

**Different scales.** The charger reports millivolts, milliamps and centiwatts.
The power bank uses tenths for all three. Same struct shape, different divisors —
one of the easier ways to produce a confidently wrong reading.

## Realtime frames

Per-port telemetry arrives as type-`0x04` structs of
`[mode, mV u16le, mA u16le, cW u16le]`.

| TLV | meaning |
|---|---|
| `0xA5` / `0xA6` / `0xA7` | port C1 / C2 / C3 telemetry struct |
| `0xAC` / `0xAD` / `0xAE` | port C1 / C2 / C3 cable descriptor |
| `0xB4` | per-port attached-device identity, 3 x (VID u16le, PID u16le) |
| `0xB5` | per-port brand/model codes, 3 x 4 bytes |

Total output power is summed from the ports rather than read from a field.

### Cable and charging-protocol codes

The cable descriptor's last two bytes are a capability byte and a fast-charge
protocol byte:

| capability | meaning |
|---|---|
| `00` | 3A-60W MAX |
| `01` | 5A-100W MAX |
| `02` | EPR-240W MAX |

| protocol | meaning |
|---|---|
| `01` | Apple PD Fast Charging |
| `02` | Samsung Fast Charging |
| `03` | Samsung Super Fast Charging |

### Attached-device identity

`0xB4` is **inferred from captured traffic**, not from any recovered app table.
On the capture that established it, C2 held an Apple device and decoded to VID
`0x05AC` while an empty C1 read the `0xFFFA`/`0xFFFB` sentinel. Treat as
provisional until more devices are sampled.

`DEVICE_MODEL_NAMES` in `charger.py` only contains (VID, PID) pairs that have
actually been observed on real hardware alongside what the official app printed.
An unknown PID falls back to the vendor name plus the raw value — never a guess.

`0xB5` carries newer brand/model codes. The brand table is known; the matching
85-entry model table was not recovered, so a model code can be reported
numerically but not named.

**Worth contrasting with the power bank**, whose equivalent identity slot stayed
all-`0xFF` even with a 90 W laptop attached and drawing. That firmware appears
not to report attached-device identity at all.

## Snapshot frames

`0x0200` (and `0x0A00`, `0x0040`, `0x0405`) carry identifiers and settings rather
than live port data:

| TLV | meaning |
|---|---|
| `0xA1` | state code |
| `0xA2` | serial or identifier |
| `0xA4` | product code |
| `0xD0` / `0xD1` | port configuration |
| `0xFD` | firmware tag |

Anything unrecognized is kept as raw hex under `field_0xNN` rather than dropped.

## Status

This decoder was written against a working device and has been in use, but it
has not been re-verified against hardware since being reshaped into this
repository's interface — the charger was tied up by another application
throughout the power bank work. The refactor was checked against synthetic
payloads built to the documented struct shape, which exercises the decoding path
but not the assumption that the device still sends what it used to.

There are no charger captures in `captures/` for the same reason. Recording one
is the obvious next step, and would let the charger decoder join the same replay
regression the power bank enjoys.
