# Anker Prime Power Bank (20K, 220W) — A110G

Field map for the power bank's telemetry, with the evidence behind each entry.
For how it was worked out — and the five hypotheses that were wrong along the
way — see [NOTES.md](../NOTES.md).

Everything below is pushed at 1 Hz once a session is open — no polling, and no
Anker account ID required.

Target device on which it was observed:

| | |
|---|---|
| model | **A110G** — Anker Prime Power Bank (20K, 220W), Black Myth: Wukong edition |
| advertised name | `AJ7DLCH0F49600286` |
| MAC | `7C:E9:13:78:CA:DA` |
| CoreBluetooth address (this Mac) | `AABFD37C-5524-715F-1F3E-6B167D41ED4B` |
| firmware | `v0.0.5.1`, re-verified on `v0.0.5.2` |

Product specs that the captures line up with: 220 W total output across 2x USB-C
plus 1x USB-A, 100 W USB-C input (20 V / 5 A), and a Pogo-pin charging base —
either the 100 W A1902 or the 150 W 3-port A1903. The dock captures here peaked
at 99.4 W, and Anker documents that a bank on the A1902 base cannot output to
other devices while charging, which matches those captures showing both USB-C
ports as empty.

## Snapshot-only settings

`0x0200` carries settings the realtime stream does not. One is confirmed:

| field | meaning | how |
|---|---|---|
| `0xAC` | **Pomodoro timer, seconds** | read `1500` on the 25-minute default, then `1200` the moment 20 minutes was set in the app |
| `0xE2` | `[enabled, seconds]` | repeats `0xAC` behind a flag byte |

Still unidentified, with the values seen so far:

| field | value | note |
|---|---|---|
| `0xA2` | `1605` → `1707` | moves slowly and cumulatively; a lifetime counter of some kind |
| `0xA3` | `7` | constant across every capture |
| `0xA9`, `0xAA` | `100`, `100` | two settings pinned at 100 |
| `0xAB` | `0x80` | flag byte |
| `0xE0` | `0x3F` → `7FFFFFFF` | the firmware update widened this from 1 byte to 4 |
| `0xBE` | `5` → `6` | increments across sessions |

Battery health and cycle count were the obvious guesses for `0xA3`/`0xA9`/`0xAA`,
but the Anker app for this model shows neither, so there is no ground truth to
match them against and they stay unidentified rather than plausibly labelled.

Untested hardware path: the 150 W A1903 base, which unlike the 100 W A1902
allows output while the bank is docked.

## Firmware

Decoded against **v0.0.5.1**, re-verified unchanged on **v0.0.5.2**. Every
realtime field survived the update; the two differences were `0xE0` widening
from one byte to four, and the sticky per-port power slots resetting to zero,
which a reboot would explain.

After any future update, re-run `replay --decode` over a stored capture and then
a live `status` before trusting the map again.

## What is identical to the charger

The power bank is the same stack as the A2687 top to bottom, so
`ff09.py` is shared between the two device decoders rather than duplicated:

- same GATT service `8c850001-0302-41c5-b46e-cf057c562025`, write on `…0002`,
  notify on `…0003`;
- same `FF 09 <len16> 03 00 <group> <cmd|flags>` framing with the trailing XOR
  checksum;
- same AES-GCM with the fixed initial key, same 16-byte AAD;
- same ECDH P-256 session-key exchange on command `0x0021`;
- same TLV bodies with a leading type tag (`01` u8, `02` u16, `03` u32, `04`
  struct).

It advertises `0000ff09` exactly like the charger. Manufacturer data is the same
shape too — `02 | MAC(6) | 02 B4 | model` — with model byte `0A` for the bank
against `05` for the charger.

## What is different

**No account ID is needed.** The charger will not start its `0x0300` stream
unless `0x0027` carries the Anker account ID. The bank starts streaming at 1 Hz
after `0x0022` alone; `0x0027` and `0x020A` were never sent in any capture here.

**Telemetry lives in different TLVs.** The charger's per-port structs do not
apply. The bank's layout is below.

## Frames

| command | direction | what it is |
|---|---|---|
| `0x0029` | reply | fixed label (`Charging`), firmware, serial, MAC |
| `0x0200` | reply | settings + status snapshot, sent once on request |
| `0x0300` | pushed | realtime telemetry, ~1 Hz, unprompted |

`0x0300` is a subset of `0x0200`: the realtime block appears in the snapshot
shifted by `+0x0D` (`0x0300 0xA5` is `0x0200 0xB2`, and so on).

## Telemetry frames are not encrypted

The handshake is encrypted — fixed key, then the ECDH session key, exactly like
the charger. **The telemetry that follows is not.** A `0x0300` frame carries its
TLVs in clear right after the header, with the encrypted flag clear in the
command byte:

    FF09 7300 03 01 11 03 00 | A1 01 31 A2 03 04 3B 55 …
                        └flags=0x03, bit 0x40 not set
                                     └TLVs, no ciphertext

