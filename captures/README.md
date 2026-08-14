# Captures

Recorded BLE sessions with the A110G power bank. Each line of a `.jsonl` is one
frame: direction, command, the raw frame, and the decrypted payload.

These are both the evidence behind [docs/powerbank.md](../docs/powerbank.md) and
a regression suite. After changing a decoder, replay all of them — no radio, no
device:

```bash
for f in captures/*.jsonl; do
  echo "== $f"
  .venv/bin/python -m anker_prime_ble replay "$f" --decode | grep -E '^  (battery|in )'
done
```

| file | state | why it exists |
|---|---|---|
| `powerbank-01` | dock charging 99.4 W | first session; established the transport works |
| `powerbank-02` | dock charging, tapering | showed battery % climbing |
| `powerbank-03` | dock charging 70.0 W | confirmed V x A = W at a second operating point |
| `powerbank-04` | C1 output 90 W | bound `0xA8` to C1 |
| `powerbank-05` | thermally limited, C2 attached | cable at 5 V drawing nothing; bound `0xA9` to C2 |
| `powerbank-06` | idle, cooling | start of the `0xA4` experiment |
| `powerbank-07` | C2 input 100 W | C2 in the other direction |
| `powerbank-08` | C1 input 69.6 W | killed "`0xA7` is the input channel" |
| `powerbank-09` | firmware v0.0.5.2, A port trickle | bound `0xAC` to the A port; Pomodoro timer confirmed |
| `powerbank-10` | pass-through: 28.6 W in, 23.0 W out | first non-zero hours in the time field |
| `powerbank-11` | dual output 28.6 + 89.1 W | proved `0xA6` is a true sum |

Captures 01–08 are firmware v0.0.5.1; 09 onward are v0.0.5.2. The upgrade
changed no realtime field — see the firmware section of the field map.

They contain the device's serial number and MAC address. That is deliberate:
without them the captures cannot be tied back to the hardware they describe, and
this repository is private.
