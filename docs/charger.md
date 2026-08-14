# Anker Prime 160W charger — A2687

Field map for the charger's telemetry. This decoder predates the power bank work
and began as a Python port of `AnkerPrimeWebBle_A2687.js` from
[Hyper-Beast/Anker_Prime_160W_WebBLE](https://github.com/Hyper-Beast/Anker_Prime_160W_WebBLE),
which is the origin of the framing, crypto constants and cable tables used here
— see [Prior work](../README.md#prior-work). It is included in this repository
because the two devices share their entire transport, and because having both in
one place is what made the shared layer obvious.

Everything below that is marked as observed was re-verified against hardware;
where this decoder now disagrees with its upstream, the disagreement is called
out with the capture that caused it.

| | |
|---|---|
| advertised name | `ASHDJW…` (stable prefix, unlike the power bank) |
| service | `0000ff09`, same as the whole Prime line |
| serial | `ASHDJW7CF49200487` |
| MAC | `7C:E9:13:77:C3:64` |
| firmware observed | `v0.0.5.1` |

## What differs from the power bank

**It needs an Anker account ID.** Without `0x0027` carrying a 40-character
account ID, the pushed `0x0300` stream never starts, and `0x020A` cannot be built
at all. Pass `--user-id` or set `$ANKER_USER_ID`.

That ID is not just authentication. A controlled A/B test — same device, same
handshake, only the ID changed — showed the current account's ID preserved the
selected lock-screen image while an older captured ID made it stop displaying.
The charger ties personalized screen state to that identity.

**Never ship a captured ID as a constant.** Supply it at runtime, and use the
account that actually owns the charger — see below.

### Getting the account ID

It is a 40-character lowercase hex string, and it is **not** the charger serial,
the BLE address, a phone identifier, or an auth token. Nothing on the device or
in the BLE traffic contains it — it belongs to the Anker account.

The way it was obtained here, which needed no decryption at all:

1. Install **ProxyPin** on the phone (an on-device HTTPS capture app; iOS and
   Android both work) and trust its certificate.
2. Open the Anker app and go to the charger's **screensaver / display settings**,
   so it fetches the screensaver endpoints.
3. Export the session as HAR.
4. Find any request to Anker's API and read the plain **`uid` request header**.
   That is the ID.

The request *bodies* are encrypted; they were never needed. The ID travels in
clear in a header.

**It is not only authentication.** A controlled A/B test — same charger, same
handshake, same polling, only the ID changed — found that the account's own ID
preserved the selected lock-screen image, while an older ID captured from a
different session made the image stop displaying. The charger ties personalized
screen state to this identity, so borrowing someone else's ID does not just fail
to authenticate: it changes what the device shows.

Store it somewhere real. On this machine the macOS app keeps it in the Keychain
under `anker-user-id`, and this tooling reads `$ANKER_USER_ID` at runtime. It is
deliberately absent from this repository, including from the captures — see
`captures/README.md`.

### Other differences

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

`0x0200` (and `0x0A00`, `0x0040`, `0x0405`) carry identifiers and settings
alongside the port data described in the next section:

| TLV | meaning |
|---|---|
| `0xA1` | state code |
| `0xA2` | serial or identifier |
| `0xA4` | product code |
| `0xD0` / `0xD1` | port configuration |
| `0xFD` | firmware tag |

Anything unrecognized is kept as raw hex under `field_0xNN` rather than dropped.

## The snapshot carries live port data

The service this decoder came from deliberately skipped port structs in
`0x0200`, treating it as settings only. A capture taken with no account ID
disagreed — and that case matters, because without an ID the snapshot is the
*only* frame the charger sends.

`0xA5` decoded to 20.08 V x 4.444 A = 89.2 W against a reported 89.15 W, while
`0xB4` named an Apple laptop on that same port and `0xAC` reported an EPR-240W
cable doing Apple PD fast charging. Three independent TLVs agreeing is enough to
trust the struct, so `parse_snapshot` now runs the realtime decoder too.

Whether the original exclusion was protecting against a different firmware, or a
different command in `SNAPSHOT_COMMANDS`, is unknown. Only `0x0200` has been
observed here; `0x0A00`, `0x0040` and `0x0405` have not.

## Status

Verified against hardware end to end: handshake, identity, the `0x0200` snapshot,
and — with an account ID supplied — the `0x020A` reply and the pushed `0x0300`
stream at 1 Hz. See `captures/charger-01.jsonl` (no ID, snapshot only) and
`captures/charger-02.jsonl` (with ID, 35 stream frames).

`0x0027` is what unlocks the stream: the same session without it produced no
`0x0300` frame at all.