The charger encrypts its telemetry, so an implementation written for it first
tends to grow a `if frame.encrypted` guard and silently drop every power bank
frame. That produces a connection that handshakes fine and then never delivers
anything — which is exactly what happened to the macOS reporter. Branch on the
frame's own flag, never on which device you think you are talking to.

Note that fixtures cannot catch this: they replay decoded payloads and bypass
the frame layer entirely. It needs a test at the raw-frame level.

## `0x0300` field map

Two encodings are in play, and mixing them up is the main trap here:

- **tenths** — u16 little-endian, one decimal place. Volts, amps and watts.
- **decimal pair** — two bytes, `[integer, remainder out of 100]`. Battery
  percentage and time remaining. Read as a u16 these produce large numbers that
  look plausible and are wrong.

The hardware has three ports: **C1 and C2 are bidirectional, A is output only.**

| TLV | meaning | encoding | confidence |
|---|---|---|---|
| `0xA1` | state code, `0x31` in every capture | raw u8 | unknown, constant |
| `0xA2` | **battery percentage** | decimal pair → `31.91 %` | confirmed |
| `0xA3` | **time to full** | `[flag, hours, minutes]` | confirmed |
| `0xA4` | **thermal state**, `1` normal / `2` limited | u8 | confirmed |
| `0xA5` | **total input power** | `[flag, tenths]` | confirmed |
| `0xA6` | **total output power** | `[flag, tenths]` | confirmed |
| `0xA7` | **charging base input** | `[mode, V, A, W]` tenths | confirmed |
| `0xA8` | **port C1**, both directions | 14-byte block, see below | confirmed |
| `0xA9` | **port C2**, both directions | 14-byte block | confirmed |
| `0xAC` | **port A** | 8-byte block | confirmed |
| `0xAF` | **temperature 1** | u8 °C | confirmed via `0xA4` |
| `0xB0` | **temperature 2** | u8 °C | confirmed via `0xA4` |
| `0xB1` | constant 5 | u16 | unknown |
| `0xFE` | timestamp slot, always 0 | u32 | — |

### `0xA4` is the thermal state, and how that was proven

`0xA4` reads `1` both while charging and while discharging, so it is not a
charging flag. It goes to `2` when the bank refuses to charge — a cable can be
attached and live on C2 and it will still sit at 5 V drawing nothing.

That left two readings that fit equally well: "thermally limited", or plainly
"no power is flowing". They were separated by sampling the bank every 45 s with
**nothing plugged into any port** while it cooled:

| sample | temps | `0xA4` |
|---|---|---|
| 1–4 | 46/47 → 45/46 °C | `2` |
| 5–6 | 44/45 °C | `1` |

No power flowed in any of the six samples, so "no power flowing" cannot explain
a field that changed between them. Temperature was the only variable, and the
flip sits between 44 °C and 45 °C on `0xAF`. That also corroborates `0xAF`/`0xB0`
as real temperatures, since an unrelated field tracks their threshold.

**The `0x0029` string is not a state either.** It says `Charging` even
mid-discharge, so it is a fixed product label.

Take the charge/discharge direction from `0xA5`/`0xA6`, the total input and
output power. `0xA4` is thermal, and `0xA7` goes stale — an early version of
this decoder read direction off `0xA7` and reported `charging=False` during a
100 W charge.

`0xA3` only counts down to full. On discharge the firmware sends `0h00m` rather
than switching to a time-to-empty.

Its hours byte read `0` in every early capture, so the `[hours, minutes]` split
was unproven until a pass-through capture — 28.6 W in on C1 while 23.0 W went
out on C2 — reported `9h48m`, which is what ~5.6 W of net input should take.

**Input and output are tracked independently.** In that same capture `0xA5` read
28.6 W and `0xA6` read 23.0 W, each matching its own port exactly.

**`0xA6` is a true sum**, not a mirror of whichever port is busiest. Every
single-port capture leaves those two possibilities indistinguishable, so it took
driving both C ports at once: C1 at 14.9 V x 1.9 A = 28.6 W and C2 at
19.9 V x 4.4 A = 89.1 W, with `0xA6` reading exactly 117.7 W. That capture also
shows the bank negotiating a different PD voltage per port.

### How the electrical scaling was confirmed

Not by assumption — by watching it move. Early in a charge the input read
`[02, 204, 48, 994]` and later, as the charge tapered, `[02, 204, 34, 700]`:

    20.4 V x 4.8 A = 97.9 W   reported 99.4 W   (current truncated from 4.87)
    20.4 V x 3.4 A = 69.4 W   reported 70.0 W

Both are self-consistent at one decimal place, and 20 V / 5 A / 100 W is exactly
the PD contract this bank charges on.

