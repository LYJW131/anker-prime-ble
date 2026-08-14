# Notes

How the power bank's protocol got decoded, and what it cost. The field maps in
`docs/` say *what* each byte means; this says *how it was established* and where
the reasoning nearly went wrong — which matters more if you are extending it.

## Where this started

The A2687 charger was already decoded — not by me. That work is
[Hyper-Beast/Anker_Prime_160W_WebBLE](https://github.com/Hyper-Beast/Anker_Prime_160W_WebBLE),
a Web Bluetooth page whose `AnkerPrimeWebBle_A2687.js` is where the framing,
crypto constants and handshake sequence in this repository come from; it credits
flip-dots/SolixBLE and atc1441/Anker_Prime_BLE_hacking before it. Starting from
a working charger implementation is the only reason the power bank took an
afternoon rather than a fortnight.

The power bank was a guess: it advertised the same `0xff09` service UUID, so the
transport was probably identical.

It was. The charger's handshake worked verbatim — same GATT service, same
framing, same fixed-key AES-GCM, same ECDH P-256 exchange on `0x0021`. Within
one session the bank was streaming `0x0300` frames at 1 Hz.

Two differences showed up immediately:

- **No account ID needed** — wrong, and it took a long time to find out. The
  bank *starts* streaming after `0x0022` alone, then drops the link at 26
  seconds. `0x0027` with the account ID is what makes the session persist. Every
  capture taken here was shorter than 26 s, so the two possibilities predicted
  identical recordings. See below.
- **Different TLV layout.** None of the charger's port structs applied.

## The thing that nearly stopped it before it started

On macOS, a Python process that touches CoreBluetooth **dies with SIGABRT** if
the host app has no `NSBluetoothAlwaysUsageDescription` in its Info.plist. No
traceback. No stderr. An empty log file. It looks exactly like a hang.

The crash report is the only place it says so:

> This app has crashed because it attempted to access privacy-sensitive data
> without a usage description.

Terminal.app holds the Bluetooth grant, so the working loop was: write the
script, hand it to Terminal via `osascript`, poll its log file.

```bash
osascript -e 'tell application "Terminal" to do script
  "cd <repo> && .venv/bin/python -m anker_prime_ble … > /tmp/out.log 2>&1;
   echo __DONE__ >> /tmp/out.log; exit"'
```

Worth knowing before spending an hour on a "hang".

## A thing that is not in the protocol at all

The charger will not stream without an Anker **account** ID, and no amount of
staring at BLE traffic produces one — it is not on the device. It came from a
phone-side HTTPS capture of the Anker app, out of a plain request header, with
the encrypted bodies untouched. `docs/charger.md` has the recipe.

Worth internalizing as a category: when a device withholds data pending some
value you cannot find in its own traffic, the value probably lives in the vendor's
app or cloud, and the cheapest path is usually a header rather than a payload.

## The method

**Differential capture.** Make exactly one thing change in the physical world,
see which bytes move. That is the whole technique, and `watch` exists to serve
it — it prints only fields whose value changed since the last frame.

`decode.py` complements it by printing *every* plausible reading of each field:
u8, u16 both endians, decimal pair, scaled by 10/100/1000, ASCII, MAC. The point
is to make the correct reading recognizable rather than guessed at.

Everything gets recorded to JSONL, so `replay` can re-run a changed decoder over
every past capture without touching the radio. By the end there were eleven
captures covering charge, discharge, pass-through, dual output, thermal
throttling, and a firmware upgrade. Changing a decoder and replaying all eleven
caught more mistakes than any amount of staring at hex.

## Five hypotheses that were wrong

Every one of these fit **all the data available when it was formed**. That is
the actual lesson: a hypothesis consistent with every capture you have is not
thereby correct — it may just mean your captures are all the same shape.

**1. `0xA4` is a charging flag.** It read `1` during charging. Then a discharge
capture showed `1` as well. Dead.

**2. `0xA4` is a constant.** Having been burned once, I labelled it "constant,
meaning unknown". Two captures later it read `2`. Also dead — and worse than the
first, because "constant" is the label you stop questioning.

What it actually is: a thermal state, `1` normal and `2` limited. Proving that
took a deliberately designed experiment, described below.

**3. The `0x0029` string is live state.** It reads `Charging`. It reads
`Charging` mid-discharge too. It is a fixed product label.

**4. The port block's power slot is a negotiated PD contract.** While C1 was the
charge path it held `100.0 W` with volts and amps at zero, next to a measured
input of `99.4 W` — a beautiful story about contracts versus measurements. Then
a port went idle and kept reading `91.3 W`, exactly the last live value from
minutes earlier. The slot is simply **sticky**: it retains its last value instead
of resetting. It means nothing unless the mode byte is non-zero.

(It does reset on reboot. The firmware update cleared every port's value, which
is how that got confirmed.)

**5. `0xA7` is the input channel.** It carried the live charge in the first
captures. Then a 100 W charge through C2 was reported by C2's own block while
`0xA7` sat frozen at values tens of minutes stale. This one had a real
consequence: `charging` was derived from `0xA7`'s mode byte, so the dashboard
cheerfully printed `charging=False` next to `in 100.0W`.

`0xA7` turned out to be the **charging base**. It was found by contradiction, not
by looking for it: in the early captures it carried a live charge *while both
USB-C port blocks reported nothing attached*, which meant the power was arriving
through something that was not a USB-C port. The owner then confirmed the bank
had been sitting on its dock.

## The experiment that settled `0xA4`

Two readings fit every capture: "thermally limited", and plainly "no power is
flowing". Both predicted the same thing in all existing data.

The fix is to design a capture the two hypotheses **disagree** about: hold one
candidate cause fixed and vary the other. Nothing was plugged into any port —
so no power flowed in any sample — while the bank cooled, sampled every 45 s:

| sample | temps | `0xA4` |
|---|---|---|
| 1–4 | 46/47 → 45/46 °C | `2` |
| 5–6 | 44/45 °C | `1` |

"No power flowing" cannot explain a field that changed between samples where no
power flowed. Temperature was the only variable. It also corroborated
`0xAF`/`0xB0` as real temperatures, since an unrelated field tracks their
threshold.

**A capture both hypotheses predict equally is a wasted capture.** That is the
one transferable idea here.

The same shape of reasoning settled `0xA6`. Whether it was a true sum or a mirror
of the busiest port is indistinguishable in every single-port capture — and ten
of the eleven were single-port. Driving both C ports at once resolved it in one
frame: C1 at 28.6 W, C2 at 89.1 W, `0xA6` reading exactly 117.7 W.

## The sixth wrong hypothesis, and the worst one

**"The power bank does not need an account ID."** It starts streaming without
one, so this looked settled — and it was stated in the docs, encoded in the
profile, and used to justify not asking users for it.

It is wrong. Without `0x0027` the bank drops the link after 26 seconds. With it,
a 99-second session ran to completion.

What makes this the worst of the six is that the evidence was *systematically*
unable to show it: every capture here ran 20–45 seconds, and the ones that ran
longer were cut short by the very disconnect being explained. Both hypotheses
predicted every recording in the archive. The device owner suggested trying the
account ID early on and was told the captures ruled it out — they did not rule
it out, they just could not see the difference.

The general form: **when a hypothesis is contradicted, check whether the evidence
against it could even have shown the alternative.** Eleven captures agreeing
means nothing if all eleven are the same shape. This repository's own notes say
exactly that, two sections down, and it happened anyway.

## The one the fixtures could not catch

The power bank sends its telemetry **unencrypted**, while the charger encrypts
its own. A second implementation, written charger-first, guarded its receive path
with `if frame.encrypted` and dropped every power bank frame. The connection
handshook cleanly (handshake frames *are* encrypted), reported itself connected,
and delivered nothing — then dropped every 14 seconds when the stall watchdog
concluded the stream was dead.

The conformance fixtures did not catch it, and could not have: they replay
decoded payloads and never touch the frame layer. That is a real limit of this
kind of contract worth knowing — it pins down *interpretation*, not *plumbing*.

What found it was recording the raw frames on the failing side and diffing them
against a known-good capture. Reading the code did not; the two handshakes were
byte-for-byte identical, and every plausible culprit checked out. Two hours of
staring lost to what one capture answered in a minute.

## Two encodings, and why that matters

The bank mixes them:

- **tenths** — u16 little-endian, one decimal place: volts, amps, watts.
- **decimal pair** — `[integer, remainder out of 100]`: battery percentage and
  time remaining.

Reading a decimal pair as a u16 gives a large, plausible-looking, wrong number.
`31.91 %` becomes `12,703`, which could be anything and looks like something.
Both live behind named functions in `device.py` for that reason.

The `[hours, minutes]` split of the time field was itself unverified for most of
this work — every early capture had `0` hours, so the byte was never exercised.
It was only confirmed by a pass-through capture that charged at 28.6 W while
outputting 23.0 W and reported `9h48m`, which is what ~5.6 W of net input should
take.

## On the confidence column

`docs/` marks each field confirmed, corroborated, or open. That column records
**what conditions were actually tested**, not how convincing the number looked.
Every one of the five wrong hypotheses above looked convincing.

Practical rule: a field seen in only one direction, one port, one thermal state,
or one firmware version is one capture away from surprising you.

## Extending this

1. `watch` while changing one thing physically. Note what moved.
2. If two readings both fit, design the capture that makes them disagree — do
   not collect more of the same.
3. Record everything; `replay --decode` over all captures after any decoder
   change.
4. When a field is confirmed, write down *what was tested*, not just the
   conclusion.

Adding a third device: write a `DeviceProfile` plus a decoder module, matching
the shape of `powerbank.py`. The transport, session, CLI, and generic annotator
should not need edits — if they do, that is worth a second look, because these
two devices agreed on all of it.
