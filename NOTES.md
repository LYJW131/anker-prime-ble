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

## The charger's custom cover

The A2687 can show a custom picture. The title and the JPEG live in Anker's
cloud. This firmware keeps **four pixel slots** on the charger; a fifth add
in the official app **re-uploads** (BLE transfer again, overwriting a slot)
instead of allocating a fifth JPEG. The cloud list can still show more than
four. The charger reports the current cloud id in snapshot TLV `0xE1`.
Switching the current picture is a single BLE write, command `0x021F`,
group `0x0F`. `docs/screensaver.md` is the recipe; this section is what it
cost to get there.

### Tools that actually moved the work

- **This repo's session**, from Terminal.app or Ghostty with a Bluetooth TCC
  grant. CoreBluetooth still SIGABRTs any host without
  `NSBluetoothAlwaysUsageDescription`.
- **ProxyPin HARs** of the official iOS app against
  `aiot-api-cn.anker.com.cn`. The CN host accepts the same plaintext JSON as
  [anker-solix-api](https://github.com/thomluther/anker-solix-api) if you omit
  `x-encryption-info`. The official app wraps bodies in `algo_ecdh`; that
  wrapping is not required.
- **PacketLogger** from Additional Tools for Xcode, **New iOS Trace**, phone on
  USB, Developer Mode, and Apple's Bluetooth logging profile installed on the
  phone. macOS Trace sees zero iPhone HCI. Live traces cannot Save/Export;
  stop the trace first. A new PacketLogger window is fine; a new *BLE session*
  is not — see below.
- **Peekaboo** to drive PacketLogger and System Settings when the Bluetooth
  profile and Accessibility grants are in place.
- **Anker 3.22.2** (`com.anker.charging`) from APKPure as an XAPK. The Flutter
  AOT is `libapp.so`. This split was only `armeabi-v7a`. Strings named
  `setMiniChargeScreensaverReq`, `action_set_screen_saver_params`,
  `screensaverId`, `hash_code`, `screenSaverType`. Flutter does not build the
  BLE TLV; that happens in the VMP-protected `ak_iot_kit` native layer.
  [Hyper-Beast's 3.18.0 notes](https://github.com/Hyper-Beast/Anker_Prime_160W_WebBLE)
  already flagged `0x021F` as the multi-parameter screensaver write and left
  the body unpublished.

### Wrong turns

**The slot is not on the wire.** Analytics event
`AN_App_Custom_Picture_Change` carries the 1-based `seq`. `0xE1` does not.
Bytes 2–3 are the cloud `id` as u16le. Bytes 0–1 are a flag word, usually
`00 03` or `80 03` — not a constant `0x0380`. Names such as `FSF` never
appear on BLE.

**`0x021F` ACKs garbage.** Every guessed 47-byte layout came back
`00 A1 01 31`, including an empty GET. The ACK means the command exists, not
that the body applied. Only a change in `0xE1` counts. Nearby `0x021D`
still rejects with `04 A1 01 31`. `0x0220` / `0x0221` returned the same
on empty or guessed bodies — they are the image-transfer commands, and
they only apply with the official TLV.

**Auth was not the gate.** Official `GCMUserAuthCmd` documents `0x0027`
`A3 = password`. This firmware applies `0x0204` brightness (TLV `0xA9`,
0–100) with only `A2` = account id. Empty, user-id, serial and hash values in
`A3` did not change that. `session.auth_password` exists for the day a unit
needs it.

**Guessing the 47-byte TLV from XOR offsets was not enough.** Two official
writes in one session differ at plaintext offsets 10–11 (`id` u16le), 17–20
(`hash_code` u32le) and 43 (unix epoch). Consecutive slots (2 and 3) make
offset 10 look like either the id low byte *or* a 0-based seq (`0x01 ^ 0x02
= 0x03`). A same-session jump `1→2→4→1→3` kills the seq reading: 2^4 is
`0x06`/`0x02`, the observed byte is `0x04`, and no extra byte moves. Putting
id at A2`[2:4]` and seq at A2`[5]` — the first layout that matched those
offsets — writes the seq into the real id slot. Firmware ACKs and ignores it.

**`FE 04 <epoch>` is the wrong timestamp wrapper.** Brightness uses that.
`0x021F` uses `FE 05 03 <epoch>` (typed u32). A 36-byte A2 blob with the id
and hash in the XOR slots also fails, because the official body has no A2:
it is `A1` / `A3=type 3` / `A4=id` / `A5=hash` / `FD="SmallChargingUrl"` /
typed `FE`.

**The session key is not on HCI.** Official GCM is ECDH P-256; the private
half never leaves the phone. Ciphertext XOR equals plaintext XOR only while
the app stays connected and PacketLogger keeps one iOS Trace. A new window
on a *new* connection cannot be XORed against an earlier one. The first
nine ciphertext bytes of `0x021F` are the cheap check: they stay put inside
one session (`1929B44C…` vs `719DD669…` were different sessions).

**Nonce reuse is the decrypt.** This firmware feeds the same GCM nonce to
every post-handshake frame. `0x0300` starts `A1 01 31 A2 03 02 46 06 …`.
XOR that known prefix against a `0x0300` captured in the same window as the
select writes, then XOR the keystream onto each `0x021F`. Idle C1
(`A5 08 04 00` plus zeros) is the template that made id and hash match on
all four jumps; a live charging C1 template missed the last hash byte.
Do not commit PacketLogger traces that still contain a live session.

**Cloud login looks encrypted until it is not.** Cold-start HARs do not show
a helpful `ENCRYPT_APP_PUBLICKEY` header. Key exchange is
`openapi/oauth/key/exchange` with `client_public_key` in the JSON. SMS send
is a bare `{phone_number, phone_code: "86", kind: "login"}`. SMS login
without the Solix-style ECDH envelope (`client_secret_info.public_key` plus
the static server point in `docs/screensaver.md`) returns `code: 10000` /
`请求失败` and no hint. One new token can kick the phone app, after which
the charger stops advertising until that app is quit.

**The APK names the fields and then stops.** `libapp.so` strings are the
Flutter parameter map. The native kit is iJiami VMP; JADX of `classes.dex`
is a stub. APKPure's 3.22.2 XAPK advertised only `armeabi-v7a`, so
arm64-only dumpers such as blutter never ran. Image transfer is a separate
path (`action_start_transfer_screen_saver_image` and friends). Select of a
picture that is already on the charger is the one 47-byte write; a picture
that exists only in the cloud will ACK and stay put until the official app
has pushed the pixels. Official-app testing on this unit: **four on-device
slots**. Adding a fifth re-uploads rather than growing the device set. That
is how the 2026-08-19 add-cover capture was provoked. That trace recovered
the pixel path: one `0x021F` (new id `45470`), one `0x0220` start (size
25397, chunk 156, count 163, ACK every 10), then 163 `0x0221` writes.
Same-session XOR against the known `0x021F` body opened `0x0220`; the
first `0x0221` plaintext begins `FFD8…JFIF`. Cloud `hash_code` for that
row is `0xc026bfef` (XOR of the last hash byte against a 0x0300 template
was off by one). Do not commit that `.pklg` — it still holds a live
session. Flooding all `0x0221` writes without waiting for the every-10
ACK overruns the charger (`12 A1 01 31` still at index 10). Wait on
every tenth chunk.

**Uploading a new cover is two different transports.** Cloud
`add_manual_clock_screensavers` only allocates `id` / `hash_code` / URL.
The charger never fetches that URL. Pixels go over BLE
(`action_start_transfer_screen_saver_image`, `sendImageDataGroup`). Existing
HARs only have list/get_url. The Flutter map for add is `{sn?, img_url,
hash_code}`; OSS writes start at `/app/cloudstor/get_app_up_token_general`
and the response fields Flutter checks are `upToken` / `keyPrefix`. Live CN
wants `{type: <int>, file_name}` and replies with snake_case `uptoken` (a
signed PUT URL) and `key_prefix`. Types 4/6/7 work; 6 and 7 are the
`edge-aiot` host. Official JPEGs are 240×240, not the 375 overlay, and
`hash_code` is IEEE CRC-32 of those bytes (five/five). Passport login
rate-limits at `100028`. Do not guess the BLE chunk opcode — `0x020C` is
port-history, not image data, even though it uses the same `packageNumber` /
`packageTotalNumber` names.

### What is now confirmed

`charger.screensaver_select_tlv(id, hash_code)` is the official body.
Live writes moved `0xE1` 45314 → 24551, 24551 → 45317, 45313 → 45410, and
45410 → 24551. Pixel transfer is `0x0220` + `0x0221`. Pushing FSF
(`24551`, 20876 bytes) after the official `45470` add restored it and
evicted `45313`, not the picture on screen. Pushing `45313` (18539 bytes)
then showed that picture. Listing is still HTTP
`/mini_power/v1/app/style/get_manual_clock_screensavers`. There is no HTTP
"make this the current picture".