Battery percentage was confirmed the same way: `31.87 %` rose to `39.10 %` over
4.4 minutes at ~85 W average, which implies a pack of roughly 75 Wh — the right
size for a 20,000 mAh Prime.

### The 14-byte port blocks

    C1 idle      00 0000 0000 E803 FF 00 FFFFFFFF 00
    C1 under load 01 C700 2D00 8403 02 07 FFFFFFFF 00
                  └mode └V── └A── └W── └link┘ └identity┘

Bytes 0..6 are the same `[mode, V, A, W]` shape as the input channel.

**How `0xA8` was bound to C1:** a 90 W laptop was put on C1 while the capture
ran. Only this block moved, and it read `[mode 1, 19.9 V, 4.5 A, 90.0 W]` —
19.9 x 4.5 = 89.6, self-consistent. `0xA6` (total output) carried the identical
number in the same frame and tracked it byte-for-byte for the whole capture, so
the port block and the total confirm each other.

**How `0xA9` was bound to C2:** the charge cable was moved to C2 while the bank
was too hot to accept it. Only this block changed, to `5.0 V` at zero amps —
a cable sitting at USB default voltage with no PD contract negotiated.

**How `0xAC` was bound to the A port:** it was all zeros in every capture until
trickle-charge mode was switched on for the A port, then read `5.0 V` at zero
amps — the rail up, waiting for a low-draw device. Its block is 8 bytes rather
than 14 because USB-A negotiates nothing, so there is no attach or identity tail;
use the voltage, not an attach flag, to tell that port is live.

**Both C ports carry both directions in one block**, separated by the mode byte:
`1` output, `2` input, `0` idle. Each was seen both ways — C1 under a 90 W laptop
load and then taking a 69.6 W charge, C2 idle at 5 V and then taking 100 W.

### `0xA7` is the charging base

It has a port block's `[mode, V, A, W]` shape but no attach or identity tail —
the dock contacts negotiate nothing, so there is nothing to report there.

It was found by contradiction rather than by looking for it. In the earliest
captures it carried a live charge tapering 99.4 W → 70 W **while both C-port
blocks reported nothing attached**, which meant the power was arriving through
something that was not a USB-C port. Every charge since — through C1, through
C2 — has been reported by that port's own block instead, with `0xA7` frozen at
its last live values. The owner then confirmed the bank had been sitting on its
charging base for those first captures.

**It goes stale rather than resetting**, so read it only when its mode byte is
non-zero. This is the same trap as the sticky power slot, one level up.

The remaining bytes:

- **`[5:7]` is sticky.** It is the live power while `mode != 0`, but an idle port
  keeps whatever it last carried instead of resetting: C1 still read `91.3 W`
  minutes after the laptop came off, matching its final live reading exactly.
  Never read this slot without checking the mode byte first.
- **`[8]` is an attach flag** — `00` empty, `07` with a cable in the port. True
  both under a 90 W load and for the idle 5 V cable on C2, which is the only way
  to tell "cable attached, nothing negotiated" from "empty".
- **`[7]` tracks the source role** — `02` whenever the port is outputting, `FF`
  whenever it is drawing in or attached but idle. Seen on both C ports in both
  roles, including one capture with C1 taking input and C2 sourcing at once.
- **`[9:13]`** stayed `FF FF FF FF` even with a laptop attached and drawing 90 W.
  It is the same slot the charger uses for VID/PID, so this firmware appears
  simply not to report what is plugged in — unlike the A2687.

## Method, for extending this

`watch` prints only fields that changed, which is the entire technique: make one
thing happen in the physical world, see which bytes move. Put a load on C1 and
`0xA8` stops being all zeros. Pull the input and `0xA5` drops. Anything that
never moves across a session is configuration, not telemetry.

`decode.py` deliberately prints *every* plausible reading of each field — u8,
u16 both endians, decimal pair, scaled by 10/100/1000, ASCII — so the correct
one can be recognized rather than guessed.

Four claims have died this way already, and each had looked solid: `0xA4` as a
charging flag, then `0xA4` as a constant, the `0x0029` string as a live state,
and the port block's power slot as a negotiated PD contract. Each fell to a
capture taken in a condition the earlier ones had never covered — the opposite
power direction, an idle port, a hot bank. Anything above that has only ever
been seen in one condition deserves the same suspicion; the confidence column
records what was tested, not how convincing the number looked.

The `0xA4` case is the template worth copying. Two readings fit every capture
taken, so the next capture was chosen specifically to make them disagree — hold
one candidate cause fixed (no power flowing, in every sample) and vary the other
(temperature). A capture that both hypotheses predict equally is wasted.

## Safety

Read-only. The writes this tool makes are the session handshake plus the
`0x0200` status read. No setting is changed, and no control command is
implemented.
